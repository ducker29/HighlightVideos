"""Independent framing draft UI and low-resolution composition preview."""
import copy
import tkinter as tk
from tkinter import messagebox, ttk

from job_store import parse_time, timecode
from shorts_framing import normalize_framing
from ui_editing import install_editing


def framing_mode_at(framing, time):
    """Half-open local intervals; the document has already been normalized."""
    return next((item["mode"] for item in framing["intervals"]
                 if item["start"] <= time < item["end"]), "CROP")


def fit_blur_preview(image, size):
    """Compose one cached RGB frame. No decoding, disk access or subprocesses.

    The caller supplies the portrait canvas size, not the source resolution.
    Blur is spatial only; every call derives both layers from this frame.
    """
    from PIL import Image, ImageFilter, ImageOps

    background = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
    background = background.filter(ImageFilter.GaussianBlur(radius=max(1, size[0] * 0.025)))
    foreground = ImageOps.contain(image, size, method=Image.Resampling.LANCZOS)
    background.paste(foreground, ((size[0] - foreground.width) // 2,
                                  (size[1] - foreground.height) // 2))
    return background


class FramingPanel(ttk.LabelFrame):
    """Framing-only transaction: cancel never affects Smart Crop segments."""

    def __init__(self, master, framing, duration, get_time, on_change, on_save):
        super().__init__(master, text="Участки кадрирования", padding=4)
        self.duration = duration
        self.get_time = get_time
        self.on_change = on_change
        self.on_save = on_save
        self.saved = normalize_framing(framing, duration)
        self.draft = copy.deepcopy(self.saved)
        self.start_var = tk.StringVar(value="00:00:00.000")
        self.end_var = tk.StringVar(value=timecode(duration))
        self.status_var = tk.StringVar(value="")
        self.mode_var = tk.StringVar(value="CROP")
        self.table = ttk.Treeview(self, columns=("start", "end", "mode"), show="headings", height=3,
                                  selectmode="browse")
        for key, title in (("start", "Начало от старта Shorts"), ("end", "Конец"), ("mode", "Режим")):
            self.table.heading(key, text=title)
            self.table.column(key, width=150, stretch=True)
        self.table.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.table.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.bind("<<TreeviewSelect>>", self._selected)
        self.columnconfigure(0, weight=1)
        form = ttk.Frame(self)
        form.grid(row=1, column=0, columnspan=2, sticky="ew", pady=3)
        for title, variable in (("Начало", self.start_var), ("Конец", self.end_var)):
            ttk.Label(form, text=title).pack(side="left", padx=3)
            entry = ttk.Entry(form, textvariable=variable, width=15)
            entry.pack(side="left")
            install_editing(entry)
        ttk.Button(form, text="Начало = курсор", command=lambda: self.use_cursor("start")).pack(side="left", padx=3)
        ttk.Button(form, text="Конец = курсор", command=lambda: self.use_cursor("end")).pack(side="left")
        ttk.Label(form, text="FIT_BLUR").pack(side="left", padx=8)
        actions = ttk.Frame(self)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew")
        for title, command in (("Добавить", self.add_interval), ("Изменить", self.edit_interval),
                               ("Удалить", self.delete_interval), ("Сохранить", self.save), ("Отмена", self.cancel)):
            ttk.Button(actions, text=title, command=command).pack(side="left", padx=2)
        ttk.Label(actions, textvariable=self.mode_var).pack(side="left", padx=8)
        ttk.Label(self, textvariable=self.status_var).grid(row=3, column=0, columnspan=2, sticky="w")
        self._refresh()

    @property
    def dirty(self):
        return self.draft != self.saved

    def _refresh(self, select_time=None):
        self.table.delete(*self.table.get_children())
        for index, item in enumerate(self.draft["intervals"]):
            self.table.insert("", "end", iid=str(index), values=(timecode(item["start"]),
                              timecode(item["end"]), "FIT_BLUR"))
            if select_time is not None and item["start"] <= select_time < item["end"]:
                self.table.selection_set(str(index))
        prefix = "Есть несохранённые изменения. " if self.dirty else ""
        self.status_var.set(prefix + "Вне интервалов — CROP. Сохранённые участки применяются при экспорте.")

    def _index(self):
        selected = self.table.selection()
        if not selected:
            raise ValueError("Выберите участок кадрирования.")
        return int(selected[0])

    def _selected(self, _event=None):
        if self.table.selection():
            item = self.draft["intervals"][self._index()]
            self.start_var.set(timecode(item["start"]))
            self.end_var.set(timecode(item["end"]))

    def use_cursor(self, boundary):
        variable = self.start_var if boundary == "start" else self.end_var
        variable.set(timecode(self.get_time()))

    def _change(self, action):
        try:
            draft = copy.deepcopy(self.draft)
            if action == "delete":
                del draft["intervals"][self._index()]
                start = None
            else:
                start = parse_time(self.start_var.get())
                item = {"start": start, "end": parse_time(self.end_var.get()), "mode": "FIT_BLUR"}
                if action == "add":
                    draft["intervals"].append(item)
                else:
                    draft["intervals"][self._index()] = item
            self.draft = normalize_framing(draft, self.duration)
            self._refresh(start)
            self.on_change()
        except (ValueError, OverflowError) as exc:
            messagebox.showerror("Участки кадрирования", str(exc), parent=self)

    def add_interval(self):
        self._change("add")

    def edit_interval(self):
        self._change("edit")

    def delete_interval(self):
        self._change("delete")

    def save(self):
        if not self.dirty:
            return
        try:
            normalized = normalize_framing(self.draft, self.duration)
            self.on_save(copy.deepcopy(normalized))
        except (ValueError, OSError) as exc:
            messagebox.showerror("Сохранение framing", str(exc), parent=self)
            return
        self.saved = normalized
        self.draft = copy.deepcopy(normalized)
        self._refresh()

    def cancel(self):
        self.draft = copy.deepcopy(self.saved)
        self.start_var.set("00:00:00.000")
        self.end_var.set(timecode(self.duration))
        self._refresh()
        self.on_change()
