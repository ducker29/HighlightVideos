"""Smart Crop Editor: separate Toplevel window for previewing and rough 16:9 -> 9:16 cropping.

Decoding runs on a single background worker thread; all Tkinter widget access
happens on the main thread through a queue polled with ``after()``. Stale
frame requests are dropped so a fast timeline scrub does not pile up work.

This is the prototype stage: the crop comes from the Smart Crop settings and is
shown as a preview. Interactive crop dragging and interval editing are not part
of this stage.
"""
import math
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox

from smart_crop_storage import segment_at, set_segment_crop, split_segment, merge_segments
from job_store import timecode

try:
    import av
except ImportError:  # pragma: no cover - av is a declared dependency
    av = None


# --------------------------------------------------------------------------- #
# Pure geometry helpers (no Tkinter / no video required to test).
# --------------------------------------------------------------------------- #
def crop_pixels(normalized_crop, width, height):
    """Convert a normalized crop to integer source pixels, clamped to the frame."""
    width = int(width)
    height = int(height)
    x = int(round(normalized_crop["x"] * width))
    y = int(round(normalized_crop["y"] * height))
    w = int(round(normalized_crop["width"] * width))
    h = int(round(normalized_crop["height"] * height))
    x = max(0, min(x, width - 1))
    y = max(0, min(y, height - 1))
    w = max(1, min(w, width - x))
    h = max(1, min(h, height - y))
    return x, y, w, h


def fit_size(width, height, max_w, max_h):
    """Scale (width, height) into (max_w, max_h) preserving aspect, min 1px."""
    scale = min(max_w / width, max_h / height)
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def portrait_fit(width, height, target_h):
    """Scale a crop region to exactly 9:16 portrait (height = target_h, min 1px).

    The Shorts output is a fixed 9:16 portrait; the preview frame must have that
    aspect regardless of the crop region's own aspect, so it is scaled to a fixed
    height and the width follows from the 9/16 ratio (never stretched).
    """
    h = max(1, int(round(target_h)))
    w = max(1, int(round(h * 9 / 16)))
    return w, h


def split_time_for_displayed_frame(playhead_time, frame_time, duration):
    """Use the decoded frame's actual local PTS for a manual split.

    The decoder accepts a half-frame tolerance, so the frame displayed at a
    slider position can be newer than the slider's nominal time. Saving the
    slider value would then assign that first new frame to the old crop.
    """
    if frame_time is None:
        raise ValueError("Cannot split until the requested preview frame is ready")
    t = max(0.0, min(float(duration), float(frame_time)))
    if not 0.0 < t < float(duration):
        raise ValueError("Split frame must be inside the clip")
    return round(t, 6)


def segment_for(settings, time):
    """Return the segment dict containing time (same rules as storage.segment_at)."""
    index = segment_at(settings, time)
    return settings["segments"][index] if index >= 0 else None


def timeline_segment_at(segments, duration, x_px, canvas_width):
    """Map a horizontal pixel coordinate to a segment index on the timeline.

    ``segments`` is a list of dicts with numeric ``start``/``end`` in local
    Short time; ``duration`` is the total Short duration. Returns -1 when the
    coordinate falls outside the drawn timeline. Pure geometry: no validation,
    no copying of settings.
    """
    if canvas_width <= 0 or duration <= 0:
        return -1
    x = float(x_px)
    if x < 0 or x > canvas_width:
        return -1
    time = min(max(x / canvas_width, 0.0), 1.0) * duration
    for i, seg in enumerate(segments):
        if seg["start"] <= time < seg["end"] or (i == len(segments) - 1 and abs(time - seg["end"]) < 1e-6):
            return i
    return -1


def display_geometry(fw, fh, max_w, max_h):
    """Centered display geometry of an (fw, fh) frame inside (max_w, max_h).

    Returns (cx, cy, dw, dh, scale): top-left corner and size in canvas px.
    """
    dw, dh = fit_size(fw, fh, max_w, max_h)
    cx = (max_w - dw) / 2
    cy = (max_h - dh) / 2
    return cx, cy, dw, dh, dw / fw


CENTERED_916 = {"x": 0.341796875, "y": 0.0, "width": 0.31640625, "height": 1.0}  # 16:9 source only
_MIN_CROP = 0.02  # smallest allowed crop side (normalized), avoids zero-size boxes


def clamp_crop(x, y, w, h, fw=16, fh=9):
    """Keep a normalized crop inside the source at a 9:16 pixel aspect ratio.

    The width is the requested zoom level; height is derived from source pixels.
    """
    if fw <= 0 or fh <= 0:
        raise ValueError("Source dimensions must be positive")
    ar = 9.0 / 16.0
    width_per_height = ar * fh / fw
    # A minimum normalized width is useful for mouse interaction. The resulting
    # height is derived rather than independently clamped (which breaks 9:16).
    w = max(_MIN_CROP, float(w))
    h = w / width_per_height
    if h > 1.0:
        h = 1.0
        w = h * width_per_height
    if w > 1.0:
        w = 1.0
        h = w / width_per_height
    x = min(max(0.0, float(x)), 1.0 - w)
    y = min(max(0.0, float(y)), 1.0 - h)
    return {"x": x, "y": y, "width": w, "height": h}


def centered_crop_916(width=1, height=1):
    """Return a centered, largest-possible 9:16 crop for any source aspect."""
    if width <= 0 or height <= 0:
        raise ValueError("Source dimensions must be positive")
    width_per_height = (9.0 / 16.0) * height / width
    if width_per_height <= 1.0:
        w, h = width_per_height, 1.0
    else:
        w, h = 1.0, 1.0 / width_per_height
    return clamp_crop((1.0 - w) / 2.0, (1.0 - h) / 2.0,
                      w, h, width, height)


def clamp_drag(x, y, w, h, canvas_w, canvas_h):
    """Clamp a drag so the 9:16 box stays fully inside the visible canvas."""
    x = min(max(0.0, x), max(0.0, canvas_w - w))
    y = min(max(0.0, y), max(0.0, canvas_h - h))
    return x, y, w, h


def clamp_box_in_area(x, y, ww, hh, area):
    """Clamp a box to a (cx, cy, aw, ah, scale) area (letterboxed image bounds)."""
    cx, cy, aw, ah, _scale = area
    x = min(max(cx, x), max(cx, cx + aw - ww))
    y = min(max(cy, y), max(cy, cy + ah - hh))
    return x, y, ww, hh


def local_to_absolute(local_t, start):
    """Map editor (candidate-local) time to absolute source-video time."""
    return start + local_t


def absolute_to_local(abs_t, start):
    """Map absolute source-video time to editor (candidate-local) time."""
    return abs_t - start


def _time_base_seconds(tb):
    if tb is None or tb.denominator == 0:
        return 1.0
    return tb.numerator / tb.denominator


# --------------------------------------------------------------------------- #
# Background frame decoder.
# --------------------------------------------------------------------------- #
class FrameDecoder:
    """Decode a single frame near a source time on demand (blocking).

    ``close()`` is thread-safe and idempotent. The worker checks ``is_closed``
    before opening a container so a queued request arriving after close is a
    no-op rather than an error.
    """

    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._closed = False
        self._container = None
        self.width = self.height = 0
        self.fps = 30.0
        self._frames = None
        self._last_frame_time = None
        self._last_frame = None

    def is_closed(self):
        return self._closed

    def open(self):
        if self._closed:
            return False
        container = av.open(self.path)
        stream = container.streams.video[0]
        self.width = stream.codec_context.width
        self.height = stream.codec_context.height
        self.fps = float(stream.average_rate or 30.0)
        self._container = container
        return True

    def request_frame(self, time_sec, seq=None):
        """Return an RGB numpy array (H, W, 3) for the frame nearest time_sec."""
        with self._lock:
            if self._closed or self._container is None:
                return None
            container = self._container
            stream = container.streams.video[0]
            target = max(0.0, float(time_sec))
            tolerance = 0.5 / max(self.fps, 1.0)
            # Consecutive playback frames use the existing decoder iterator.
            # A random seek is needed only for backward or large jumps.
            if (self._frames is None or self._last_frame_time is None
                    or target < self._last_frame_time - tolerance
                    or target - self._last_frame_time > 0.5):
                tb = _time_base_seconds(stream.time_base)
                offset = int(target / tb)
                try:
                    container.seek(offset, stream=stream, backward=True)
                except TypeError:  # older PyAV
                    container.seek(offset)
                self._frames = iter(container.decode(video=0))
                self._last_frame_time = None
                self._last_frame = None
            if (self._last_frame is not None and self._last_frame_time is not None
                    and abs(target - self._last_frame_time) <= tolerance):
                return self._last_frame
            for frame in self._frames:
                if frame.pts is None:
                    continue
                frame_time = float(frame.pts * (frame.time_base or stream.time_base))
                if frame_time + tolerance < target:
                    continue
                self._last_frame_time = frame_time
                self._last_frame = frame.to_rgb().to_ndarray()
                return self._last_frame
        return None

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            container, self._container = self._container, None
        if container is not None:
            try:
                container.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# The editor window.
# --------------------------------------------------------------------------- #
SOURCE_W = 640
SOURCE_H = 360
PREVIEW_W = 300
PREVIEW_H = 533
POLL_MS = 40
MAX_DECODE_PIXELS = 1024 * 1024  # decode at most ~1M pixels per frame (~1024x1024)
TIMELINE_H = 44


class SmartCropWindow(ttk.Frame):
    """Toplevel-style Smart Crop editor embedded as a Frame.

    Pass ``parent=None`` to create and own the Toplevel, or pass an existing
    ``root``/frame to embed. All state updates go through ``_on_event``.
    """

    def __init__(self, master, source_path, start, end, candidate_id, settings,
                 own_toplevel=False):
        self.source_path = str(source_path)
        self.start = float(start)
        self.end = float(end)
        self.candidate_id = candidate_id
        self.settings = settings
        self.duration = max(0.001, self.end - self.start)
        self.fps = 30.0

        self._events = queue.Queue()
        self._commands = queue.Queue()
        self._worker = None
        self._decoder = None
        self._playing = False
        self._setting_slider = False
        self._programmatic_slider_value = None
        self._seq = 0
        self._frame_pending = False
        self._displayed_frame_local_time = None
        self._closed = False
        self._source_image = None
        self._preview_image = None
        self._poll_id = None
        self._worker_after_id = None

        # Crop-editing state (canvas pixel space of the source canvas).
        self._canvas_size = (SOURCE_W, SOURCE_H)
        self._source_display = None  # (cx, cy, dw, dh, scale) of the shown source frame
        self._crop_px = None  # (x, y, w, h) crop box in canvas px
        self._frame_cached = None  # last decoded (possibly capped) RGB array
        self._frame_size = None
        self._drag = None  # (offset_x, offset_y) while dragging the box
        self._crop_rect_id = None
        self._crop_label = None
        self._shown_segment_index = None
        self._selected_segment = 0
        self._timeline_rect_ids = None

        if own_toplevel or master is None:
            self.toplevel = tk.Toplevel()
            self.toplevel.title(f"Smart Crop — {candidate_id}")
            self.toplevel.protocol("WM_DELETE_WINDOW", self.close)
            frame_parent = self.toplevel
        else:
            self.toplevel = None
            frame_parent = master

        super().__init__(frame_parent)
        self._build()
        self._seek_to(0.0)
        self._poll_id = self.after(POLL_MS, self._poll)
        self._worker_after_id = self.after(50, self._start_worker)

    # -- actual canvas geometry -------------------------------------------- #
    def _source_canvas_size(self):
        """Real rendered size of the source canvas (px), never zero."""
        wdt = max(1, int(self.source_canvas.winfo_width()))
        hgt = max(1, int(self.source_canvas.winfo_height()))
        return wdt, hgt

    def _preview_canvas_size(self):
        wdt = max(1, int(self.preview_canvas.winfo_width()))
        hgt = max(1, int(self.preview_canvas.winfo_height()))
        return wdt, hgt

    # -- layout ----------------------------------------------------------- #
    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(4, 2))
        ttk.Label(top, text=f"Shorts {self.candidate_id}   16:9 → 9:16").pack(side="left")
        self.info_label = ttk.Label(top, text="загрузка…")
        self.info_label.pack(side="right")

        row = ttk.Frame(self)
        self._image_frame = row
        row.pack(fill="both", expand=True, padx=4, pady=2)
        self.source_canvas = tk.Canvas(row, width=SOURCE_W, height=SOURCE_H,
                                       bg="black", highlightthickness=0)
        self.source_canvas.pack(side="left", fill="both", expand=True, padx=(0, 4))
        self.source_canvas.bind("<Configure>", self._on_canvas_configure)
        self.source_canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.source_canvas.bind("<B1-Motion>", self._on_drag_move)
        self.source_canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        self.source_canvas.bind_all("<Button-5>", self._on_zoom)   # Linux scroll down
        self.source_canvas.bind_all("<Button-4>", self._on_zoom)   # Linux scroll up
        self.source_canvas.bind_all("<MouseWheel>", self._on_zoom) # Windows/macOS
        self.preview_canvas = tk.Canvas(row, width=PREVIEW_W, height=PREVIEW_H,
                                        bg="black", highlightthickness=0)
        self.preview_canvas.pack(side="right", fill="both", expand=False, padx=(8, 0))

        bottom = ttk.Frame(self)
        self._transport_frame = bottom
        bottom.pack(fill="x", padx=8, pady=6)
        btns = ttk.Frame(bottom)
        btns.pack(side="left", fill="x", expand=True)
        self.play_btn = ttk.Button(btns, text="▶ Play", command=self.toggle_play)
        self.play_btn.pack(side="left", padx=2)
        ttk.Button(btns, text="⏮ кадр", command=lambda: self.step_frame(-1)).pack(side="left", padx=2)
        ttk.Button(btns, text="кадр ⏭", command=lambda: self.step_frame(1)).pack(side="left", padx=2)
        self.reset_crop_button = ttk.Button(btns, text="Сбросить crop", command=self.reset_crop)
        self.reset_crop_button.pack(side="left", padx=2)
        ttk.Button(btns, text="Разделить", command=self.split_current_segment).pack(side="left", padx=2)
        ttk.Button(btns, text="Объединить со следующим", command=self.merge_current_segment).pack(side="left", padx=2)
        self.crop_label = ttk.Label(btns, text="")
        self.crop_label.pack(side="left", padx=(8, 2))

        self.slider = ttk.Scale(bottom, from_=0.0, to=self.duration, orient="horizontal",
                                command=self._on_slider)
        self.slider.pack(side="left", fill="x", expand=True, padx=(12, 6))
        self.time_label = ttk.Label(bottom, text="00:00:00.000")
        self.time_label.pack(side="left")

        self.timeline_canvas = tk.Canvas(self, height=TIMELINE_H, bg="#1a1a1a",
                                         highlightthickness=0)
        self.timeline_canvas.pack(fill="x", padx=8, pady=(0, 4))
        self.timeline_canvas.bind("<ButtonPress-1>", self._on_timeline_click)
        self.timeline_canvas.bind("<Configure>", lambda _e: self._draw_timeline())
        self._draw_timeline()

    def configure_framing(self, framing, on_save):
        """Attach report-backed framing without changing the Smart Crop document."""
        from framing_panel import FramingPanel
        self._framing_frame_time = None
        self.framing_panel = FramingPanel(self, framing, self.duration, self._current_time,
                                          self._framing_changed, on_save)
        # Reserve controls first; the large requested preview height must not
        # push the transport or timeline outside a normal 1180x760 window.
        self._image_frame.pack_forget()
        self._transport_frame.pack_forget()
        self.timeline_canvas.pack_forget()
        self.timeline_canvas.pack(side="bottom", fill="x", padx=8, pady=(0, 4))
        self._transport_frame.pack(side="bottom", fill="x", padx=8, pady=6)
        self.framing_panel.pack(side="bottom", fill="x", padx=8, pady=4)
        self._image_frame.pack(fill="both", expand=True, padx=4, pady=2)
        self.preview_canvas.bind("<Configure>", lambda _event: self._refresh_preview())
        self._framing_changed()

    def _framing_mode(self, time=None):
        from framing_panel import framing_mode_at
        panel = getattr(self, "framing_panel", None)
        return framing_mode_at(panel.draft, self._current_time() if time is None else time) if panel is not None else "CROP"

    def _framing_changed(self):
        self._drag = None
        self._sync_framing_state()
        self._draw_timeline()
        self._refresh_preview()

    def _sync_framing_state(self):
        panel = getattr(self, "framing_panel", None)
        if panel is None:
            return
        mode = self._framing_mode()
        panel.mode_var.set(f"Курсор: {mode}")
        self.reset_crop_button.configure(state="disabled" if mode == "FIT_BLUR" else "normal")
        if self._crop_rect_id is not None:
            self.source_canvas.itemconfigure(self._crop_rect_id, state="hidden" if mode == "FIT_BLUR" else "normal")
        if mode == "FIT_BLUR":
            self._drag = None
            self.source_canvas.configure(cursor="")

    # -- playback state --------------------------------------------------- #
    def _current_time(self):
        return float(self.slider.get())

    def toggle_play(self):
        self._playing = not self._playing
        self.play_btn.configure(text="⏸ Pause" if self._playing else "▶ Play")

    def step_frame(self, delta):
        self._playing = False
        self.play_btn.configure(text="▶ Play")
        step = 1.0 / self.fps
        t = max(0.0, min(self.duration, self._current_time() + delta * step))
        self._set_time(t)

    def _on_slider(self, value):
        # Ignore callbacks caused by programmatic Scale updates.
        if self._setting_slider:
            return
        if self._programmatic_slider_value is not None:
            expected = self._programmatic_slider_value
            self._programmatic_slider_value = None
            if abs(float(value) - expected) < 1e-6:
                return
        # Пользовательский seek: вызывается только Tk при перемещении мышью.
        self._playing = False
        self.play_btn.configure(text="▶ Play")
        self._seek_to(float(value))

    def _seek_to(self, t):
        t = max(0.0, min(self.duration, t))
        self._update_time_label(t)
        index = self._current_segment_index()
        if index >= 0:
            seg = self.settings["segments"][index]
            if self._shown_segment_index is None or self._shown_segment_index != index:
                self._crop_px = None
                self._apply_crop_from_settings()
                self._update_crop_label()
            self._shown_segment_index = index
            self._selected_segment = index
        self._request_frame(t)
        self._update_timeline()
        if getattr(self, "framing_panel", None) is not None:
            self._sync_framing_state()
            self._refresh_preview()

    def _set_time(self, t):
        """Update timeline, timecode and frame without treating it as a user seek."""
        t = max(0.0, min(self.duration, t))
        self._programmatic_slider_value = t
        self._setting_slider = True
        try:
            self.slider.set(t)
        finally:
            self._setting_slider = False
        self._seek_to(t)

    def _update_time_label(self, t):
        self.time_label.configure(text=timecode(t))

    # -- decoding --------------------------------------------------------- #
    def _start_worker(self):
        if self._closed:
            return
        if av is None:
            self.info_label.configure(text="PyAV недоступен")
            return
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker.start()

    def _worker_loop(self):
        decoder = FrameDecoder(self.source_path)
        if not decoder.open():
            self._events.put(("error", "не удалось открыть видео"))
            return
        self._decoder = decoder
        self._events.put(("ready", decoder.fps))
        try:
            while not self._closed:
                item = self._commands.get()
                if item is None:
                    break
                kind = item[0]
                if kind == "get_frame":
                    # Keep the newest seek; do not spend seconds decoding old
                    # positions after a fast scrub or repeated frame stepping.
                    _, seq, t = item
                    while True:
                        try:
                            newer = self._commands.get_nowait()
                        except queue.Empty:
                            break
                        if newer is None:
                            return
                        if newer[0] == "get_frame":
                            _, seq, t = newer
                    try:
                        arr = decoder.request_frame(t, seq=seq)
                    except Exception as exc:
                        self._events.put(("error", f"ошибка декодирования: {exc}"))
                        self._events.put(("frame_failed", seq))
                        continue
                    if arr is not None:
                        self._events.put(("frame", seq, arr, decoder._last_frame_time))
                    else:
                        self._events.put(("frame_failed", seq))
        finally:
            decoder.close()

    # -- main-thread event handling --------------------------------------- #
    def _poll(self):
        if self._closed:
            return
        try:
            while True:
                event = self._events.get_nowait()
                try:
                    self._on_event(event)
                except Exception:
                    import traceback
                    traceback.print_exc(file=sys.stderr)
                    self._events.put(("error", "внутренняя ошибка при обработке кадра"))
        except queue.Empty:
            pass
        self._advance_playback()
        self._poll_id = self.after(POLL_MS, self._poll)

    def _on_event(self, event):
        kind = event[0]
        if kind == "ready":
            self.fps = float(event[1]) or self.fps
            self.slider.configure(to=self.duration)
            self.info_label.configure(
                text=f"{self.fps:.0f} fps · {self.duration:.2f} с")
        elif kind == "frame":
            _, seq, arr, frame_time = event
            if seq < self._seq:  # stale frame from a superseded request
                return
            self._frame_pending = False
            self._displayed_frame_local_time = (
                frame_time - self.start if frame_time is not None else None
            )
            if getattr(self, "framing_panel", None) is not None:
                self._framing_frame_time = self._displayed_frame_local_time
            self._render(arr)
        elif kind == "frame_failed":
            if event[1] == self._seq:
                self._frame_pending = False
        elif kind == "error":
            self.info_label.configure(text=event[1])

    def _request_frame(self, t):
        """Запросить кадр: локальное время кандидата -> абсолютное время исходника.

        Единственное место, где локальное время окна преобразуется в абсолютное
        (``local_to_absolute``), поэтому декодер всегда получает ``start + t``.
        Двойного прибавления ``start`` нет: сюда приходит локальное время, а не
        уже абсолютное. Ползунок/Play/Pause/таймкод/покадрово работают в
        локальном времени и сюда стекаются только через этот метод.
        """
        # While playing, allow one decode in flight. Otherwise a slow 4K
        # decode is *always* stale by the time the next 40 ms tick arrives.
        # Paused seeks and frame-step commands must still supersede it.
        if self._playing and self._frame_pending:
            return
        self._seq += 1
        self._displayed_frame_local_time = None
        self._frame_pending = True
        self._commands.put(("get_frame", self._seq, local_to_absolute(t, self.start)))

    def _advance_playback(self):
        if not self._playing or self._closed:
            return
        t = self._current_time() + POLL_MS / 1000.0
        if t >= self.duration:
            t = 0.0
            self._playing = False
            self.play_btn.configure(text="▶ Play")
        self._set_time(t)

    # -- rendering -------------------------------------------------------- #
    def _render(self, arr):
        try:
            import cv2
            from PIL import Image, ImageTk
        except Exception:
            self.info_label.configure(text="нет cv2/Pillow для предпросмотра")
            return
        h, w = arr.shape[:2]

        # Cap decode size for 4K sources. We keep the capped array cached so the
        # crop preview can be recomputed instantly on a mouse move (no re-decode).
        if w * h > MAX_DECODE_PIXELS:
            s = math.sqrt((w * h) / MAX_DECODE_PIXELS)
            arr = cv2.resize(arr, (int(w / s), int(h / s)), interpolation=cv2.INTER_AREA)
            h, w = arr.shape[:2]
        self._frame_cached = arr
        self._frame_size = (w, h)

        self._source_image = self._make_source_image(arr, cv2, Image, ImageTk)
        self._source_display = self._display_geometry(arr.shape[1], arr.shape[0])
        self._redraw_source()
        self._apply_crop_from_settings()
        self._update_crop_label()
        self._refresh_preview()

    def _make_source_image(self, arr, cv2, Image, ImageTk):
        h, w = arr.shape[:2]
        cw, ch = self._source_canvas_size()
        dw, dh = fit_size(w, h, cw, ch)
        small = cv2.resize(arr, (dw, dh), interpolation=cv2.INTER_AREA)
        img = Image.fromarray(small)
        return ImageTk.PhotoImage(img)

    def _display_geometry(self, fw, fh):
        cw, ch = self._source_canvas_size()
        return display_geometry(fw, fh, cw, ch)

    def _redraw_source(self):
        self.source_canvas.delete("all")
        if self._source_image is not None:
            cx, cy, dw, dh, _ = self._source_display
            self.source_canvas.create_image(cx + dw / 2, cy + dh / 2,
                                             image=self._source_image, anchor="center")
        self._crop_rect_id = self.source_canvas.create_rectangle(
            0, 0, 0, 0, outline="#00ff66", width=2, dash=(4, 2))
        self._position_crop_rect()

    def _crop_box_canvas(self):
        """Current crop box in canvas px: (x, y, w, h)."""
        if self._crop_px is None:
            cw, ch = self._source_canvas_size()
            return (0, 0, cw, ch)
        return self._crop_px

    def _position_crop_rect(self):
        if self._crop_rect_id is None:
            return
        x, y, w, h = self._crop_box_canvas()
        self.source_canvas.coords(self._crop_rect_id, x, y, x + w, y + h)
        self._sync_framing_state()

    def _update_crop_label(self):
        if self._crop_label is None:
            return
        seg = self._current_segment()
        if not seg:
            return
        c = seg["crop"]
        self._crop_label.configure(
            text=f"x {c['x']:.3f}  y {c['y']:.3f}  "
                 f"w {c['width']:.3f}  h {c['height']:.3f}"
                 f"{'   (ручной)' if seg.get('manual_override') else ''}")

    # -- crop editing (drag / zoom / reset) -------------------------------- #
    def _current_segment_index(self):
        return segment_at(self.settings, self._current_time())

    def _current_segment(self):
        index = self._current_segment_index()
        return self.settings["segments"][index] if index >= 0 else None

    def _display_to_frame(self, px, py):
        """Convert a canvas px point to (frame_x, frame_y, inside_frame)."""
        if self._source_display is None:
            return px, py, False
        cx, cy, dw, dh, scale = self._source_display
        fx = (px - cx) / scale
        fy = (py - cy) / scale
        inside = 0 <= fx <= dw and 0 <= fy <= dh
        return fx, fy, inside

    def _apply_crop_from_settings(self):
        seg = self._current_segment()
        if not seg:
            self._crop_px = None
            self._position_crop_rect()
            return
        if self._source_display is None or self._frame_size is None:
            # Frame has not been rendered yet; defer crop positioning.
            self._crop_px = None
            self._position_crop_rect()
            return
        cx, cy, dw, dh, _ = self._source_display
        fw, fh = self._frame_size
        sx, sy = dw / fw, dh / fh  # exact per-axis canvas scale (aspect preserved)
        x, y, w, h = crop_pixels(seg["crop"], fw, fh)
        self._crop_px = (cx + x * sx, cy + y * sy, w * sx, h * sy)
        self._position_crop_rect()

    def _crop_box_to_normalized(self, box):
        """Convert a canvas-px crop box back to normalized source coordinates."""
        cx, cy, dw, dh, _ = self._source_display
        fw, fh = self._frame_size
        sx, sy = dw / fw, dh / fh  # exact per-axis canvas scale (aspect preserved)
        nx = (box[0] - cx) / sx / fw
        ny = (box[1] - cy) / sy / fh
        nw = box[2] / sx / fw
        nh = box[3] / sy / fh
        return nx, ny, nw, nh

    def _commit_crop(self, normalized):
        if self._framing_mode() == "FIT_BLUR":
            return
        index = self._current_segment_index()
        if index < 0:
            return
        self.settings = set_segment_crop(self.settings, index, normalized)
        self._crop_px = None
        self._apply_crop_from_settings()
        self._update_crop_label()
        self._refresh_preview()

    def _clamp_source_box(self, x, y, ww, hh):
        """Clamp a crop box (canvas px) to the letterboxed source image area."""
        if self._source_display is None:
            cw, ch = self._source_canvas_size()
            return clamp_drag(x, y, ww, hh, cw, ch)
        return clamp_box_in_area(x, y, ww, hh, self._source_display)

    def _on_drag_start(self, event):
        if self._framing_mode() == "FIT_BLUR":
            return
        if self._source_display is None:
            return
        x, y, w, h = self._crop_box_canvas()
        if x <= event.x <= x + w and y <= event.y <= y + h:
            self._drag = (event.x - x, event.y - y)
            self.source_canvas.itemconfig(self._crop_rect_id, dash=())
            self.source_canvas.configure(cursor="fleur")

    def _on_drag_move(self, event):
        if self._framing_mode() == "FIT_BLUR":
            return
        if not self._drag:
            return
        dx, dy = self._drag
        x = event.x - dx
        y = event.y - dy
        _, _, w, h = self._crop_box_canvas()
        x, y, w, h = self._clamp_source_box(x, y, w, h)
        self._crop_px = (x, y, w, h)
        self._position_crop_rect()
        nx, ny, nw, nh = self._crop_box_to_normalized(self._crop_px)
        self._commit_crop(clamp_crop(nx, ny, nw, nh, *self._frame_size))

    def _on_drag_end(self, event):
        if self._drag:
            self._drag = None
            self.source_canvas.itemconfig(self._crop_rect_id, dash=(4, 2))
            self.source_canvas.configure(cursor="")

    def _on_zoom(self, event):
        if self._framing_mode() == "FIT_BLUR":
            return
        if self._source_display is None:
            return
        delta = event.delta if hasattr(event, "delta") else (1 if getattr(event, "num", 0) < 0 else -1)
        factor = 1.1 if delta > 0 else (1 / 1.1)
        x, y, w, h = self._crop_box_canvas()
        # Zoom about the cursor, or about the box centre if the cursor is outside.
        if x <= event.x <= x + w and y <= event.y <= y + h:
            ox, oy = event.x, event.y
        else:
            ox, oy = x + w / 2, y + h / 2
        nw = w * factor
        nh = h * factor
        nx = ox - (ox - x) * factor
        ny = oy - (oy - y) * factor
        nx, ny, nw, nh = self._clamp_source_box(nx, ny, nw, nh)
        nxn, nyn, nwn, nhn = self._crop_box_to_normalized((nx, ny, nw, nh))
        self._commit_crop(clamp_crop(nxn, nyn, nwn, nhn, *self._frame_size))

    def _refresh_preview(self):
        """Recompute the 9:16 preview from the cached frame (no re-decode).

        The crop is scaled with its original aspect and centered in the canvas.
        No stretching or overflow, including for legacy saved crops.
        """
        if self._frame_cached is None:
            return
        try:
            from PIL import Image, ImageTk
        except Exception:
            return
        frame_time = getattr(self, "_framing_frame_time", None)
        if self._framing_mode(frame_time) == "FIT_BLUR":
            from framing_panel import fit_blur_preview
            pcw, pch = self._preview_canvas_size()
            size = fit_size(9, 16, pcw, pch)
            preview = fit_blur_preview(Image.fromarray(self._frame_cached), size)
            self._preview_image = ImageTk.PhotoImage(preview)
            self.preview_canvas.delete("all")
            self.preview_canvas.create_image(pcw // 2, pch // 2, image=self._preview_image, anchor="center")
            return
        seg = self._current_segment()
        if not seg:
            return
        x, y, w, h = crop_pixels(seg["crop"], *self._frame_size)
        crop = self._frame_cached[y:y + h, x:x + w]
        if not crop.size:
            return
        ch, cw = crop.shape[:2]
        pcw, pch = self._preview_canvas_size()
        # Preserve the crop's real pixel aspect. A 9:16 crop fills a portrait
        # preview; a legacy/manual non-9:16 crop is never distorted.
        pw, ph = fit_size(cw, ch, pcw, pch)
        preview = Image.fromarray(crop).resize((pw, ph), Image.LANCZOS)
        self._preview_image = ImageTk.PhotoImage(preview)
        cx = pcw // 2
        cy = pch // 2
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(cx, cy, image=self._preview_image, anchor="center")

    def reset_crop(self):
        if self._framing_mode() == "FIT_BLUR":
            return
        index = self._current_segment_index()
        if index < 0:
            return
        fw, fh = self._frame_size or (1, 1)
        self.settings = set_segment_crop(self.settings, index, centered_crop_916(fw, fh))
        self._crop_px = None
        self._apply_crop_from_settings()
        self._update_crop_label()
        self._refresh_preview()

    def split_current_segment(self):
        """Разделить текущий сегмент по позиции playhead'а."""
        self._playing = False
        self.play_btn.configure(text="▶ Play")
        try:
            if self._frame_pending:
                raise ValueError("Дождитесь загрузки кадра перед разделением")
            t = split_time_for_displayed_frame(
                self._current_time(), self._displayed_frame_local_time, self.duration
            )
            index = segment_at(self.settings, t)
            # split_segment: new settings with the segment split at t.
            self.settings = split_segment(self.settings, t)
            # The right part keeps the crop and manual_override of the original.
            # Recompute its index (segments list changed): it's the first whose
            # start == t; fall back to the index just after the old one.
            new_index = index + 1
            for i, s in enumerate(self.settings["segments"]):
                if abs(s["start"] - t) < 1e-6:
                    new_index = i
                    break
            self._selected_segment = new_index
            # Force _seek_to to treat this as a new segment (apply crop + preview),
            # even though the time (and thus the raw segment_at) is unchanged.
            self._shown_segment_index = -1
            self._seek_to(t)
            self._draw_timeline()
        except Exception as exc:
            messagebox.showwarning(
                "Разделить",
                f"Не удалось разделить сегмент:\n{exc}",
                parent=self)

    def merge_current_segment(self):
        """Объединить выбранный сегмент со следующим."""
        self._playing = False
        self.play_btn.configure(text="▶ Play")
        index = self._selected_segment
        seg = self.settings["segments"][index] if 0 <= index < len(self.settings["segments"]) else None
        if not seg:
            messagebox.showwarning(
                "Объединить",
                "Нет выбранного сегмента.",
                parent=self)
            return
        if index >= len(self.settings["segments"]) - 1:
            messagebox.showwarning(
                "Объединить",
                "Нечего объединять: выбран последний сегмент.",
                parent=self)
            return
        t = self._current_time()
        try:
            # merge_segments: new settings with segments[index] + [index+1] merged.
            self.settings = merge_segments(self.settings, index)
            # The merged segment takes the index of the left one.
            self._selected_segment = index
            # Keep the playhead inside the merged segment, preserving position.
            t = min(max(0.0, t), self.settings["segments"][index]["end"])
            self._shown_segment_index = -1
            self._seek_to(t)
            self._draw_timeline()
        except Exception as exc:
            messagebox.showwarning(
                "Объединить",
                f"Не удалось объединить сегменты:\n{exc}",
                parent=self)

    def _on_canvas_configure(self, event=None):
        # Source canvas was resized: re-center the image and the crop box.
        cw, ch = self._source_canvas_size()
        self._canvas_size = (cw, ch)
        if self._source_image is not None and self._frame_size is not None:
            self._source_display = self._display_geometry(*self._frame_size)
            # Rebuild the display image at the new size (aspect preserved).
            try:
                import cv2
                from PIL import Image, ImageTk
            except Exception:
                return
            dw, dh = fit_size(*self._frame_size, cw, ch)
            small = cv2.resize(self._frame_cached, (dw, dh), interpolation=cv2.INTER_AREA)
            self._source_image = ImageTk.PhotoImage(Image.fromarray(small))
            # Redraw the source image AND the crop box, then reposition the box
            # (order matters: _redraw_source resets the rect to 0,0,0,0).
            self._redraw_source()
            self._apply_crop_from_settings()
        self._refresh_preview()
        self._update_timeline()

    # -- segment timeline ------------------------------------------------- #
    def _timeline_segment_index(self, time):
        """Segment index at local ``time`` from the (already validated) settings.

        No full re-validation: segments are sorted, non-overlapping, and cover
        [0, duration], so a plain scan is enough.
        """
        segments = self.settings["segments"]
        n = len(segments)
        if not segments or time < 0 or time > self.duration + 1e-6:
            return -1
        for i, seg in enumerate(segments):
            if seg["start"] <= time < seg["end"] or (i == n - 1 and time <= seg["end"] + 1e-6):
                return i
        return -1

    def _draw_timeline(self):
        """Redraw the segment strip and the playhead."""
        canvas = self.timeline_canvas
        canvas.delete("all")
        width = max(1, int(canvas.winfo_width()))
        height = TIMELINE_H
        canvas.configure(width=width, height=height)
        segments = self.settings["segments"]
        duration = self.duration
        rect_ids = []
        for i, seg in enumerate(segments):
            x1 = seg["start"] / duration * width
            x2 = seg["end"] / duration * width
            if i == self._selected_segment:
                fill = "#2d6ca3"
                outline = "#ffffff"
            else:
                fill = "#262626"
                outline = "#444444"
            rect = canvas.create_rectangle(x1, 2, x2, height - 2, fill=fill,
                                           outline=outline, width=1)
            rect_ids.append(rect)
            if x2 - x1 > 18:
                canvas.create_text((x1 + x2) / 2, height / 2, text=str(i + 1),
                                   fill="#ffffff")
        self._timeline_rect_ids = rect_ids
        panel = getattr(self, "framing_panel", None)
        if panel is not None:
            for item in panel.draft["intervals"]:
                canvas.create_rectangle(item["start"] / duration * width, height - 9,
                                        item["end"] / duration * width, height - 2,
                                        fill="#d89435", outline="", tags="fit_blur")
        t = float(self.slider.get())
        px = min(max(t / duration, 0.0), 1.0) * width
        canvas.create_line(px, 0, px, height, fill="#ff5555", width=2,
                           tags="playhead")

    def _update_timeline(self):
        """Move the playhead without re-validating settings."""
        width = max(1, int(self.timeline_canvas.winfo_width()))
        height = TIMELINE_H
        duration = self.duration
        t = float(self.slider.get())
        px = min(max(t / duration, 0.0), 1.0) * width
        ids = self.timeline_canvas.find_withtag("playhead")
        if ids:
            self.timeline_canvas.coords(ids[-1], px, 0, px, height)

    def _on_timeline_click(self, event):
        width = max(1, int(self.timeline_canvas.winfo_width()))
        if (getattr(self, "framing_panel", None) is not None
                and TIMELINE_H - 9 <= event.y <= TIMELINE_H - 2):
            self._playing = False
            self.play_btn.configure(text="▶ Play")
            self._set_time(max(0.0, min(1.0, event.x / width)) * self.duration)
            return
        index = timeline_segment_at(
            self.settings["segments"], self.duration, event.x, width)
        if index < 0:
            return
        seg = self.settings["segments"][index]
        self._selected_segment = index
        self._playing = False
        self.play_btn.configure(text="▶ Play")
        self._set_time(seg["start"])

    # -- lifecycle -------------------------------------------------------- #
    def close(self):
        if self._closed:
            return
        self._closed = True
        self._playing = False
        try:
            self._commands.put(None)  # stop the worker
        except Exception:
            pass
        if self._worker_after_id is not None:
            try:
                self.after_cancel(self._worker_after_id)
            except Exception:
                pass
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except Exception:
                pass
        try:
            self.destroy()
        except Exception:
            pass
