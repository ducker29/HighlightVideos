"""Non-modal editor for the settings used by the next Shorts render."""
import tkinter as tk
from tkinter import messagebox, ttk

from shorts_settings import COMPRESSIONS, DEFAULTS, RESOLUTIONS, ShortsSettings


class ShortsSettingsWindow:
    def __init__(self, parent, active, apply_callback):
        self.top = tk.Toplevel(parent)
        self.top.title("Настройки Shorts")
        self.top.resizable(False, False)
        self.closed = False
        self.apply_callback = apply_callback
        self.resolution = tk.StringVar(master=self.top)
        self.compression = tk.StringVar(master=self.top)
        self.status = tk.StringVar(master=self.top)
        frame = ttk.Frame(self.top, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Разрешение видео:").grid(row=0, column=0, sticky="w", pady=6)
        self.resolution_choice = ttk.Combobox(frame, state="readonly", width=47, textvariable=self.resolution,
                                              values=tuple(RESOLUTIONS.values()))
        self.resolution_choice.grid(row=0, column=1, padx=8, pady=6)
        ttk.Label(frame, text="Сжатие видео:").grid(row=1, column=0, sticky="w", pady=6)
        self.compression_choice = ttk.Combobox(frame, state="readonly", width=47, textvariable=self.compression,
                                               values=tuple(item[0] for item in COMPRESSIONS.values()))
        self.compression_choice.grid(row=1, column=1, padx=8, pady=6)
        ttk.Label(frame, text="Меньшее разрешение и более сильное сжатие обычно уменьшают вес файла.\n"
                  "CRF не задаёт точный размер в МБ: результат зависит от движения и деталей.\n"
                  "«Оригинальное» сохраняет высоту источника в пределах кадра 9:16,\n"
                  "но видео всё равно перекодируется. При Smart Crop с увеличением\n"
                  "выбранная область может масштабироваться до выходного разрешения.\n\n"
                  "Параметры применяются к следующим рендерам одного или всех Shorts.\n"
                  "Изменения в полях активируются кнопкой «Применить».",
                  wraplength=580, justify="left").grid(row=2, column=0, columnspan=2, sticky="w", pady=12)
        ttk.Label(frame, textvariable=self.status, wraplength=580).grid(row=3, column=0, columnspan=2, sticky="w")
        actions = ttk.Frame(frame)
        actions.grid(row=4, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(actions, text="По умолчанию", command=lambda: self.populate(DEFAULTS)).pack(side="left", padx=4)
        ttk.Button(actions, text="Применить", command=self.apply).pack(side="left", padx=4)
        ttk.Button(actions, text="Закрыть", command=self.close).pack(side="left", padx=4)
        self.resolution_choice.bind("<<ComboboxSelected>>", self.changed)
        self.compression_choice.bind("<<ComboboxSelected>>", self.changed)
        self.populate(active)
        self.status.set("Активно: " + self.describe(active))
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.top.bind("<Destroy>", self._destroy, add="+")

    @staticmethod
    def describe(settings):
        return f"{RESOLUTIONS[settings.resolution]}; {COMPRESSIONS[settings.compression][0]}"

    def current(self):
        resolution = next((key for key, label in RESOLUTIONS.items() if label == self.resolution.get()), None)
        compression = next((key for key, (label, _) in COMPRESSIONS.items() if label == self.compression.get()), None)
        return ShortsSettings(resolution, compression)

    def populate(self, settings):
        self.resolution.set(RESOLUTIONS[settings.resolution])
        self.compression.set(COMPRESSIONS[settings.compression][0])
        self.changed()

    def changed(self, event=None):
        self.status.set("Изменения в полях не применены. Нажмите «Применить».")

    def apply(self):
        try:
            settings = self.current()
            self.apply_callback(settings)
            self.status.set("Применено: " + self.describe(settings))
        except (ValueError, OSError) as exc:
            messagebox.showerror("Настройки Shorts", str(exc), parent=self.top)

    def _destroy(self, event):
        if event.widget is self.top:
            self.closed = True

    def close(self):
        if not self.closed:
            self.closed = True
            self.top.destroy()
