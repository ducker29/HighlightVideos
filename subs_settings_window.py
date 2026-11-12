"""Non-modal Shorts style editor with an actual libass single-frame preview."""
import queue
import subprocess
import tempfile
import threading
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import asdict
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk

from subtitle_settings import DEFAULTS, LEGACY_STYLE, SubtitleSettings, build_ass, load_settings, save_settings
from ui_editing import install_editing


def render_preview(settings):
    # No source video, ASR or models: just one synthetic frame through libass.
    from renderer import _filtergraph_escape_path
    srt = "1\n00:00:00,000 --> 00:00:02,000\nПример субтитров\nВторая строка\n"
    with tempfile.TemporaryDirectory(prefix="subtitle_preview_") as directory:
        path = Path(directory) / ("preview.srt" if settings == DEFAULTS else "preview.ass")
        path.write_text(srt if settings == DEFAULTS else build_ass(srt, settings), encoding="utf-8-sig")
        vf = f"subtitles='{_filtergraph_escape_path(path)}'"
        if settings == DEFAULTS:
            vf += f":force_style='{LEGACY_STYLE}'"
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
        self.traces = []
        self.variables = {}
        fonts = sorted(set(tkfont.families(parent)), key=str.casefold)
        self.fonts = fonts
        frame = ttk.Frame(self.top, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        labels = {"font": "Font", "font_size": "Font Size (ASS 384×288)",
                  "position_mode": "Позиционирование", "x": "Position X (%)", "y": "Position Y (%)",
                  "background_enabled": "Прямоугольная подложка", "background_color": "Background Color",
                  "background_opacity": "Background Opacity (%)"}
        for row, (key, value) in enumerate(asdict(active).items()):
            var = tk.BooleanVar(value=value) if key == "background_enabled" else tk.StringVar(value=str(value))
            self.variables[key] = var
            ttk.Label(frame, text=labels[key]).grid(row=row, column=0, sticky="w", pady=5)
            if key == "font":
                widget = ttk.Combobox(frame, textvariable=var, values=fonts, width=30)
            elif key == "position_mode":
                widget = ttk.Combobox(frame, textvariable=var, values=("historical", "free"), state="readonly")
            elif key == "background_enabled":
                widget = ttk.Checkbutton(frame, variable=var)
            else:
                widget = ttk.Entry(frame, textvariable=var)
                install_editing(widget)
            widget.grid(row=row, column=1, sticky="ew", padx=8)
            if key in ("x", "y", "background_opacity"):
                ttk.Scale(frame, from_=0, to=100, variable=var).grid(row=row, column=2, sticky="ew")
            if key == "background_color":
                ttk.Button(frame, text="Выбрать цвет", command=self.choose_color).grid(row=row, column=2)
            self.traces.append((var, var.trace_add("write", lambda *_, k=key: self.changed(k))))
        self.status = tk.StringVar()
        ttk.Label(frame, textvariable=self.status, wraplength=540).grid(row=8, column=0, columnspan=3, sticky="w")
        ttk.Label(frame, text="historical: исходные отступы. free: центр блока; у краёв позиция ограничивается.\n"
                  "Размер — единицы ASS, не пиксели. Применяется только к будущим Shorts.",
                  wraplength=540).grid(row=9, column=0, columnspan=3, pady=10)
        actions = ttk.Frame(frame)
        actions.grid(row=10, column=0, columnspan=3)
        for text, command in (("Save", self.save), ("Load", self.load),
                              ("Set Default", lambda: self.populate(DEFAULTS)), ("Применить", self.apply)):
            ttk.Button(actions, text=text, command=command).pack(side="left", padx=4)
        preview = ttk.LabelFrame(self.top, text="Subtitle Preview — 9:16", padding=8)
        preview.grid(row=0, column=1, padx=8, pady=8)
        self.image_label = ttk.Label(preview)
        self.image_label.pack()
        ttk.Label(preview, text="FFmpeg/libass, уменьшенный кадр 270×480.\n"
                  "Масштабирование предпросмотра сглаживает детали.").pack()
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.top.bind("<Destroy>", self._destroy, add="+")
        self.changed()
        self.poll()

    def _destroy(self, event):
        if event.widget == self.top:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        for timer in (self.timer, self.preview_timer):
            if timer is not None:
                self.top.after_cancel(timer)
        for var, token in self.traces:
            var.trace_remove("write", token)
        if self.top.winfo_exists():
            self.top.destroy()

    def current(self):
        values = {key: var.get() for key, var in self.variables.items()}
        for key in ("font_size", "x", "y", "background_opacity"):
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
        if key in ("x", "y") and self.variables["position_mode"].get() != "free":
            self.variables["position_mode"].set("free")
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
                self.results.put((revision, render_preview(settings), None))
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
                    self.image_label.configure(image=self.image)
            else:
                self.start_preview()
        except queue.Empty:
            pass
        self.timer = self.top.after(100, self.poll)

    def choose_color(self):
        color = colorchooser.askcolor(parent=self.top, title="Background Color")[1]
        if color:
            self.variables["background_color"].set(color)

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
