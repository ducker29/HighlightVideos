"""Non-modal Shorts style editor with an actual libass single-frame preview."""
import queue
import math
import subprocess
import tempfile
import threading
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import asdict
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk

from subtitle_settings import (DEFAULTS, LEGACY_STYLE, SubtitleSettings, build_ass, load_settings,
                               save_settings, subtitle_area, uses_legacy_srt_style)
from subtitle_background import MeasurementCache, prepare_ass
from ui_editing import install_editing


BACKGROUND_GEOMETRY_FIELDS = ("background_padding_x_px", "background_padding_y_px", "background_corner_radius_px")


def render_preview(settings, measurement_cache=None):
    # No source video, ASR or models: just one synthetic frame through libass.
    from renderer import _filtergraph_escape_path
    srt = "1\n00:00:00,000 --> 00:00:02,000\nПример субтитров\nВторая строка\n"
    legacy = uses_legacy_srt_style(settings)
    with tempfile.TemporaryDirectory(prefix="subtitle_preview_") as directory:
        path = Path(directory) / ("preview.srt" if legacy else "preview.ass")
        sample_words = [[{"word": word, "start": i * .5, "end": (i + 1) * .5}
                         for i, word in enumerate(("Пример", "субтитров", "Вторая", "строка"))]]
        if legacy:
            content = srt
        elif settings.background_enabled and settings.background_geometry_mode == "custom":
            content = prepare_ass(srt, settings, 1080, 1920, sample_words, cache=measurement_cache)
        else:
            content = build_ass(srt, settings, sample_words)
        path.write_text(content, encoding="utf-8-sig")
        vf = f"subtitles='{_filtergraph_escape_path(path)}'"
        if legacy:
            vf += f":force_style='{LEGACY_STYLE}'"
        if settings.area_enabled:
            left, top, right, bottom = subtitle_area(settings)
            vf += (f",drawbox=x={left * 1080 / 384}:y={top * 1920 / 288}:"
                   f"w={(right - left) * 1080 / 384}:h={(bottom - top) * 1920 / 288}:color=yellow@0.7:t=2")
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=0x405570:s=1080x1920:d=0.04",
             "-vf", vf + ",scale=270:480", "-frames:v", "1", "-f", "image2pipe", "-vcodec", "ppm", "pipe:1"],
            capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise ValueError(result.stderr.decode("utf-8", errors="replace")[-1500:])
        return result.stdout


class SubsSettingsWindow:
    def __init__(self, parent, active, apply_callback):
        self.top = tk.Toplevel(parent)
        self.top.title("Subs Settings — Shorts")
        self.apply_callback = apply_callback
        self.closed = False
        self.timer = None
        self.preview_timer = None
        self.running = False
        self.revision = 0
        self.results = queue.Queue()
        self.measurement_cache = MeasurementCache()
        self.traces = []
        self.variables = {}
        fonts = sorted(set(tkfont.families(parent)), key=str.casefold)
        self.fonts = fonts
        self.top.rowconfigure(0, weight=1)
        self.top.columnconfigure(0, weight=1)
        body = ttk.Frame(self.top, padding=12)
        body.grid(row=0, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.settings_canvas = tk.Canvas(left, highlightthickness=0, width=1, height=1)
        self.settings_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(left, orient="vertical", command=self.settings_canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.settings_canvas.configure(yscrollcommand=scrollbar.set)
        self.settings_frame = ttk.Frame(self.settings_canvas)
        self.settings_frame.columnconfigure(0, weight=1)
        self.settings_item = self.settings_canvas.create_window(0, 0, window=self.settings_frame, anchor="nw")
        self.settings_frame.bind("<Configure>", self._update_scrollregion)
        self.settings_canvas.bind("<Configure>", self._resize_settings)
        # A toplevel binding sees descendant events without a process-wide bind_all.
        self.top.bind("<MouseWheel>", self._scroll_settings, add="+")
        self.top.bind("<FocusIn>", self._reveal_setting, add="+")
        labels = {"font": "Font", "font_size": "Font Size (ASS 384×288)",
                  "position_mode": "Позиционирование", "x": "Position X (%)", "y": "Position Y (%)",
                  "background_enabled": "Прямоугольная подложка", "background_color": "Background Color",
                  "background_opacity": "Background Opacity (%)",
                  "background_padding_x_px": "Padding X (px)",
                  "background_padding_y_px": "Padding Y (px)",
                  "background_corner_radius_px": "Corner Radius (px)",
                  "background_fill_region": "Во всю область",
                  "outline_enabled": "Обводка текста", "outline_color": "Цвет обводки",
                  "area_enabled": "Ограничить область субтитров",
                  "width_percent": "Ширина области (%)", "height_percent": "Высота области (%)",
                  "highlight_word_enabled": "Менять цвет текущего слова",
                  "highlight_word_color": "Цвет текущего слова"}
        sections = (
            ("Шрифт и позиционирование", ("font", "font_size", "position_mode", "x", "y")),
            ("Подложка субтитров", ("background_enabled", "background_color", "background_opacity",
                                    "background_padding_x_px", "background_padding_y_px", "area_enabled",
                                    "width_percent", "height_percent", "background_fill_region",
                                    "background_corner_radius_px")),
            ("Обводка и дополнительные эффекты", ("outline_enabled", "outline_color",
                                                     "highlight_word_enabled", "highlight_word_color")),
        )
        self.sections = []
        self.field_widgets = {}
        placements = {}
        for index, (title, keys) in enumerate(sections):
            section = ttk.LabelFrame(self.settings_frame, text=title, padding=8)
            section.grid(row=index, column=0, sticky="ew", pady=(0, 10))
            section.columnconfigure(1, weight=1)
            self.sections.append(section)
            placements.update({key: (section, row) for row, key in enumerate(keys)})
        for key, value in asdict(active).items():
            var = tk.BooleanVar(value=value) if type(value) is bool else tk.StringVar(value=str(value))
            self.variables[key] = var
            if key == "background_geometry_mode":
                self.traces.append((var, var.trace_add("write", lambda *_, k=key: self.changed(k))))
                continue  # Persisted internal state; never a separate user control.
            frame, row = placements[key]
            ttk.Label(frame, text=labels[key]).grid(row=row, column=0, sticky="w", pady=5)
            if key == "font":
                widget = ttk.Combobox(frame, textvariable=var, values=fonts, width=24)
            elif key == "position_mode":
                widget = ttk.Combobox(frame, textvariable=var, values=("historical", "free"), state="readonly", width=14)
            elif type(value) is bool:
                widget = ttk.Checkbutton(frame, variable=var)
            else:
                widget = ttk.Entry(frame, textvariable=var, width=12)
                install_editing(widget)
            widget.grid(row=row, column=1, sticky="ew", padx=8)
            self.field_widgets[key] = widget
            if key in ("x", "y", "background_opacity", "width_percent", "height_percent"):
                ttk.Scale(frame, from_=1 if key.endswith("_percent") else 0, to=100,
                          variable=var).grid(row=row, column=2, sticky="ew")
            if key in ("background_color", "outline_color", "highlight_word_color"):
                ttk.Button(frame, text="Выбрать цвет", command=lambda k=key: self.choose_color(k)).grid(row=row, column=2)
            self.traces.append((var, var.trace_add("write", lambda *_, k=key: self.changed(k))))
        self.geometry_notice = tk.StringVar()
        self.geometry_notice_label = ttk.Label(self.sections[1], textvariable=self.geometry_notice, wraplength=540)
        self.geometry_notice_label.grid(row=len(sections[1][1]), column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.status = tk.StringVar()
        ttk.Label(self.settings_frame, text="historical: исходные отступы. free: центр блока; у краёв позиция ограничивается.\n"
                  "Область: перенос по ширине, обрезка за границами; размер шрифта не меняется.\n"
                  "Размер шрифта — единицы ASS. Padding и радиус — пиксели итогового видео.\n"
                  "Применяется только к будущим Shorts.",
                  wraplength=540).grid(row=len(sections), column=0, sticky="ew", pady=10)
        footer = ttk.Frame(self.top, padding=(12, 0, 12, 12))
        footer.grid(row=1, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(footer, textvariable=self.status, wraplength=540)
        self.status_label.grid(row=0, column=0, sticky="ew")
        footer.bind("<Configure>", lambda event: self.status_label.configure(wraplength=max(1, event.width - 24)))
        self.actions = ttk.Frame(footer)
        self.actions.grid(row=1, column=0, pady=(6, 0))
        for text, command in (("Save", self.save), ("Load", self.load),
                              ("Set Default", lambda: self.populate(DEFAULTS)), ("Применить", self.apply)):
            ttk.Button(self.actions, text=text, command=command).pack(side="left", padx=4)
        preview = ttk.LabelFrame(body, text="Subtitle Preview — 9:16", padding=8)
        preview.grid(row=0, column=1, padx=(12, 0), sticky="nsew")
        preview.rowconfigure(0, weight=1)
        preview.columnconfigure(0, weight=1)
        self.preview_canvas = tk.Canvas(preview, width=270, height=1, highlightthickness=0)
        self.preview_canvas.grid(row=0, column=0, sticky="nsew")
        self.preview_canvas.bind("<Configure>", self._display_preview)
        ttk.Label(preview, text="FFmpeg/libass, уменьшенный кадр 270×480.\n"
                  "Масштабирование предпросмотра сглаживает детали.\n"
                  "Жёлтая рамка области — только в предпросмотре.", wraplength=270).grid(row=1, column=0)
        self.top.update_idletasks()
        width = min(self.settings_frame.winfo_reqwidth() + preview.winfo_reqwidth() + 64,
                    self.top.winfo_screenwidth() - 80)
        height = min(720, self.top.winfo_screenheight() - 100)
        self.top.geometry(f"{width}x{height}")
        self.top.minsize(min(width, self.settings_frame.winfo_reqwidth() + preview.winfo_reqwidth() + 48),
                         min(height, 420))
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.top.bind("<Destroy>", self._destroy, add="+")
        self.changed()
        self.poll()

    def _update_scrollregion(self, event=None):
        self.settings_canvas.configure(scrollregion=self.settings_canvas.bbox("all"))

    def _resize_settings(self, event):
        self.settings_canvas.itemconfigure(self.settings_item, width=event.width)

    def _scroll_settings(self, event):
        widget = event.widget
        while widget is not None:
            if widget == self.settings_canvas:
                if self.settings_frame.winfo_height() > self.settings_canvas.winfo_height():
                    self.settings_canvas.yview_scroll(-int(event.delta / 120), "units")
                return "break"
            widget = getattr(widget, "master", None)

    def _reveal_setting(self, event):
        widget = event.widget
        ancestor = widget
        while ancestor is not None:
            if ancestor == self.settings_frame:
                canvas = self.settings_canvas
                top = widget.winfo_rooty() - self.settings_frame.winfo_rooty()
                bottom = top + widget.winfo_height()
                visible_top = canvas.canvasy(0)
                visible_bottom = visible_top + canvas.winfo_height()
                target = top if top < visible_top else bottom - canvas.winfo_height()
                if top < visible_top or bottom > visible_bottom:
                    canvas.yview_moveto(max(0, target) / max(1, self.settings_frame.winfo_height()))
                return
            ancestor = getattr(ancestor, "master", None)

    def _display_preview(self, event=None):
        if not hasattr(self, "image"):
            return
        width, height = self.preview_canvas.winfo_width(), self.preview_canvas.winfo_height()
        factor = max(1, math.ceil(self.image.width() / max(1, width)),
                     math.ceil(self.image.height() / max(1, height)))
        # Exact integer subsampling preserves 9:16 without adding an imaging dependency.
        factor = next((value for value in range(factor, max(self.image.width(), self.image.height()) + 1)
                       if self.image.width() % value == 0 and self.image.height() % value == 0), factor)
        self.display_image = self.image.subsample(factor, factor) if factor > 1 else self.image
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(width / 2, height / 2, image=self.display_image)

    def _destroy(self, event):
        if event.widget == self.top:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.measurement_cache.close()
        for timer in (self.timer, self.preview_timer):
            if timer is not None:
                self.top.after_cancel(timer)
        for var, token in self.traces:
            var.trace_remove("write", token)
        if self.top.winfo_exists():
            self.top.destroy()

    def current(self):
        values = {key: var.get() for key, var in self.variables.items()}
        for key in ("font_size", "x", "y", "background_opacity", "width_percent", "height_percent") + BACKGROUND_GEOMETRY_FIELDS:
            try:
                values[key] = float(values[key])
            except ValueError as exc:
                raise ValueError(f"{key}: требуется число.") from exc
        settings = SubtitleSettings(**values)
        if settings.font.casefold() not in {f.casefold() for f in self.fonts}:
            self.status.set("Шрифт отсутствует в Windows; libass использует доступную замену. Выберите Arial при необходимости.")
        return settings

    def populate(self, settings):
        # Disable traces to avoid X/Y edits switching a loaded historical preset to free.
        for var, token in self.traces:
            var.trace_remove("write", token)
        for key, value in asdict(settings).items():
            self.variables[key].set(value)
        self.traces = [(var, var.trace_add("write", lambda *_, k=key: self.changed(k)))
                       for key, var in self.variables.items()]
        self.changed()

    def changed(self, key=None):
        if self.closed:
            return
        if ((key in BACKGROUND_GEOMETRY_FIELDS
             or key == "background_fill_region" and self.variables["background_fill_region"].get())
                and self.variables["background_geometry_mode"].get() != "custom"):
            self.variables["background_geometry_mode"].set("custom")
        background = self.variables["background_enabled"].get()
        area = self.variables["area_enabled"].get()
        fill = self.variables["background_fill_region"].get()
        fill_active = background and area and fill
        for name, enabled in (("background_fill_region", background and area),
                              ("background_padding_x_px", background and not fill_active),
                              ("background_padding_y_px", background and not fill_active),
                              ("background_corner_radius_px", fill_active)):
            self.field_widgets[name].state(["!disabled"] if enabled else ["disabled"])
        notice = ""
        if fill and not fill_active:
            notice = "Флажок сохранён; режим временно неактивен без включённых подложки и ограничения области."
        self.geometry_notice.set(notice)
        if key in ("width_percent", "height_percent"):
            self.variables["area_enabled"].set(True)
        if (key in ("x", "y", "width_percent", "height_percent")
                or key == "area_enabled" and self.variables["area_enabled"].get()) and self.variables["position_mode"].get() != "free":
            self.variables["position_mode"].set("free")
        if key == "position_mode" and self.variables["position_mode"].get() == "historical":
            self.variables["area_enabled"].set(False)
        self.revision += 1
        if self.preview_timer is not None:
            self.top.after_cancel(self.preview_timer)
        self.preview_timer = self.top.after(300, self.start_preview)

    def start_preview(self):
        self.preview_timer = None
        if self.running or self.closed:
            return
        try:
            self.status.set("")
            settings = self.current()
        except (ValueError, tk.TclError) as exc:
            self.status.set(str(exc))
            return
        revision = self.revision
        self.running = True

        def worker():
            try:
                self.results.put((revision, render_preview(settings, measurement_cache=self.measurement_cache), None))
            except Exception as exc:
                self.results.put((revision, None, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        self.timer = None
        if self.closed:
            return
        try:
            revision, image, error = self.results.get_nowait()
            self.running = False
            if revision == self.revision:
                if error:
                    self.status.set("Предпросмотр недоступен: " + error)
                else:
                    self.image = tk.PhotoImage(data=image, format="PPM", master=self.top)
                    self._display_preview()
            else:
                self.start_preview()
        except queue.Empty:
            pass
        self.timer = self.top.after(100, self.poll)

    def choose_color(self, key="background_color"):
        color = colorchooser.askcolor(parent=self.top, title="Цвет обводки" if key == "outline_color" else "Background Color")[1]
        if color:
            self.variables[key].set(color)

    def save(self):
        try:
            settings = self.current()
            path = filedialog.asksaveasfilename(parent=self.top, defaultextension=".json", filetypes=[("JSON", "*.json")])
            if path:
                save_settings(path, settings)
                messagebox.showinfo("Subs Settings", "Пресет сохранён. Для активации нажмите Применить.", parent=self.top)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Subs Settings", str(exc), parent=self.top)

    def load(self):
        path = filedialog.askopenfilename(parent=self.top, filetypes=[("JSON", "*.json")])
        if path:
            try:
                self.populate(load_settings(path))
            except (ValueError, OSError) as exc:
                messagebox.showerror("Subs Settings", str(exc), parent=self.top)

    def apply(self):
        try:
            self.apply_callback(self.current())
            self.status.set("Настройки применены к следующим рендерам Shorts.")
        except (ValueError, OSError) as exc:
            messagebox.showerror("Subs Settings", str(exc), parent=self.top)
