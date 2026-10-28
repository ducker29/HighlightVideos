"""Tkinter desktop UI. Workers communicate through a queue, never through widgets."""
import copy
import queue
import shutil
import subprocess
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from analyzer import list_models
from app_backend import open_report, run_analysis
from job_store import DATA, ROOT, analysis_bounds, load_json, parse_time, save_json, timecode, validate_interval
from renderer import approval_key, output_filename, render_approved
from clip_padding import padding_seconds, prepare_review_report

STATUS = {"pending": "На проверке", "approved": "Утверждён", "rejected": "Отклонён"}


class HighlightApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Highlight Pipeline — горизонтальные хайлайты 16:9")
        self.root.geometry("1250x900")
        self.root.minsize(1000, 700)
        self.events = queue.Queue()
        self.busy = False
        self.context = None
        self.controls = []
        self.settings = load_json(ROOT / "config.json")
        saved = DATA / "ui_settings.json"
        if saved.exists():
            self.settings.update(load_json(saved))
        self.variables = {key: tk.StringVar(value=str(value)) for key, value in self.settings.items()}
        self.variables["analyze_full_video"] = tk.BooleanVar(value=self.settings.get("analyze_full_video", True))
        self.status = tk.StringVar(value="Готово. Укажите ссылку и длительность, затем запустите анализ.")
        self._build()
        self.variables["analyze_full_video"].trace_add("write", lambda *_: self.sync_analysis_range())
        self.sync_analysis_range()
        self.root.after(100, self._poll)
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
        self.controls.append(widget)
        return widget

    def _build(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        settings = ttk.LabelFrame(outer, text="Источник и настройки", padding=8)
        settings.pack(fill="x")
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="YouTube VOD:").grid(row=0, column=0, sticky="w")
        self.entry(settings, "youtube_url", 0, 1, 65).grid(columnspan=5)
        ttk.Label(settings, text="Длительность, секунды: от").grid(row=1, column=0, sticky="w")
        self.entry(settings, "min_duration_sec", 1, 1)
        ttk.Label(settings, text="до").grid(row=1, column=2)
        self.entry(settings, "max_duration_sec", 1, 3)
        ttk.Label(settings, text="Запас в конце, с:").grid(row=1, column=4)
        self.entry(settings, "end_padding_sec", 1, 5, 6, values=("0", "0.5", "1"))
        ttk.Label(settings, text="Whisper:").grid(row=2, column=0, sticky="w")
        self.entry(settings, "asr_model", 2, 1, values=("large-v3", "medium", "small"))
        ttk.Label(settings, text="Устройство:").grid(row=2, column=2)
        self.entry(settings, "device", 2, 3, values=("cuda", "cpu"))
        ttk.Label(settings, text="Язык:").grid(row=2, column=4)
        self.entry(settings, "language", 2, 5, 6)
        ttk.Label(settings, text="Ollama URL:").grid(row=3, column=0, sticky="w")
        self.entry(settings, "ollama_url", 3, 1, 30)
        ttk.Label(settings, text="LLM:").grid(row=3, column=2)
        self.model_choice = self.entry(settings, "llm_model", 3, 3, 32, values=(self.settings["llm_model"],))
        self.button(settings, "Обновить модели", self.refresh_models, row=3, column=4, columnspan=2, padx=5)
        ttk.Label(settings, text="LLM-редактор:").grid(row=4, column=0, sticky="w")
        self.editorial_choice = self.entry(settings, "editorial_model", 4, 1, 65,
                                           values=(self.settings["editorial_model"],))
        self.editorial_choice.grid(columnspan=5)

        scope = ttk.Frame(settings)
        scope.grid(row=5, column=0, columnspan=6, sticky="ew", pady=4)
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
        self.button(actions, "1. Скачать и найти кандидатов", self.analyze, row=0, column=0, padx=(0, 8))
        self.button(actions, "Открыть сохранённый отчёт…", self.load_report, row=0, column=1, padx=8)
        self.button(actions, "2. Нарезать утверждённые…", self.render, row=0, column=2, padx=8)
        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=150)
        self.progress.grid(row=0, column=3, padx=8)
        self.button(actions, "Применить запас к отчёту…", self.apply_end_padding,
                    row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(outer, textvariable=self.status, wraplength=1180).pack(fill="x", pady=(0, 5))

        panes = ttk.Panedwindow(outer, orient="vertical")
        panes.pack(fill="both", expand=True)
        table_frame = ttk.Frame(panes)
        columns = ("candidate_id", "status", "start", "end", "duration", "score", "title")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", height=9, selectmode="browse")
        for key, label, width in zip(columns, ("ID", "Решение", "Начало", "Конец", "Длина, с", "Оценка", "Тема / заголовок"),
                                     (75, 120, 115, 115, 85, 70, 405)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, stretch=(key == "title"))
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.bind("<<TreeviewSelect>>", lambda event: self.show_candidate())
        panes.add(table_frame, weight=2)

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
            self.controls.append(widget)
        fields.columnconfigure(5, weight=1)
        decisions = ttk.Frame(review)
        decisions.pack(fill="x", pady=5)
        self.button(decisions, "Сохранить правки / на доработку", lambda: self.decide("pending"), row=0, column=0, padx=3)
        self.button(decisions, "Утвердить интервал", lambda: self.decide("approved"), row=0, column=1, padx=3)
        self.button(decisions, "Отклонить", lambda: self.decide("rejected"), row=0, column=2, padx=3)
        self.button(decisions, "Посмотреть интервал (FFplay)", self.preview, row=0, column=3, padx=3)
        ttk.Label(review, text="Правки сохраняются кнопками выше. Изменённые границы требуют нового утверждения.").pack(anchor="w")
        self.details = tk.Text(review, height=9, wrap="word", state="disabled")
        self.details.pack(fill="both", expand=True, pady=4)
        panes.add(review, weight=3)
        log_frame = ttk.LabelFrame(panes, text="Ход выполнения", padding=4)
        self.logs = tk.Text(log_frame, height=7, wrap="word", state="disabled")
        self.logs.pack(fill="both", expand=True)
        panes.add(log_frame, weight=1)

    def log(self, message):
        self.events.put(("log", str(message)))

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
                        self.status.set(f"{result['manifest']['title']} — кандидатов: {len(result['report']['candidates'])}.{scope} Ожидается ревью.")
                        if result["report"].get("analysis_complete") is False:
                            self.status.set(self.status.get() + " Есть блоки с неполным отбором — см. журнал.")
                    else:
                        if self.context and not self.has_unsaved_edits():
                            self.show_candidate()
                        self.status.set(f"Готово. Создано клипов: {len(result)}.")
                        messagebox.showinfo("Рендер завершён", "\n".join(result), parent=self.root)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def append_log(self, message):
        self.logs.configure(state="normal")
        self.logs.insert("end", message + "\n")
        self.logs.see("end")
        self.logs.configure(state="disabled")

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
        if not all(settings[k] for k in ("youtube_url", "asr_model", "llm_model", "ollama_url")):
            raise ValueError("Заполните ссылку и названия моделей.")
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
        self.start_worker("Скачивание и смысловой анализ…", lambda: run_analysis(settings, self.log))

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

    def refresh_table(self, selected=None):
        self.table.delete(*self.table.get_children())
        for candidate in self.context["report"]["candidates"]:
            self.table.insert("", "end", iid=candidate["candidate_id"], values=(
                candidate["candidate_id"], STATUS.get(candidate["status"], candidate["status"]), timecode(candidate["start"]), timecode(candidate["end"]),
                round(candidate["end"] - candidate["start"], 2), candidate["score"], candidate["title"]))
        children = self.table.get_children()
        if children:
            self.table.selection_set(selected if selected in children else children[0])
        else:
            self.start.set("")
            self.end.set("")
            self.title.set("")
            self.set_details("Подходящих кандидатов не найдено. Произвольные интервалы не создаются.")

    def set_details(self, text):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("end", text)
        self.details.configure(state="disabled")

    def show_candidate(self):
        candidate = self.current_candidate()
        if not candidate:
            return
        self.start.set(timecode(candidate["start"]))
        self.end.set(timecode(candidate["end"]))
        self.title.set(candidate["title"])
        sentences = self.context["transcript"]["sentences"]
        text = "\n".join(f"[{timecode(s['start'])}] {s['text']}" for s in sentences
                         if s["end"] > candidate["start"] and s["start"] < candidate["end"])
        notes = self.context.get("quality_notes", {}).get(candidate["candidate_id"], {})
        padding = candidate.get("end_padding")
        semantic_end = candidate["end"]
        if padding and abs(candidate["end"] - padding["padded_end"]) < 0.000001:
            semantic_end = padding["base_end"]
            padding_text = (f"Конец до запаса: {timecode(semantic_end)}; "
                            f"добавлен запас {padding['applied_sec']:g} с (запрошен {padding['requested_sec']:g} с).\n")
        elif padding:
            padding_text = "Конец изменён вручную; автоматический запас повторно не добавляется.\n"
        else:
            padding_text = "Запас к этому отчёту ещё не применён — доступна кнопка «Применить запас к отчёту…».\n"
        qa = ""
        if notes and abs(notes["start"] - candidate["start"]) < 0.001 and abs(notes["end"] - semantic_end) < 0.001:
            qa = "\n\nЗАМЕЧАНИЯ ТЕСТОВОГО ПРОГОНА К ЭТИМ ГРАНИЦАМ:\n" + "\n".join(notes["notes"])
        output_dir = (Path(self.context["job_dir"]) / "output" / self.context["report"]["signature"]
                      if "job_dir" in self.context and "signature" in self.context["report"] else None)
        self.set_details(f"ID: {candidate['candidate_id']}\nИмя следующего файла: {output_filename(candidate, output_dir)}\n"
                         + padding_text + "\n"
                         f"Почему выбран: {candidate['reason']}\n"
                         + "\n".join(candidate.get("warnings", [])) + qa + "\n\n" + text)

    def edited_candidate(self):
        current = self.current_candidate()
        if not current:
            raise ValueError("Выберите кандидата.")
        settings = self.context["report"]["settings"]
        start, end = validate_interval(parse_time(self.start.get()), parse_time(self.end.get()),
                                       self.context["manifest"]["media"]["duration_sec"],
                                       settings["min_duration_sec"], settings["max_duration_sec"])
        candidate = dict(current, start=start, end=end, title=self.title.get().strip())
        if not candidate["title"]:
            raise ValueError("Введите заголовок.")
        return candidate

    def decide(self, status):
        if self.busy:
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
            self.refresh_table(candidate["candidate_id"])
        except (ValueError, OSError) as exc:
            messagebox.showerror("Ревью", str(exc), parent=self.root)

    def has_unsaved_edits(self):
        candidate = self.current_candidate()
        if candidate is None:
            return False
        return (self.start.get() != timecode(candidate["start"]) or self.end.get() != timecode(candidate["end"])
                or self.title.get().strip() != candidate["title"])

    def render(self):
        if not self.context:
            messagebox.showinfo("Нарезка", "Сначала получите или откройте отчёт кандидатов.", parent=self.root)
            return
        if self.has_unsaved_edits():
            messagebox.showinfo("Ревью", "Сохраните и при необходимости утвердите правки выбранного кандидата.", parent=self.root)
            return
        report = copy.deepcopy(self.context["report"])
        approved = [c for c in report["candidates"] if c["status"] == "approved"]
        if not approved:
            messagebox.showinfo("Нарезка", "Сначала утвердите хотя бы один интервал.", parent=self.root)
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
        self.start_worker("Финальная нарезка утверждённых интервалов…",
                          lambda: render_approved(manifest, report, job_dir, self.log))

    def preview(self):
        try:
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
        if self.busy:
            messagebox.showinfo("Выполняется обработка", "Дождитесь завершения текущей операции. Промежуточные этапы сохраняются на диск.", parent=self.root)
            return
        if self.has_unsaved_edits() and not messagebox.askyesno("Несохранённые правки", "Закрыть окно без сохранения правок?", parent=self.root):
            return
        self.root.destroy()


def main(report_path=None):
    root = tk.Tk()
    app = HighlightApp(root)
    if report_path:
        root.after(150, lambda: app.start_worker("Загрузка отчёта…", lambda: open_report(report_path)))
    root.mainloop()
