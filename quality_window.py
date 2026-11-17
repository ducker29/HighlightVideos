"""Non-modal quality picker. Workers only communicate through the event queue."""
import copy
import queue
import threading
import tkinter as tk
from tkinter import font as tkfont
from pathlib import Path
from tkinter import ttk

from downloader import video_id_from_url
from job_store import DATA, load_json, save_json
from quality_state import QualityState as S, QualityTableController, STATE_LABELS
from youtube_download import (download_quality, list_available_qualities,
                              migrate_legacy_source, publish_quality, quality_path,
                              set_active_quality)


class QualityDialog:
    def __init__(self, parent, url, on_done, root=DATA, manifest=None, on_change=None):
        self.url = url.strip()
        self.video_id = video_id_from_url(self.url)
        if self.video_id is None:
            raise ValueError("Укажите корректную YouTube-ссылку.")
        self.job_dir = Path(root) / self.video_id
        self.root = Path(root)
        self.manifest_path = self.job_dir / "source.json"
        self.table_cache_path = self.job_dir / "quality_table.json"
        self.manifest = copy.deepcopy(manifest) if manifest else {
            "video_id": self.video_id, "url": self.url, "title": self.video_id}
        if self.manifest.get("video_id") != self.video_id:
            raise ValueError("Манифест относится к другому видео.")
        self.on_done = on_done
        self.on_change = on_change
        self.closed = False
        self.busy = False
        self.events = queue.Queue()
        self.controller = QualityTableController()
        self.qualities = {}
        self.progress_views = {}
        self.progress_values = {}
        self._progress_phase = 0
        self._after = None
        self.top = tk.Toplevel(parent)
        self.top.title("Качество YouTube")
        self.top.transient(parent)
        self.top.geometry("660x430")
        self.top.minsize(540, 280)
        self.status = tk.StringVar(master=self.top, value="Нажмите «Проверить качество».")
        body = ttk.Frame(self.top, padding=12)
        body.pack(fill="both", expand=True)
        self.check_button = ttk.Button(body, text="Проверить качество", command=self.check)
        self.check_button.pack(anchor="w", pady=(0, 8))
        table_frame = ttk.Frame(body)
        table_frame.pack(fill="both", expand=True)
        style = ttk.Style(self.top)
        self.row_padding = round(self.top.winfo_fpixels("10p"))
        row_font = tkfont.nametofont(style.lookup("Treeview", "font") or "TkDefaultFont", root=self.top)
        sample_button = ttk.Button(table_frame, text="загружается")
        content_height = max(row_font.metrics("linespace"), sample_button.winfo_reqheight())
        sample_button.destroy()
        style.configure("Quality.Treeview", rowheight=content_height + 2 * self.row_padding)
        self.table = ttk.Treeview(table_frame, columns=("height", "fps", "status", "action"),
                                  show="headings", selectmode="none", style="Quality.Treeview")
        for column, title, width in (("height", "Разрешение", 100), ("fps", "FPS", 60),
                                     ("status", "Состояние", 140), ("action", "Кнопка", 170)):
            self.table.heading(column, text=title)
            self.table.column(column, width=width, minwidth=60)
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=lambda first, last: self._scrolled(scroll, first, last))
        scroll.pack(side="right", fill="y")
        self.table.pack(side="left", fill="both", expand=True)
        self.table.bind("<Configure>", lambda event: self._position_buttons())
        ttk.Label(body, textvariable=self.status, wraplength=610).pack(fill="x", pady=8)
        actions = ttk.Frame(body)
        actions.pack()
        self.ok_button = ttk.Button(actions, text="Ок", command=self.close)
        self.ok_button.pack(side="left", padx=5)
        self.cancel_button = ttk.Button(actions, text="Отмена", command=self.close)
        self.cancel_button.pack(side="left", padx=5)
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.top.bind("<Destroy>", self._destroy, add="+")
        self._restore_table()
        self._after = self.top.after(60, self._poll)

    def _restore_table(self):
        """Restore metadata offline; durable states come from files/source.json."""
        try:
            manifest = self.manifest
            if self.manifest_path.exists():
                manifest = load_json(self.manifest_path)
                if not isinstance(manifest, dict) or manifest.get("video_id") != self.video_id:
                    raise ValueError("ID сохранённого источника не совпадает со ссылкой.")
            qualities = {}
            if self.table_cache_path.exists():
                cached = load_json(self.table_cache_path)
                if (not isinstance(cached, dict) or cached.get("schema") != 1
                        or cached.get("video_id") != self.video_id
                        or not isinstance(cached.get("qualities"), dict)):
                    raise ValueError("Некорректный кеш таблицы качеств.")
                for height, entry in cached["qualities"].items():
                    h = int(height)
                    if h <= 0 or not isinstance(entry, dict) or not isinstance(entry.get("format_id"), str):
                        raise ValueError("Некорректная строка кеша качеств.")
                    qualities[h] = entry
            self.populate(qualities, manifest)
            if self.qualities:
                self.status.set("Таблица восстановлена. «Проверить качество» обновит список из YouTube.")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.status.set(f"Таблица не восстановлена: {exc}. Нажмите «Проверить качество».")

    def _scrolled(self, scrollbar, first, last):
        scrollbar.set(first, last)
        self._position_buttons()

    def _position_buttons(self):
        for height, button in self.controller.buttons.items():
            downloading = self.controller.states[height] == S.DOWNLOADING
            if downloading and height not in self.progress_views:
                canvas = tk.Canvas(self.table, highlightthickness=1, highlightbackground="#8b8b8b",
                                   background="#e9e9e9", borderwidth=0)
                canvas.bind("<Configure>", lambda event, h=height: self._draw_progress(h))
                self.progress_views[height] = canvas
            canvas = self.progress_views.get(height)
            if canvas is not None and not downloading:
                canvas.destroy()
                del self.progress_views[height]
                self.progress_values.pop(height, None)
            bbox = self.table.bbox(str(height), "action")
            widget = self.progress_views[height] if downloading else button
            if downloading:
                button.place_forget()
            if bbox:
                x, y, width, height_px = bbox
                widget.place(x=x, y=y + self.row_padding, width=width,
                             height=max(1, height_px - 2 * self.row_padding))
            else:
                widget.place_forget()
            if downloading:
                self._draw_progress(height)

    def _draw_progress(self, height):
        canvas = self.progress_views.get(height)
        if canvas is None:
            return
        width, size = canvas.winfo_width(), canvas.winfo_height()
        percent = self.progress_values.get(height)
        canvas.delete("all")
        if percent is None:
            band = max(1, width * .3)
            left = (self._progress_phase % 100) / 100 * (width + band) - band
            right = left + band
            text = "Загружается…"
        else:
            left, right = 0, width * percent / 100
            text = f"Загружается {percent}%"
        canvas.create_rectangle(max(0, left), 0, min(width, right), size,
                                fill="#55bd6a", outline="", tags="fill")
        canvas.create_text(width / 2, size / 2, text=text, fill="#17251a", tags="label")

    def _refresh(self):
        for height, state in self.controller.states.items():
            info = self.qualities[height]
            self.table.item(str(height), values=(f"{height}p", info.get("fps", 0), STATE_LABELS[state], ""))
        self._position_buttons()

    def populate(self, qualities, manifest):
        for canvas in self.progress_views.values():
            canvas.destroy()
        self.progress_views.clear()
        self.progress_values.clear()
        for button in self.controller.buttons.values():
            button.destroy()
        self.table.delete(*self.table.get_children())
        self.manifest = manifest
        self.qualities = dict(qualities)
        for key, entry in manifest.get("qualities", {}).items():
            self.qualities.setdefault(int(key), entry)
        self.controller = QualityTableController(sorted(self.qualities, reverse=True))
        for height in self.controller.states:
            if quality_path(self.job_dir, height).is_file():
                self.controller.set_state(height, S.SELECTED if manifest.get("active_height") == height else S.DOWNLOADED)
            self.table.insert("", "end", iid=str(height))
            button = ttk.Button(self.table, command=lambda h=height: self.click(h))
            self.controller.bind_button(height, button)
        self._refresh()

    def _start(self, operation, work):
        self.busy = True
        self.controller.block_all(True)
        self.check_button.configure(state="disabled")
        def run():
            try:
                result = work()
                self.events.put(("done", operation, result))
            except Exception as exc:
                self.events.put(("error", operation, str(exc)))
        threading.Thread(target=run, daemon=True).start()

    def _log(self, text):
        self.events.put(("log", None, str(text)))

    def check(self):
        if self.busy or self.closed:
            return
        def work():
            manifest = copy.deepcopy(self.manifest)
            if self.manifest_path.exists():
                manifest = load_json(self.manifest_path)
                if manifest.get("video_id") != self.video_id:
                    raise ValueError("ID сохранённого источника не совпадает со ссылкой.")
                if "active_height" not in manifest:
                    migrate_legacy_source(self.job_dir, self._log)
                    manifest = load_json(self.manifest_path)
            qualities = list_available_qualities(self.url, self._log)
            save_json(self.table_cache_path, {"schema": 1, "video_id": self.video_id,
                                             "qualities": {str(h): entry for h, entry in qualities.items()}})
            return qualities, manifest
        self._start("check", work)

    def click(self, height):
        if self.busy or self.closed:
            return
        previous = copy.deepcopy(self.controller.states)
        state = self.controller.click(height)
        self._refresh()
        if state == S.DOWNLOADING:
            manifest = copy.deepcopy(self.manifest)
            def work():
                last_percent = [-1]
                def progress(event):
                    total = event.get("total_bytes") or event.get("total_bytes_estimate")
                    if event.get("status") == "downloading":
                        percent = max(0, min(100, int(100 * event.get("downloaded_bytes", 0) / total))) if total else None
                        if percent != last_percent[0]:
                            last_percent[0] = percent
                            self.events.put(("progress", height, percent))
                    elif event.get("status") == "finished":
                        self.events.put(("progress", height, None))
                _, media = download_quality(self.url, self.video_id, height, self.root, self._log, progress)
                # Publish the cache entry but retain the user's current selection.
                entry = self.qualities[height]
                manifest.setdefault("qualities", {})[str(height)] = dict(entry)
                publish_quality(manifest, height, media, "yt-dlp", entry["format_id"],
                                self.manifest_path, activate=False)
                return height, manifest
            self._start("download", work)
        else:
            def work():
                if state == S.SELECTED:
                    return set_active_quality(self.manifest_path, height)
                manifest = copy.deepcopy(self.manifest)
                manifest["active_height"] = None
                save_json(self.manifest_path, manifest)
                return manifest
            self._selection_before = previous
            self._start("select", work)

    def _poll(self):
        self._after = None
        if self.closed:
            return
        while True:
            try:
                kind, operation, result = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.status.set(result)
                continue
            if kind == "progress":
                if self.controller.states.get(operation) == S.DOWNLOADING:
                    self.progress_values[operation] = result
                    self._draw_progress(operation)
                    self.status.set(f"Загрузка {operation}p" + (f": {result}%" if result is not None else "…"))
                continue
            self.busy = False
            if kind == "error":
                if operation == "download":
                    for h, state in list(self.controller.states.items()):
                        if state == S.DOWNLOADING:
                            self.controller.set_state(h, S.NEUTRAL)
                elif operation == "select":
                    self.controller.states = self._selection_before
                self.status.set("Ошибка: " + result)
            else:
                if operation == "check":
                    self.populate(*result)
                elif operation == "download":
                    height, self.manifest = result
                    self.controller.set_state(height, S.DOWNLOADED)
                else:
                    self.manifest = result
                self.status.set("Готово. Выбор применяется сразу; Ок и Отмена закрывают окно.")
            self.controller.block_all(False)
            self.check_button.configure(state="normal")
            self._refresh()
            if kind == "done" and self.on_change is not None:
                self.on_change(self.controller.selected(), copy.deepcopy(self.manifest))
        self._progress_phase = (self._progress_phase + 4) % 100
        for height in self.progress_views:
            if self.progress_values.get(height) is None:
                self._draw_progress(height)
        self._after = self.top.after(60, self._poll)

    def close(self):
        if self.closed:
            return
        if self.busy:
            self.status.set("Дождитесь завершения операции перед закрытием.")
            return
        self.closed = True
        if self._after is not None:
            self.top.after_cancel(self._after)
            self._after = None
        self.top.destroy()
        self.on_done(self.controller.selected(), copy.deepcopy(self.manifest))

    def _destroy(self, event):
        if event.widget is self.top:
            self.closed = True
            if self._after is not None:
                self.top.after_cancel(self._after)
                self._after = None
