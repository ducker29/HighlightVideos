"""Tkinter desktop UI. Workers communicate through a queue, never through widgets."""
import copy
import queue
import re
import shutil
import subprocess
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from analyzer import list_models
from app_backend import open_report, run_analysis, recalc_shorts_for_report
from job_store import DATA, ROOT, analysis_bounds, load_json, parse_time, save_json, timecode, validate_interval
from renderer import approval_key, output_filename, render_approved, render_shorts_approved, shorts_approval_key
from clip_padding import padding_seconds, prepare_review_report, refresh_padding_overlaps, set_candidate_padding
from ui_editing import install_editing
from transcript_review import review_sections, transcript_chunks
from transcript_editing import apply_transcript_edits
from smart_crop_storage import create_settings, load_settings, save_settings, settings_path
from subtitle_settings import DEFAULTS as SUBTITLE_DEFAULTS, load_settings as load_subtitle_settings, save_settings as save_subtitle_settings
from shorts_settings import DEFAULTS as SHORTS_DEFAULTS, load_settings as load_shorts_settings, save_settings as save_shorts_settings
from local_source import local_source_path
from downloader import video_id_from_url

STATUS = {"pending": "На проверке", "approved": "Утверждён", "rejected": "Отклонён"}


class HighlightApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Highlight Pipeline — горизонтальные хайлайты 16:9")
        self.root.geometry("1250x900")
        self.root.minsize(1000, 700)
        self.events = queue.Queue()
        self._poll_timers = set()
        self._closed = False
        self.busy = False
        self.context = None
        self.quality_window = None
        self._loading_candidate = False
        self.controls = []
        self.settings = load_json(ROOT / "config.json")
        self.subtitle_settings = SUBTITLE_DEFAULTS
        self.subs_window = None
        self.shorts_export_settings = SHORTS_DEFAULTS
        self.shorts_settings_window = None
        shorts_path = DATA / "shorts_render_settings.json"
        if shorts_path.exists():
            try:
                self.shorts_export_settings = load_shorts_settings(shorts_path)
            except (ValueError, OSError) as exc:
                messagebox.showwarning("Настройки Shorts", f"Используются 1080p и CRF 18. Настройки не загружены:\n{exc}", parent=root)
        subtitle_path = DATA / "subtitle_settings.json"
        if subtitle_path.exists():
            try:
                self.subtitle_settings = load_subtitle_settings(subtitle_path)
            except (ValueError, OSError) as exc:
                messagebox.showwarning("Subs Settings", f"Используется исторический стиль. Настройки не загружены:\n{exc}", parent=root)
        saved = DATA / "ui_settings.json"
        if saved.exists():
            self.settings.update(load_json(saved))
        self.variables = {key: tk.StringVar(value=str(value)) for key, value in self.settings.items()}
        self.variables.setdefault("local_video_path", tk.StringVar(value=""))
        self.variables["analyze_full_video"] = tk.BooleanVar(value=self.settings.get("analyze_full_video", True))
        # Shorts настройки
        self.variables["shorts_min_duration_sec"] = tk.StringVar(value=str(self.settings.get("shorts_min_duration_sec", 20)))
        self.variables["shorts_target_duration_sec"] = tk.StringVar(value=str(self.settings.get("shorts_target_duration_sec", 40)))
        self.variables["shorts_max_duration_sec"] = tk.StringVar(value=str(self.settings.get("shorts_max_duration_sec", 60)))
        self.status = tk.StringVar(value="Готово. Укажите ссылку или локальный файл и длительность, затем запустите анализ.")
        self._build()
        self.start.trace_add("write", self._review_bounds_changed)
        self.end.trace_add("write", self._review_bounds_changed)
        self.variables["analyze_full_video"].trace_add("write", lambda *_: self.sync_analysis_range())
        self.sync_analysis_range()
        self.root.bind("<Destroy>", self._on_destroy, add="+")
        self._queue_poll()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def button(self, parent, text, command, **layout):
        widget = ttk.Button(parent, text=text, command=command)
        widget.grid(**layout)
        self.controls.append(widget)
        return widget

    def entry(self, parent, key, row, column, width=12, values=None):
        if values:
            widget = ttk.Combobox(parent, textvariable=self.variables[key], values=values, width=width)
        else:
            widget = ttk.Entry(parent, textvariable=self.variables[key], width=width)
        widget.grid(row=row, column=column, sticky="ew", padx=5, pady=4)
        install_editing(widget)
        self.controls.append(widget)
        return widget

    def _build(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        settings_container = ttk.Frame(outer)
        settings_container.pack(fill="x")
        self.source_settings_expanded = True
        self.source_settings_toggle = ttk.Button(settings_container, text="▼ Источник и настройки",
                                                   command=self.toggle_source_settings)
        self.source_settings_toggle.pack(fill="x")
        settings = self.source_settings_body = ttk.Frame(settings_container, padding=8)
        settings.pack(fill="x")
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="YouTube VOD:").grid(row=0, column=0, sticky="w")
        self.url_entry = self.entry(settings, "youtube_url", 0, 1, 65)
        self.url_entry.grid(columnspan=4)
        self.button(settings, "Вставить", self.paste_url, row=0, column=5, padx=5)
        self.quality_button = ttk.Button(settings, text="Загрузить", command=self.open_quality_window)
        self.quality_button.grid(row=0, column=6, padx=5)
        ttk.Label(settings, text="Локальный файл (приоритет):").grid(row=1, column=0, sticky="w")
        self.local_video_entry = self.entry(settings, "local_video_path", 1, 1, 65)
        self.local_video_entry.grid(columnspan=4)
        self.button(settings, "Обзор…", self.browse_local_video, row=1, column=5, padx=5)
        ttk.Label(settings, text="Длительность, секунды: от").grid(row=2, column=0, sticky="w")
        self.entry(settings, "min_duration_sec", 2, 1)
        ttk.Label(settings, text="до").grid(row=2, column=2)
        self.entry(settings, "max_duration_sec", 2, 3)
        ttk.Label(settings, text="Запас в конце, с:").grid(row=2, column=4)
        self.entry(settings, "end_padding_sec", 2, 5, 6, values=("0", "0.5", "1"))
        ttk.Label(settings, text="Whisper:").grid(row=3, column=0, sticky="w")
        self.entry(settings, "asr_model", 3, 1, values=("large-v3", "medium", "small"))
        ttk.Label(settings, text="Устройство:").grid(row=3, column=2)
        self.entry(settings, "device", 3, 3, values=("cuda", "cpu"))
        ttk.Label(settings, text="Язык:").grid(row=3, column=4)
        self.entry(settings, "language", 3, 5, 6)
        ttk.Label(settings, text="Ollama URL:").grid(row=4, column=0, sticky="w")
        self.entry(settings, "ollama_url", 4, 1, 30)
        ttk.Label(settings, text="LLM:").grid(row=4, column=2)
        self.model_choice = self.entry(settings, "llm_model", 4, 3, 32, values=(self.settings["llm_model"],))
        self.button(settings, "Обновить модели", self.refresh_models, row=4, column=4, columnspan=2, padx=5)
        ttk.Label(settings, text="LLM-редактор:").grid(row=5, column=0, sticky="w")
        self.editorial_choice = self.entry(settings, "editorial_model", 5, 1, 65,
                                           values=(self.settings["editorial_model"],))
        self.editorial_choice.grid(columnspan=5)

        # Настройки Shorts
        shorts_settings = ttk.Frame(settings)
        shorts_settings.grid(row=6, column=0, columnspan=6, sticky="ew", pady=4)
        self.shorts_enabled_var = tk.BooleanVar(value=self.settings.get("shorts_enabled", False))
        checkbox = ttk.Checkbutton(shorts_settings, text="Включить Shorts 9:16", variable=self.shorts_enabled_var)
        checkbox.grid(row=0, column=0, padx=(0, 12), sticky="w")
        self.controls.append(checkbox)
        ttk.Label(shorts_settings, text="Мин. длительность:").grid(row=0, column=1)
        self.entry(shorts_settings, "shorts_min_duration_sec", 0, 2, width=6)
        ttk.Label(shorts_settings, text="Целевая:").grid(row=0, column=3)
        self.entry(shorts_settings, "shorts_target_duration_sec", 0, 4, width=6)
        ttk.Label(shorts_settings, text="Макс. длительность:").grid(row=0, column=5)
        self.entry(shorts_settings, "shorts_max_duration_sec", 0, 6, width=6)

        scope = ttk.Frame(settings)
        scope.grid(row=7, column=0, columnspan=6, sticky="ew", pady=4)
        checkbox = ttk.Checkbutton(scope, text="Анализ полного видео", variable=self.variables["analyze_full_video"])
        checkbox.grid(row=0, column=0, padx=(0, 12), sticky="w")
        self.controls.append(checkbox)
        ttk.Label(scope, text="с").grid(row=0, column=1)
        self.range_start_entry = self.entry(scope, "analysis_start_min", 0, 2)
        ttk.Label(scope, text="до").grid(row=0, column=3)
        self.range_end_entry = self.entry(scope, "analysis_end_min", 0, 4)
        ttk.Label(scope, text="мин от начала исходного видео").grid(row=0, column=5, padx=5)

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=8)
        self.button(actions, "Анализировать видео", self.analyze, row=0, column=0, padx=(0, 8))
        self.button(actions, "Открыть сохранённый отчёт", self.load_report, row=0, column=1, padx=8)
        self.button(actions, "Нарезать утвержденные клипы", self.render, row=0, column=2, padx=8)
        self.button(actions, "Рендер все Shorts", self.render_shorts, row=0, column=3, padx=8)
        self.button(actions, "Смарт-кроп", self.open_smart_crop, row=0, column=4, padx=8)
        self.button(actions, "Пересчитать Shorts", self.recalc_shorts, row=1, column=3, padx=8)
        self.button(actions, "Subs Settings", self.open_subs_settings, row=1, column=4, padx=8)
        self.button(actions, "Настройки Shorts", self.open_shorts_settings, row=1, column=5, padx=8)
        self.button(actions, "Применить запас к отчёту", self.apply_end_padding,
                    row=0, column=5, padx=8)
        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=150)
        self.progress.grid(row=0, column=6, padx=8)
        ttk.Label(outer, textvariable=self.status, wraplength=1180).pack(fill="x", pady=(0, 5))

        panes = ttk.Panedwindow(outer, orient="vertical")
        panes.pack(fill="both", expand=True)
        
        # Notebook с вкладками
        self.notebook = ttk.Notebook(panes)
        
        # Вкладка Хайлайты
        highlights_tab = ttk.Frame(self.notebook)
        self.notebook.add(highlights_tab, text="Хайлайты 16:9")
        
        table_frame = ttk.Frame(highlights_tab)
        columns = ("candidate_id", "status", "start", "end", "duration", "score", "title", "padding", "render")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", height=8, selectmode="browse")
        for key, label, width in zip(columns, ("ID", "Решение", "Начало", "Конец", "Длина, с", "Оценка", "Тема / заголовок", "Запас", "Нарезка"),
                                     (65, 110, 100, 100, 75, 60, 270, 65, 95)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, stretch=(key == "title"))
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.bind("<<TreeviewSelect>>", lambda event: self.show_candidate())
        self.table.bind("<Button-1>", self.table_action)
        table_frame.pack(fill="both", expand=True)
        
        # Вкладка Shorts
        shorts_tab = ttk.Frame(self.notebook)
        self.notebook.add(shorts_tab, text="Shorts 9:16")
        
        shorts_table_frame = ttk.Frame(shorts_tab)
        shorts_columns = ("candidate_id", "status", "start", "end", "duration", "score", "title", "render")
        self.shorts_table = ttk.Treeview(shorts_table_frame, columns=shorts_columns, show="headings", height=8, selectmode="browse")
        for key, label, width in zip(shorts_columns, ("ID", "Решение", "Начало", "Конец", "Длина, с", "Оценка", "Тема / заголовок", "Рендер"),
                                     (65, 110, 100, 100, 75, 60, 270, 95)):
            self.shorts_table.heading(key, text=label)
            self.shorts_table.column(key, width=width, stretch=(key == "title"))
        shorts_scroll = ttk.Scrollbar(shorts_table_frame, orient="vertical", command=self.shorts_table.yview)
        self.shorts_table.configure(yscrollcommand=shorts_scroll.set)
        self.shorts_table.pack(side="left", fill="both", expand=True)
        shorts_scroll.pack(side="right", fill="y")
        self.shorts_table.bind("<<TreeviewSelect>>", lambda event: self.show_shorts_candidate())
        shorts_table_frame.pack(fill="both", expand=True)
        
        panes.add(self.notebook, weight=2)
        self.notebook.bind("<<NotebookTabChanged>>", lambda _e: self._sync_active_tab_panel())

        review = ttk.LabelFrame(panes, text="Ревью выбранного кандидата", padding=6)
        fields = ttk.Frame(review)
        fields.pack(fill="x")
        self.start = tk.StringVar()
        self.end = tk.StringVar()
        self.title = tk.StringVar()
        for column, (label, variable, width) in enumerate((("Начало:", self.start, 15), ("Конец:", self.end, 15), ("Заголовок:", self.title, 50))):
            ttk.Label(fields, text=label).grid(row=0, column=column * 2, padx=3)
            widget = ttk.Entry(fields, textvariable=variable, width=width)
            widget.grid(row=0, column=column * 2 + 1, sticky="ew", padx=3)
            install_editing(widget)
            self.controls.append(widget)
        fields.columnconfigure(5, weight=1)
        decisions = ttk.Frame(review)
        decisions.pack(fill="x", pady=5)
        self.button(decisions, "Сохранить правки / на доработку", lambda: self.decide("pending"), row=0, column=0, padx=3)
        self.button(decisions, "Утвердить интервал", lambda: self.decide("approved"), row=0, column=1, padx=3)
        self.button(decisions, "Отклонить", lambda: self.decide("rejected"), row=0, column=2, padx=3)
        self.button(decisions, "Посмотреть интервал (FFplay)", self.preview, row=0, column=3, padx=3)
        ttk.Label(review, text="Двойной щелчок по реплике — изменить текст. Правки сохраняются кнопками выше и требуют утверждения.").pack(anchor="w")
        self.details = tk.Text(review, height=12, wrap="word", state="disabled")
        self.details.tag_configure("context", foreground="#6b7280")
        self.details.tag_configure("marker", foreground="#1d4ed8")
        self.details.tag_configure("partial", foreground="#b45309")
        self.details.pack(fill="both", expand=True, pady=4)
        self.details.bind("<Double-Button-1>", self.edit_transcript_sentence)
        panes.add(review, weight=3)
        log_frame = ttk.LabelFrame(panes, text="Ход выполнения", padding=4)
        self.logs = tk.Text(log_frame, height=6, wrap="word", state="disabled")
        self.logs.pack(fill="both", expand=True)
        panes.add(log_frame, weight=1)

    def log(self, message):
        self.events.put(("log", str(message)))

    def paste_url(self):
        if self.busy or self.url_entry.instate(["disabled"]) or self.url_entry.instate(["readonly"]):
            return
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            self.status.set("В буфере обмена нет доступного текста.")
            return
        if not text:
            self.status.set("В буфере обмена пустой текст.")
            return
        self.variables["youtube_url"].set(text)
        self.url_entry.focus_set()
        self.url_entry.icursor(tk.END)

    def start_worker(self, operation, function):
        if self.busy:
            return
        self.busy = True
        for control in self.controls:
            control.state(["disabled"])
        self.progress.start(15)
        self.status.set(operation)

        def work():
            try:
                self.events.put(("result", (operation, function())))
            except Exception as exc:
                self.events.put(("error", (str(exc), traceback.format_exc())))

        threading.Thread(target=work, daemon=True, name="highlight-worker").start()

    def _finish(self):
        self.busy = False
        for control in self.controls:
            control.state(["!disabled"])
        self.sync_analysis_range()
        self.progress.stop()

    def sync_analysis_range(self):
        disabled = self.busy or self.variables["analyze_full_video"].get()
        for entry in (self.range_start_entry, self.range_end_entry):
            entry.state(["disabled"] if disabled else ["!disabled"])

    def restore_report_settings(self, context):
        settings = context["report"]["settings"]
        for key, value in settings.items():
            if key in self.variables:
                self.variables[key].set(value)
        # Reports produced before the UI range controls still know their actual ASR range.
        if "analyze_full_video" not in settings:
            interval = context["transcript"].get("analysis_range")
            if interval:
                duration = context["manifest"]["media"]["duration_sec"]
                self.variables["analyze_full_video"].set(interval["start"] == 0 and abs(interval["end"] - duration) < 0.001)
                self.variables["analysis_start_min"].set(f"{interval['start'] / 60:.12g}")
                self.variables["analysis_end_min"].set(f"{interval['end'] / 60:.12g}")
            else:
                self.variables["analyze_full_video"].set(True)
        # Восстанавливаем настройки Shorts
        if "outputs" in settings and "shorts" in settings["outputs"]:
            shorts = settings["outputs"]["shorts"]
            self.shorts_enabled_var.set(shorts.get("enabled", False))
            if shorts.get("enabled"):
                self.variables["shorts_min_duration_sec"].set(str(shorts.get("min_duration_sec", 20)))
                self.variables["shorts_target_duration_sec"].set(str(shorts.get("target_duration_sec", 40)))
                self.variables["shorts_max_duration_sec"].set(str(shorts.get("max_duration_sec", 60)))
        self.sync_analysis_range()

    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.append_log(payload)
                    self.status.set(payload)
                elif kind == "error":
                    self._finish()
                    self.status.set("Ошибка. Подробности в журнале.")
                    self.append_log(payload[1])
                    messagebox.showerror("Ошибка обработки", payload[0], parent=self.root)
                elif kind == "result":
                    self._finish()
                    operation, result = payload
                    if operation == "Проверка моделей Ollama…":
                        self.model_choice["values"] = result
                        self.editorial_choice["values"] = result
                        self.status.set("Доступные модели: " + ", ".join(result))
                    elif isinstance(result, dict) and "report" in result:
                        self.context = result
                        self.restore_report_settings(result)
                        self.refresh_table()
                        self.refresh_shorts_table()
                        for warning in result["report"].get("analysis_warnings", []) + result["report"].get("editorial_warnings", []):
                            self.append_log(warning)
                        rejections = result["report"].get("editorial_rejections", [])
                        if rejections:
                            self.append_log("Редакционная проверка отклонила кандидатов:")
                            for item in rejections:
                                self.append_log(f"{item['candidate_id']}: {item['reason']}")
                            self.append_log(f"Исходный отбор сохранён: {result['report'].get('raw_report_path', '')}")
                        interval = result["transcript"].get("analysis_range")
                        scope = (f" Анализ: {timecode(interval['start'])}–{timecode(interval['end'])}." if interval else "")
                        if result["report"].get("analysis_scope", {}).get("label"):
                            scope += " " + result["report"]["analysis_scope"]["label"] + "."
                        self.status.set(f"{result['manifest']['title']} — кандидатов: {len(result['report']['candidates'])}.{scope} Ожидается ревью.")
                        if result["report"].get("analysis_complete") is False:
                            note = (" Это проверочный отбор выбранных блоков, не всего VOD."
                                    if result["report"].get("probe_only") else
                                    " Есть блоки с неполным отбором — см. журнал.")
                            self.status.set(self.status.get() + note)
                    else:
                        if self.context and not self.has_unsaved_edits():
                            self.show_candidate()
                        self.status.set(f"Готово. Создано клипов: {len(result)}.")
                        messagebox.showinfo("Рендер завершён", "\n".join(result), parent=self.root)
        except queue.Empty:
            pass
        self._queue_poll()

    def _queue_poll(self):
        if self._closed:
            return
        timer = None

        def poll_once():
            self._poll_timers.discard(timer)
            self._poll()

        timer = self.root.after(100, poll_once)
        self._poll_timers.add(timer)

    def _on_destroy(self, event):
        if event.widget is self.root:
            self._closed = True
            for timer in list(self._poll_timers):
                self.root.after_cancel(timer)
            self._poll_timers.clear()

    def append_log(self, message):
        self.logs.configure(state="normal")
        self.logs.insert("end", message + "\n")
        self.logs.see("end")
        self.logs.configure(state="disabled")

    def toggle_source_settings(self):
        self.source_settings_expanded = not self.source_settings_expanded
        if self.source_settings_expanded:
            self.source_settings_body.pack(fill="x")
        else:
            self.source_settings_body.pack_forget()
        self.source_settings_toggle.configure(text=("▼" if self.source_settings_expanded else "▶") + " Источник и настройки")

    def browse_local_video(self):
        path = filedialog.askopenfilename(parent=self.root, title="Выберите локальный видеофайл",
                                          filetypes=[("Видео", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.ts *.mts *.m2ts"),
                                                     ("Все файлы", "*.*")])
        if path:
            self.variables["local_video_path"].set(path)

    def read_settings(self):
        settings = {key: variable.get().strip() for key, variable in self.variables.items() if key != "analyze_full_video"}
        settings["analyze_full_video"] = self.variables["analyze_full_video"].get()
        settings["end_padding_sec"] = padding_seconds(settings["end_padding_sec"])
        analysis_bounds(settings)
        if not settings["analyze_full_video"]:
            for key in ("analysis_start_min", "analysis_end_min"):
                settings[key] = float(settings[key].replace(",", "."))
        for key in ("min_duration_sec", "max_duration_sec"):
            settings[key] = int(settings[key])
        if not 1 <= settings["min_duration_sec"] <= settings["max_duration_sec"] <= 480:
            raise ValueError("Укажите длительность: 1 ≤ минимум ≤ максимум ≤ 480 секунд. Обычно 120–300.")
        if settings["device"] not in ("cpu", "cuda"):
            raise ValueError("Устройство должно быть cpu или cuda.")
        if settings["local_video_path"]:
            settings["local_video_path"] = str(local_source_path(settings["local_video_path"]))
        elif not settings.get("youtube_url"):
            raise ValueError("Укажите YouTube-ссылку или путь к локальному видеофайлу.")
        if not all(settings[k] for k in ("asr_model", "llm_model", "ollama_url")):
            raise ValueError("Заполните названия моделей и адрес Ollama.")
        # Shorts настройки
        if self.shorts_enabled_var.get():
            settings["outputs"] = {
                "shorts": {
                    "enabled": True,
                    "aspect_ratio": "9:16",
                    "min_duration_sec": int(settings.get("shorts_min_duration_sec", 20)),
                    "target_duration_sec": int(settings.get("shorts_target_duration_sec", 40)),
                    "max_duration_sec": int(settings.get("shorts_max_duration_sec", 60)),
                    "crop_mode": "center",
                    "subtitles_enabled": True,
                    "max_candidates": 30,
                    "final_candidates": 10,
                }
            }
        return settings

    def refresh_models(self):
        base = self.variables["ollama_url"].get().strip()
        self.start_worker("Проверка моделей Ollama…", lambda: list_models(base))

    def analyze(self):
        try:
            settings = self.read_settings()
            save_json(DATA / "ui_settings.json", settings)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Настройки", str(exc), parent=self.root)
            return
        self.start_worker("Подготовка источника и смысловой анализ…", lambda: run_analysis(settings, self.log))

    def recalc_shorts(self):
        """Keep every existing in-range Short; discover additional candidates."""
        if self.busy:
            return
        if not self.context:
            messagebox.showinfo("Пересчитать Shorts", "Сначала откройте отчёт.", parent=self.root)
            return
        if self.has_unsaved_edits():
            messagebox.showinfo("Ревью", "Сначала сохраните правки выбранного кандидата.", parent=self.root)
            return
        if not self.shorts_enabled_var.get():
            messagebox.showerror("Shorts", "Включите Shorts 9:16.", parent=self.root)
            return
        try:
            minimum = int(self.variables["shorts_min_duration_sec"].get().strip())
            target = int(self.variables["shorts_target_duration_sec"].get().strip())
            maximum = int(self.variables["shorts_max_duration_sec"].get().strip())
            if not 1 <= minimum <= target <= maximum:
                raise ValueError("Нужно 1 ≤ минимум ≤ цель ≤ максимум.")
            settings = copy.deepcopy(self.context["report"]["settings"])
            shorts = copy.deepcopy(settings.get("outputs", {}).get("shorts", {}))
            shorts.update(enabled=True, min_duration_sec=minimum,
                          target_duration_sec=target, max_duration_sec=maximum)
            settings.setdefault("outputs", {})["shorts"] = shorts
            from job_store import normalize_profiles
            normalize_profiles(settings["outputs"])
        except (ValueError, TypeError, KeyError) as exc:
            messagebox.showerror("Настройки Shorts", str(exc), parent=self.root)
            return
        context = self.context
        self.start_worker("Пересчёт Shorts…",
                          lambda: recalc_shorts_for_report(context, settings, self.log))

    def load_report(self):
        path = filedialog.askopenfilename(title="Открыть report.json", initialdir=str(DATA if DATA.exists() else ROOT),
                                          filetypes=[("JSON", "*.json")])
        if path:
            self.start_worker("Загрузка отчёта…", lambda: open_report(path))

    def apply_end_padding(self):
        if self.busy:
            return
        if not self.context:
            messagebox.showinfo("Запас в конце", "Сначала получите или откройте отчёт кандидатов.", parent=self.root)
            return
        if self.has_unsaved_edits():
            messagebox.showinfo("Ревью", "Сначала сохраните правки выбранного кандидата.", parent=self.root)
            return
        try:
            seconds = padding_seconds(self.variables["end_padding_sec"].get())
        except ValueError as exc:
            messagebox.showerror("Запас в конце", str(exc), parent=self.root)
            return
        if any(c["status"] == "approved" for c in self.context["report"]["candidates"]):
            if not messagebox.askyesno("Повторное утверждение границ",
                                       "Если запас изменит утверждённые интервалы, потребуется утвердить их заново.\n"
                                       "Исходный отчёт сохранится. Продолжить?", parent=self.root):
                return
        source_path = self.context["report_path"]
        self.start_worker("Добавление запаса к концам клипов…",
                          lambda: open_report(prepare_review_report(source_path, seconds, self.log)))

    def current_candidate(self):
        selected = self.table.selection()
        if not selected or not self.context:
            return None
        return next((c for c in self.context["report"]["candidates"] if c["candidate_id"] == selected[0]), None)

    def table_action(self, event):
        row = self.table.identify_row(event.y)
        column = self.table.identify_column(event.x)
        if not row or column not in ("#8", "#9"):
            return
        if self.busy:
            return "break"
        if self.has_unsaved_edits():
            messagebox.showinfo("Ревью", "Сначала сохраните правки выбранного кандидата.", parent=self.root)
            return "break"
        self.table.selection_set(row)
        self.show_candidate()
        if column == "#8":
            self.toggle_candidate_padding()
        else:
            self.render(row)
        return "break"

    def toggle_candidate_padding(self):
        if self.busy or not self.context:
            return
        if self.has_unsaved_edits():
            messagebox.showinfo("Ревью", "Сначала сохраните правки выбранного кандидата.", parent=self.root)
            return
        current = self.current_candidate()
        if not current:
            return
        try:
            candidate = set_candidate_padding(
                current, not current.get("end_padding_enabled", True),
                self.variables["end_padding_sec"].get(),
                self.context["manifest"]["media"]["duration_sec"], self.context["report"]["settings"])
            updated = copy.deepcopy(self.context["report"])
            updated["candidates"] = [candidate if c["candidate_id"] == candidate["candidate_id"] else c
                                     for c in updated["candidates"]]
            refresh_padding_overlaps(updated)
            save_json(self.context["report_path"], updated)
            self.context["report"] = updated
            self.refresh_table(candidate["candidate_id"])
            self.show_candidate()
            if current["status"] == "approved" and candidate["status"] != "approved":
                self.status.set("Границы изменены. Утвердите клип заново перед нарезкой.")
        except (ValueError, OSError) as exc:
            messagebox.showerror("Запас в конце", str(exc), parent=self.root)

    def refresh_table(self, selected=None):
        self.table.delete(*self.table.get_children())
        for candidate in self.context["report"]["candidates"]:
            self.table.insert("", "end", iid=candidate["candidate_id"], values=(
                candidate["candidate_id"], STATUS.get(candidate["status"], candidate["status"]), timecode(candidate["start"]), timecode(candidate["end"]),
                round(candidate["end"] - candidate["start"], 2), candidate["score"], candidate["title"],
                "☑" if candidate.get("end_padding_enabled", True) else "☐", "▶ Нарезать"))
        children = self.table.get_children()
        if children:
            self.table.selection_set(selected if selected in children else children[0])
        else:
            self.start.set("")
            self.end.set("")
            self.title.set("")
            self.set_details("Подходящих кандидатов не найдено. Произвольные интервалы не создаются.")
        # Обновляем также таблицу Shorts
        self.refresh_shorts_table(None)

    def set_details(self, text, chunks=None):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("end", text)
        for tag, content in chunks or []:
            self.details.insert("end", content, (tag,))
        self.details.configure(state="disabled")

    def _transcript_draft_key(self, candidate):
        return (id(self.context), self._is_shorts_tab(), candidate["candidate_id"])

    def _candidate_with_text_edits(self, candidate):
        result = dict(candidate)
        drafts = getattr(self, "_transcript_drafts", {})
        edits = candidate.get("transcript_edits", [])
        if drafts:
            edits = drafts.get(self._transcript_draft_key(candidate), edits)
        if edits:
            result["transcript_edits"] = copy.deepcopy(edits)
        else:
            result.pop("transcript_edits", None)
        return result

    def _text_edits_unsaved(self, candidate):
        return self._candidate_with_text_edits(candidate).get("transcript_edits", []) != candidate.get("transcript_edits", [])

    def edit_transcript_sentence(self, event):
        if self.busy or not self.context:
            return "break"
        candidate = self.current_shorts_candidate() if self._is_shorts_tab() else self.current_candidate()
        if not candidate:
            return "break"
        index = self.details.index(f"@{event.x},{event.y}")
        line = self.details.get(f"{index} linestart", f"{index} lineend")
        match = re.match(r"\[(\d+:\d+:\d+\.\d+)–(\d+:\d+:\d+\.\d+)\]", line)
        if not match:
            return "break"
        start, end = (parse_time(value) for value in match.groups())
        source = self.context["transcript"]
        sentences = source.get("sentences", [])
        matches = [(i, s) for i, s in enumerate(sentences)
                   if s["start"] < end and s["end"] > start]
        if not matches:
            return "break"
        sentence_index, sentence = max(matches, key=lambda item: min(end, item[1]["end"]) - max(start, item[1]["start"]))
        edited = self._candidate_with_text_edits(candidate)
        current = apply_transcript_edits(source, edited.get("transcript_edits"))
        dialog = tk.Toplevel(self.root)
        dialog.title("Исправить реплику")
        dialog.transient(self.root)
        ttk.Label(dialog, text=f"{timecode(sentence['start'])} — {timecode(sentence['end'])}\n"
                  "Редактируется полная реплика, включая контекст за границами клипа.").pack(padx=12, pady=8)
        text = tk.Text(dialog, width=80, height=6, wrap="word", undo=True)
        text.pack(fill="both", expand=True, padx=12, pady=4)
        text.insert("1.0", current["sentences"][sentence_index]["text"])
        menu = tk.Menu(text, tearoff=False)
        for label, virtual in (("Вырезать", "<<Cut>>"), ("Копировать", "<<Copy>>"), ("Вставить", "<<Paste>>")):
            menu.add_command(label=label, command=lambda v=virtual: text.event_generate(v))
        def popup(e):
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                menu.grab_release()
            return "break"
        text.bind("<Button-3>", popup)
        def select_all(_):
            text.tag_add("sel", "1.0", "end-1c")
            return "break"
        def shortcuts(e):
            if e.state & 0x4 and not e.state & (0x8 | 0x20000):
                if e.keycode == 65:
                    return select_all(e)
                virtual = {67: "<<Copy>>", 86: "<<Paste>>", 88: "<<Cut>>"}.get(e.keycode)
                if virtual:
                    text.event_generate(virtual)
                    return "break"
        text.bind("<Control-KeyPress>", shortcuts)
        def accept():
            value = " ".join(text.get("1.0", "end-1c").split())
            if not value:
                messagebox.showerror("Реплика", "Текст реплики не может быть пустым.", parent=dialog)
                return
            edits = [e for e in edited.get("transcript_edits", []) if e["sentence_index"] != sentence_index]
            if value != " ".join(sentence["text"].split()):
                edits.append({"sentence_index": sentence_index, "start": sentence["start"],
                              "end": sentence["end"], "text": value})
            if not hasattr(self, "_transcript_drafts"):
                self._transcript_drafts = {}
            self._transcript_drafts[self._transcript_draft_key(candidate)] = sorted(edits, key=lambda e: e["sentence_index"])
            dialog.destroy()
            self.status.set("Текст изменён. Нажмите «Сохранить правки / на доработку» или «Утвердить интервал».")
            if self._is_shorts_tab():
                self._update_shorts_candidate_details(preserve_scroll=True)
            else:
                self._update_candidate_details(preserve_scroll=True)
        actions = ttk.Frame(dialog)
        actions.pack(pady=8)
        ttk.Button(actions, text="Внести в правки", command=accept).pack(side="left", padx=6)
        ttk.Button(actions, text="Отмена", command=dialog.destroy).pack(side="left", padx=6)
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        dialog.grab_set()
        text.focus_set()
        return "break"

    def show_candidate(self):
        # Если активна вкладка Shorts, переключаемся на Shorts
        if self._is_shorts_tab():
            self.show_shorts_candidate()
            return
        candidate = self.current_candidate()
        if not candidate:
            return
        self._loading_candidate = True
        try:
            self.start.set(timecode(candidate["start"]))
            self.end.set(timecode(candidate["end"]))
            self.title.set(candidate["title"])
        finally:
            self._loading_candidate = False
        self._update_candidate_details()

    def _review_bounds_changed(self, *_):
        if not self._loading_candidate:
            self._update_candidate_details(preserve_scroll=True)

    def _update_candidate_details(self, preserve_scroll=False):
        current = self.current_candidate()
        if not current:
            return
        try:
            start, end = validate_interval(parse_time(self.start.get()), parse_time(self.end.get()),
                                           self.context["manifest"]["media"]["duration_sec"])
        except (ValueError, OverflowError):
            self.set_details("Границы редактируются. Введите корректные начало и конец — текст и контекст обновятся автоматически. "
                             "Изменения ещё не сохранены.")
            return
        candidate = dict(current, start=start, end=end)
        draft_bounds = abs(start - current["start"]) > 0.000001 or abs(end - current["end"]) > 0.000001
        if draft_bounds:
            candidate["manual_bounds"] = True
        candidate = self._candidate_with_text_edits(candidate)
        sections = review_sections(apply_transcript_edits(self.context["transcript"], candidate.get("transcript_edits")), start, end)
        old_scroll = self.details.yview()[0] if preserve_scroll else 0
        notes = self.context.get("quality_notes", {}).get(candidate["candidate_id"], {})
        padding = candidate.get("end_padding")
        semantic_end = candidate["end"]
        if padding and abs(candidate["end"] - padding["padded_end"]) < 0.000001:
            semantic_end = padding["base_end"]
            padding_text = (f"Конец до запаса: {timecode(semantic_end)}; "
                             f"добавлен запас {padding['applied_sec']:g} с (запрошен {padding['requested_sec']:g} с).\n")
            if not candidate.get("end_padding_enabled", True):
                padding_text = "Запас для этого клипа отключён.\n"
        elif padding:
            padding_text = "Конец изменён вручную; автоматический запас повторно не добавляется.\n"
        else:
            padding_text = "Запас к этому отчёту ещё не применён — доступна кнопка «Применить запас к отчёту».\n"
        qa = ""
        if notes and abs(notes["start"] - candidate["start"]) < 0.001 and abs(notes["end"] - semantic_end) < 0.001:
            qa = "\n\nЗАМЕЧАНИЯ ТЕСТОВОГО ПРОГОНА К ЭТИМ ГРАНИЦАМ:\n" + "\n".join(notes["notes"])
        output_dir = (Path(self.context["job_dir"]) / "output" / self.context["report"]["signature"]
                      if "job_dir" in self.context and "signature" in self.context["report"] else None)
        catalog_id = candidate.get("selection_interval_id") or candidate.get("interval_id")
        catalog_text = f"Выбор PASS 2 (interval_id): {catalog_id}.\n" if catalog_id else ""
        if catalog_id and candidate.get("manual_bounds"):
            catalog_text += "Границы после выбора были изменены вручную.\n"
        draft_text = "ПРЕДПРОСМОТР: правки ещё не сохранены.\n" if draft_bounds or self._text_edits_unsaved(current) else ""
        self.set_details(f"ID: {candidate['candidate_id']}\nИмя следующего файла: {output_filename(candidate, output_dir)}\n"
                         + draft_text + catalog_text + padding_text + "\n"
                         f"Почему выбран: {candidate['reason']}\n"
                         + "\n".join(candidate.get("warnings", [])) + qa + "\n\n",
                         chunks=transcript_chunks(sections))
        self.details.yview_moveto(old_scroll)

    def edited_candidate(self):
        current = self.current_candidate()
        if not current:
            raise ValueError("Выберите кандидата.")
        settings = self.context["report"]["settings"]
        start, end = validate_interval(parse_time(self.start.get()), parse_time(self.end.get()),
                                       self.context["manifest"]["media"]["duration_sec"],
                                       settings["min_duration_sec"], settings["max_duration_sec"])
        candidate = dict(current, start=start, end=end, title=self.title.get().strip())
        if "duration_sec" in candidate:
            candidate["duration_sec"] = end - start
        if abs(start - current["start"]) > 0.000001 or abs(end - current["end"]) > 0.000001:
            candidate["manual_bounds"] = True
        if not candidate["title"]:
            raise ValueError("Введите заголовок.")
        return self._candidate_with_text_edits(candidate)

    def decide(self, status):
        if self.busy:
            return
        # Если активна вкладка Shorts, используем Shorts-логику
        if self._is_shorts_tab():
            self.decide_shorts(status)
            return
        try:
            candidate = self.edited_candidate()
            candidate["status"] = status
            candidate.pop("approval_key", None)
            if status == "approved":
                candidate["approval_key"] = approval_key(candidate)
            updated = copy.deepcopy(self.context["report"])
            updated["candidates"] = [candidate if c["candidate_id"] == candidate["candidate_id"] else c
                                      for c in updated["candidates"]]
            save_json(self.context["report_path"], updated)
            self.context["report"] = updated
            if getattr(self, "_transcript_drafts", {}):
                self._transcript_drafts.pop(self._transcript_draft_key(candidate), None)
            self.refresh_table(candidate["candidate_id"])
        except (ValueError, OSError) as exc:
            messagebox.showerror("Ревью", str(exc), parent=self.root)

    def has_unsaved_edits(self):
        # Если активна вкладка Shorts, проверяем Shorts-кандидата
        if self._is_shorts_tab():
            candidate = self.current_shorts_candidate()
            if candidate is None:
                return False
            return (self.start.get() != timecode(candidate["start"]) or self.end.get() != timecode(candidate["end"])
                    or self.title.get().strip() != candidate["title"] or self._text_edits_unsaved(candidate))
        candidate = self.current_candidate()
        if candidate is None:
            return False
        return (self.start.get() != timecode(candidate["start"]) or self.end.get() != timecode(candidate["end"])
                or self.title.get().strip() != candidate["title"] or self._text_edits_unsaved(candidate))

    def render(self, candidate_id=None):
        # Рендер работает только для хайлайтов
        if self.busy:
            return
        if not self.context:
            messagebox.showinfo("Нарезка", "Сначала получите или откройте отчёт кандидатов.", parent=self.root)
            return
        if self.has_unsaved_edits():
            messagebox.showinfo("Ревью", "Сохраните и при необходимости утвердите правки выбранного кандидата.", parent=self.root)
            return
        report = copy.deepcopy(self.context["report"])
        if candidate_id is not None:
            report["candidates"] = [c for c in report["candidates"] if c["candidate_id"] == candidate_id]
        approved = [c for c in report["candidates"] if c["status"] == "approved"]
        if not approved:
            messagebox.showinfo("Нарезка", "Сначала утвердите этот интервал." if candidate_id is not None
                                else "Сначала утвердите хотя бы один интервал.", parent=self.root)
            return
        output_dir = Path(self.context["job_dir"]) / "output" / report["signature"]
        intervals = "\n".join(f"{c['candidate_id']}: {timecode(c['start'])} — {timecode(c['end'])}\n"
                              f"{output_filename(c, output_dir)}" for c in approved)
        if not messagebox.askyesno("Разрешение на финальную нарезку",
                                   f"Нарезать утверждённые интервалы ({len(approved)} шт.)?\n{intervals}\n\n"
                                   "Будет сохранён полный горизонтальный кадр без кропа.", parent=self.root):
            return
        manifest = copy.deepcopy(self.context["manifest"])
        job_dir = self.context["job_dir"]
        transcript = copy.deepcopy(self.context["transcript"])
        self.start_worker("Финальная нарезка утверждённых интервалов…",
                          lambda: render_approved(manifest, report, job_dir, self.log, transcript=transcript))

    def render_shorts(self, candidate_id=None):
        """Рендер утверждённых Shorts 9:16."""
        if self.busy:
            return
        if not self.context:
            messagebox.showinfo("Рендер Shorts", "Сначала откройте отчёт.", parent=self.root)
            return
        if self.has_unsaved_edits():
            messagebox.showinfo("Ревью", "Сохраните правки выбранного кандидата.", parent=self.root)
            return
        report = copy.deepcopy(self.context["report"])
        shorts = report.get("shorts_candidates", [])
        if candidate_id is not None:
            shorts = [c for c in shorts if c["candidate_id"] == candidate_id]
        approved = [c for c in shorts if c["status"] == "approved"]
        if not approved:
            messagebox.showinfo("Рендер Shorts", "Сначала утвердите этот Short.", parent=self.root)
            return
        manifest = copy.deepcopy(self.context["manifest"])
        job_dir = self.context["job_dir"]
        transcript = copy.deepcopy(self.context["transcript"])
        subtitle_settings = self.subtitle_settings
        export_settings = self.shorts_export_settings
        self.start_worker(
            f"Рендер Shorts ({len(approved)} шт.)…",
            lambda: render_shorts_approved(manifest, report, job_dir, self.log,
                                            transcript=transcript, candidate_id=candidate_id,
                                            subtitle_settings=subtitle_settings, export_settings=export_settings))

    def open_quality_window(self):
        if self.quality_window is not None and not self.quality_window.closed:
            self.quality_window.top.deiconify()
            self.quality_window.top.lift()
            self.quality_window.top.focus_set()
            return
        url = self.variables["youtube_url"].get().strip()
        if not url:
            messagebox.showinfo("Качество YouTube", "Сначала вставьте YouTube-ссылку.", parent=self.root)
            return
        video_id = video_id_from_url(url)
        manifest = None
        if self.context and self.context["manifest"].get("video_id") == video_id:
            manifest = self.context["manifest"]
        try:
            from quality_window import QualityDialog
            self.quality_window = QualityDialog(self.root, url, self._on_quality_done,
                                                root=DATA, manifest=manifest,
                                                on_change=self._apply_quality_manifest)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Качество YouTube", str(exc), parent=self.root)

    def _apply_quality_manifest(self, height, manifest):
        if (self.context and manifest.get("source_path")
                and self.context["manifest"].get("video_id") == manifest.get("video_id")
                and self.context["manifest"].get("source_type") != "local"):
            self.context["manifest"] = copy.deepcopy(manifest)

    def _on_quality_done(self, height, manifest):
        self._apply_quality_manifest(height, manifest)
        self.quality_window = None

    def open_shorts_settings(self):
        if self.shorts_settings_window is not None and not self.shorts_settings_window.closed:
            self.shorts_settings_window.top.deiconify()
            self.shorts_settings_window.top.lift()
            self.shorts_settings_window.top.focus_set()
            return
        from shorts_settings_window import ShortsSettingsWindow
        self.shorts_settings_window = ShortsSettingsWindow(self.root, self.shorts_export_settings,
                                                          self.apply_shorts_settings)

    def apply_shorts_settings(self, settings):
        save_shorts_settings(DATA / "shorts_render_settings.json", settings)
        self.shorts_export_settings = settings

    def open_subs_settings(self):
        if self.subs_window is not None and not self.subs_window.closed:
            self.subs_window.top.deiconify()
            self.subs_window.top.lift()
            self.subs_window.top.focus_set()
            return
        from subs_settings_window import SubsSettingsWindow
        self.subs_window = SubsSettingsWindow(self.root, self.subtitle_settings, self.apply_subtitle_settings)

    def apply_subtitle_settings(self, settings):
        save_subtitle_settings(DATA / "subtitle_settings.json", settings)
        self.subtitle_settings = settings

    def preview(self):
        try:
            # Если активна вкладка Shorts, используем Shorts-кандидата
            if self._is_shorts_tab():
                candidate = self.edited_shorts_candidate()
            else:
                candidate = self.edited_candidate()
            if not shutil.which("ffplay"):
                raise ValueError("Для просмотра интервала нужен ffplay в PATH (входит в полную сборку FFmpeg).")
            subprocess.Popen(["ffplay", "-autoexit", "-loglevel", "error", "-ss", str(candidate["start"]),
                              "-t", str(candidate["end"] - candidate["start"]),
                              "-i", self.context["manifest"]["source_path"]],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Предпросмотр", str(exc), parent=self.root)

    def _close(self):
        if self.quality_window is not None and not self.quality_window.closed and self.quality_window.busy:
            messagebox.showinfo("Загрузка качества", "Дождитесь завершения операции в окне качества.", parent=self.root)
            return
        if self.busy:
            messagebox.showinfo("Выполняется обработка", "Дождитесь завершения текущей операции. Промежуточные этапы сохраняются на диск.", parent=self.root)
            return
        if self.has_unsaved_edits() and not messagebox.askyesno("Несохранённые правки", "Закрыть окно без сохранения правок?", parent=self.root):
            return
        self.root.destroy()

    # --- Shorts 9:16 ---

    def _is_shorts_tab(self):
        """Возвращает True, если активна вкладка Shorts."""
        return self.notebook.index(self.notebook.select()) == 1

    def _is_highlights_tab(self):
        """Возвращает True, если активна вкладка Хайлайты."""
        return self.notebook.index(self.notebook.select()) == 0

    def _sync_active_tab_panel(self):
        """При переключении вкладки синхронизирует панель «Ревью» с уже выбранным кандидатом."""
        if self._is_shorts_tab():
            self.show_shorts_candidate()
        else:
            self.show_candidate()

    def current_shorts_candidate(self):
        """Возвращает текущего выбранного Shorts-кандидата."""
        selected = self.shorts_table.selection()
        if not selected or not self.context:
            return None
        return next((c for c in self.context["report"].get("shorts_candidates", []) if c["candidate_id"] == selected[0]), None)

    def refresh_shorts_table(self, selected=None):
        """Обновляет таблицу Shorts-кандидатов."""
        previous_selection = self.shorts_table.selection()
        self.shorts_table.delete(*self.shorts_table.get_children())
        if not self.context:
            return
        for candidate in self.context["report"].get("shorts_candidates", []):
            self.shorts_table.insert("", "end", iid=candidate["candidate_id"], values=(
                candidate["candidate_id"], STATUS.get(candidate["status"], candidate["status"]),
                timecode(candidate["start"]), timecode(candidate["end"]),
                round(candidate["end"] - candidate["start"], 2), candidate["score"], candidate["title"],
                "▶ Рендер*"))
        children = self.shorts_table.get_children()
        if children:
            if selected is None and previous_selection:
                selected = previous_selection[0] if previous_selection[0] in children else children[0]
            else:
                selected = selected if selected in children else children[0]
            self.shorts_table.selection_set(selected)
        else:
            self.start.set("")
            self.end.set("")
            self.title.set("")
            self.set_details("Shorts-кандидаты не найдены. Проверьте, что Shorts включены в настройках.")
        # Добавляем поясняющий текст
        self.shorts_table.bind("<Button-1>", self.shorts_table_action)

    def show_shorts_candidate(self):
        """Показывает детали выбранного Shorts-кандидата."""
        if not self._is_shorts_tab():
            return
        candidate = self.current_shorts_candidate()
        if not candidate:
            return
        self._loading_candidate = True
        try:
            self.start.set(timecode(candidate["start"]))
            self.end.set(timecode(candidate["end"]))
            self.title.set(candidate["title"])
        finally:
            self._loading_candidate = False
        self._update_shorts_candidate_details()

    def _update_shorts_candidate_details(self, preserve_scroll=False):
        """Обновляет детали для Shorts-кандидата."""
        current = self.current_shorts_candidate()
        if not current:
            return
        try:
            start, end = validate_interval(parse_time(self.start.get()), parse_time(self.end.get()),
                                           self.context["manifest"]["media"]["duration_sec"])
        except (ValueError, OverflowError):
            self.set_details("Границы редактируются. Введите корректные начало и конец.")
            return
        candidate = dict(current, start=start, end=end)
        draft_bounds = abs(start - current["start"]) > 0.000001 or abs(end - current["end"]) > 0.000001
        if draft_bounds:
            candidate["manual_bounds"] = True
        candidate = self._candidate_with_text_edits(candidate)
        sections = review_sections(apply_transcript_edits(self.context["transcript"], candidate.get("transcript_edits")), start, end)
        notes = self.context.get("quality_notes", {}).get(candidate["candidate_id"], {})
        qa = ""
        if notes and abs(notes["start"] - candidate["start"]) < 0.001 and abs(notes["end"] - candidate["end"]) < 0.001:
            qa = "\n\nЗАМЕЧАНИЯ:\n" + "\n".join(notes["notes"])
        draft_text = "ПРЕДПРОСМОТР: правки ещё не сохранены.\n" if draft_bounds or self._text_edits_unsaved(current) else ""
        self.set_details(f"ID: {candidate['candidate_id']}\n"
                         f"Формат: 9:16 Shorts\n"
                         f"{draft_text}"
                         f"Почему выбран: {candidate['reason']}\n"
                         + "\n".join(candidate.get("warnings", [])) + qa + "\n\n",
                         chunks=transcript_chunks(sections))

    def edited_shorts_candidate(self):
        """Возвращает отредактированного Shorts-кандидата."""
        current = self.current_shorts_candidate()
        if not current:
            raise ValueError("Выберите Shorts-кандидата.")
        profiles = self.context["report"].get("profiles", {})
        shorts_profile = profiles.get("shorts", {})
        start, end = validate_interval(parse_time(self.start.get()), parse_time(self.end.get()),
                                       self.context["manifest"]["media"]["duration_sec"],
                                       shorts_profile.get("min_duration_sec", 20),
                                       shorts_profile.get("max_duration_sec", 60))
        candidate = dict(current, start=start, end=end, title=self.title.get().strip())
        if "duration_sec" in candidate:
            candidate["duration_sec"] = end - start
        if abs(start - current["start"]) > 0.000001 or abs(end - current["end"]) > 0.000001:
            candidate["manual_bounds"] = True
        if not candidate["title"]:
            raise ValueError("Введите заголовок.")
        return self._candidate_with_text_edits(candidate)

    def decide_shorts(self, status):
        """Сохраняет решение для Shorts-кандидата."""
        if self.busy:
            return
        try:
            candidate = self.edited_shorts_candidate()
            candidate["status"] = status
            candidate.pop("approval_key", None)
            if status == "approved":
                candidate["approval_key"] = shorts_approval_key(candidate)
            updated = copy.deepcopy(self.context["report"])
            updated["shorts_candidates"] = [
                candidate if c["candidate_id"] == candidate["candidate_id"] else c
                for c in updated.get("shorts_candidates", [])
            ]
            save_json(self.context["report_path"], updated)
            self.context["report"] = updated
            if getattr(self, "_transcript_drafts", {}):
                self._transcript_drafts.pop(self._transcript_draft_key(candidate), None)
            self.refresh_shorts_table(candidate["candidate_id"])
        except (ValueError, OSError) as exc:
            messagebox.showerror("Ревью Shorts", str(exc), parent=self.root)

    def shorts_table_action(self, event):
        """Обработчик клика по таблице Shorts."""
        row = self.shorts_table.identify_row(event.y)
        column = self.shorts_table.identify_column(event.x)
        if not row:
            return
        if self.busy:
            return "break"
        if self.has_unsaved_edits():
            messagebox.showinfo("Ревью Shorts", "Сначала сохраните правки выбранного кандидата.", parent=self.root)
            return "break"
        self.shorts_table.selection_set(row)
        self.show_shorts_candidate()
        if column == "#8":  # Колонка "Рендер"
            self.render_shorts(candidate_id=row)
        return "break"

    def open_smart_crop(self):
        """Открывает окно умного кропа для выбранного Shorts-кандидата."""
        if self.busy:
            return
        if not self.context:
            messagebox.showinfo("Смарт-кроп", "Сначала откройте отчёт.", parent=self.root)
            return
        candidate = self.current_shorts_candidate()
        if not candidate:
            messagebox.showinfo("Смарт-кроп", "Выберите Shorts-кандидата во вкладке Shorts.", parent=self.root)
            return
        # Предотвращаем повторное открытие окна для того же кандидата.
        for window in self.root.winfo_children():
            if isinstance(window, tk.Toplevel) and getattr(window, "smart_crop_candidate_id", None) == candidate["candidate_id"] and window.winfo_exists():
                messagebox.showinfo("Смарт-кроп", "Для этого Shorts окно умного кропа уже открыто.", parent=self.root)
                window.deiconify()
                window.lift()
                return
        try:
            source_path = Path(self.context["manifest"]["source_path"])
            if not source_path.exists():
                raise FileNotFoundError(f"Не найден исходник: {source_path}")
            media = self.context["manifest"]["media"]
            job_dir = Path(self.context["job_dir"])
            signature = self.context["report"].get("signature") or self.context["report_path"].parent.name
            path = settings_path(job_dir, signature, candidate["candidate_id"])
            if path.exists():
                settings, _warnings = load_settings(path)
                if (settings["candidate_id"] != candidate["candidate_id"]
                        or abs(settings["source_start"] - float(candidate["start"])) > 0.001
                        or abs(settings["source_end"] - float(candidate["end"])) > 0.001
                        or settings["source_width"] != media["width"]
                        or settings["source_height"] != media["height"]):
                    if not messagebox.askyesno(
                            "Смарт-кроп — изменились границы",
                            f"Сохранённый Smart Crop для {candidate['candidate_id']} относится "
                            "к другому интервалу или разрешению.\n\n"
                            "Создать новую настройку центрального кадра для текущего Short? "
                            "Старый файл будет заменён только после закрытия редактора с сохранением.",
                            parent=self.root):
                        return
                    settings = create_settings(candidate["candidate_id"], candidate["start"], candidate["end"],
                                               media["width"], media["height"])
            else:
                settings = create_settings(candidate["candidate_id"], candidate["start"], candidate["end"],
                                           media["width"], media["height"])
            start = float(candidate["start"])
            end = float(candidate["end"])
            top = tk.Toplevel(self.root)
            top.title(f"Смарт-кроп — {candidate['candidate_id']}")
            top.geometry("1180x760")
            top.transient(self.root)
            frame = ttk.Frame(top, padding=6)
            frame.pack(fill="both", expand=True)
            from smart_crop_window import SmartCropWindow
            editor = SmartCropWindow(frame, source_path, start, end,
                                     candidate["candidate_id"], settings, own_toplevel=False)
            editor.pack(fill="both", expand=True)
            top.smart_crop_candidate_id = candidate["candidate_id"]
            top.smart_crop_settings = settings
            top.smart_crop_frame = frame
            top.smart_crop_editor = editor
            top.smart_crop_save_path = path
            top.protocol("WM_DELETE_WINDOW", lambda w=top, p=path, e=editor: self._close_smart_crop(w, p, e))
        except Exception as exc:
            for w in self.root.winfo_children():
                if isinstance(w, tk.Toplevel) and not w.winfo_exists():
                    w.destroy()
            messagebox.showerror("Смарт-кроп", f"Не удалось открыть окно умного кропа:\n{exc}", parent=self.root)

    def _close_smart_crop(self, window, path, editor):
        """Сохранить settings, остановить декодер и уничтожить Toplevel."""
        try:
            # Не сохраняем настройки для устаревших границ, если отчёт изменился
            # пока редактор был открыт.
            current = next((c for c in self.context["report"].get("shorts_candidates", [])
                            if c["candidate_id"] == editor.settings["candidate_id"]), None)
            if (current is None
                    or abs(float(current["start"]) - editor.settings["source_start"]) > 0.001
                    or abs(float(current["end"]) - editor.settings["source_end"]) > 0.001):
                raise ValueError("Границы Short изменились после открытия редактора. "
                                 "Закройте окно без сохранения и откройте Smart Crop заново.")
            save_settings(editor.settings, path)
        except Exception as exc:
            messagebox.showerror("Смарт-кроп", f"Не удалось сохранить Smart Crop:\n{exc}", parent=self.root)
            return
        editor.close()
        try:
            if window.winfo_exists():
                window.destroy()
        except Exception:
            pass

def main(report_path=None):
    root = tk.Tk()
    app = HighlightApp(root)
    if report_path:
        root.after(150, lambda: app.start_worker("Загрузка отчёта…", lambda: open_report(report_path)))
    root.mainloop()
