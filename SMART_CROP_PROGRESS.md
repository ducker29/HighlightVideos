# Smart Crop Editor — Progress

## Архитектурные решения

### 1. Архитектура отдельного окна: Tkinter Toplevel

**Выбор:** `Tkinter Toplevel` внутри основного процесса.

**Обоснование:**
- `review_ui.py` уже использует Tkinter с `Notebook`, `Treeview` и queue-based event loop (`self.events`).
- `Toplevel` позволяет разделять UI без дублирования состояния приложения (отчёт, кандидаты, manifest).
- `subprocess` потребовал бы IPC (JSON, named pipe), дублирования загрузки отчёта и усложнит синхронизацию изменений границ.
- Для ручного ревью (не background-обработка) `Toplevel` достаточен; тяжёлые операции (ASR/LLM) уже вынесены в отдельные модули и не выполняются в редакторе кадрирования.

### 2. Где хранятся утверждённые Shorts

- Кандидаты Shorts хранятся в `report["shorts_candidates"]` внутри `report.json`:
  - Пример: `data/chFk6Jp_n2w/analysis/tail_v1_4f653d9c4a0231d19178/report.json`.
- Границы утверждённого Shorts:
  - `candidate_id`
  - `start`, `end`
  - `duration_sec`
  - `approval_key` (считается через `shorts_approval_key()`)
  - `render_settings` (для Shorts; в старых отчётах может отсутствовать)
- Источниковое видео: `data/{video_id}/source.mp4` (через manifest).
- Текущие настройки crop:
  - `render_settings.crop_mode` (`center`, `speaker`, `auto`)
  - `render_settings.crop_x` (`None` или `0..1`)
  - `render_settings.subtitles_enabled`
  - `render_settings.output_width` / `output_height`

### 3. Новая JSON-схема Smart Crop

Для сохранения ключевых кадров кадрирования создаётся отдельный файл, не изменяющий существующие артефакты:

**Путь:**
`data/{video_id}/analysis/{signature}/smart_crop_{candidate_id}.json`

**Схема:**
```json
{
  "schema": 1,
  "candidate_id": "sc001",
  "video_id": "chFk6Jp_n2w",
  "signature": "tail_v1_4f653d9c4a0231d19178",
  "source_width": 1920,
  "source_height": 1080,
  "keyframes": [
    {
      "time_sec": 0.0,
      "crop_mode": "left",
      "crop_x": 0.25,
      "note": ""
    },
    {
      "time_sec": 12.5,
      "crop_mode": "right",
      "crop_x": 0.75,
      "note": ""
    }
  ],
  "updated_at": "2026-11-10T12:00:00Z"
}
```

**Поля:**
- `schema`: версия схемы (сейчас `1`).
- `candidate_id`: идентификатор Shorts-кандидата.
- `video_id`, `signature`: привязка к отчёту.
- `source_width` / `source_height`: разрешение источника для валидации.
- `keyframes`: отсортированный по `time_sec` список; `time_sec` в диапазоне `[0, duration]`.
- `crop_mode`: `left`, `center`, `right`, `auto` или `custom`.
- `crop_x`: `None` или нормированное значение `0..1`; при `crop_mode != custom` может быть `None`.
- `note`: комментарий пользователя.
- `updated_at`: ISO 8601 время последнего сохранения.

**Валидация:**
- `keyframes` не пуст.
- Первое значение `time_sec` равно `0`.
- `time_sec` не уменьшается.
- `crop_x` в `[0, 1]`, если задано.
- `source_width` / `source_height` совпадают с текущим медиа (проверяется при открытии).

### 4. Компоненты

**SmartCropWindow**
- `Toplevel` окно.
- Виджет предпросмотра вертикального кадра.
- Таймлайн с ключевыми кадрами.
- Кнопки: `Добавить`, `Удалить`, `Сохранить`, `Закрыть`.
- Поля ввода: `time_sec`, `crop_mode`, `crop_x`, `note`.

**SmartCropStorage**
- Чтение/запись `smart_crop_{candidate_id}.json`.
- Валидация схемы и границ.
- Не трогает `report.json`.

**SmartCropRenderer** (позднее)
- Читает `smart_crop_{candidate_id}.json`.
- Генерирует `crop` filter-граф для FFmpeg.
- Интегрируется в `renderer.py` только после утверждения схемы.

## План из 7 задач

| # | Задача | Статус | Результат |
|---|--------|--------|-----------|
| 1 | Изучить архитектуру и определить место для Smart Crop | **done** | `SMART_CROP_PROGRESS.md` |
| 2 | Реализовать `SmartCropStorage` | **done** | `smart_crop_storage.py`, `test_smart_crop_storage.py` (35 тестов OK) |
| 3 | Реализовать `SmartCropWindow` (Toplevel) | **done** | `smart_crop_window.py`, `test_smart_crop_window.py` |
| 3b | Интерактивное редактирование crop (drag/zoom/reset) | **done** | drag мышью, зум колесом, «Сбросить crop», label координат, кеш кадра (28 тестов окна OK) |
| 3c | Исправление вечной загрузки (разделение очередей) | **done** | `_commands` (GUI→worker) / `_events` (worker→GUI); 4 регрессионных теста; 32 теста OK |
| 3d | Исправление центрирования, 9:16-предпросмотра и тайминга по кандидату | **done** | см. «Итоги задачи 3d» ниже; 50 тестов `test_smart_crop_window` OK |
| 3e | Исправление 3 падающих GUI-тестов (геометрия Canvas / preview 9:16) | **done** | см. «Итоги задачи 3e» ниже; 50 тестов `test_smart_crop_window` OK |
| 4 | Интегрировать кнопку открытия редактора в `review_ui.py` | done | кнопка «Смарт-кроп», `open_smart_crop()` |
| 5 | Реализовать `SmartCropRenderer` | pending | Интеграция в `renderer.py` |
| 6 | Написать тесты и проверить сценарии | pending | `test_smart_crop.py` |
| 7 | Обновить документацию | pending | `LOCAL_SETUP.md`, `Highlight_Video_Pipeline.md` |

## Статус

**Текущая задача:** 3e завершена — исправлены 3 падающих GUI-теста (геометрия Canvas, resize, 9:16 preview).

**Следующая задача:** 5/7 — `SmartCropRenderer` (интеграция в `renderer.py`).

### Итоги задачи 3e (исправление 3 падающих GUI-тестов)

**Симптом:** три GUI-теста падали с ошибками `bh=0.5625 != 1.0`, `dw=1 != 800`, `pw=300 != 225`.

**Причина (все три — ошибки тестов, не дефекты кода):**
1. `test_crop_box_matches_default_segment` — канвас не размещён (`winfo_width()=1`), `dw=dh=1`, crop-box вычисляется в 1×1 пространстве. Тест не мокал размеры канваса (в отличие от соседних GUI-тестов).
2. `test_configure_recenters_after_resize` — `_setup_display` вызывал `_display_geometry` до мокирования `winfo_width/height`, поэтому геометрия считалась для 1×1. Мокировались после, но `_on_canvas_configure` не пересчитывал из-за `_source_image is None`.
3. `test_preview_frame_is_916_not_stretched` — для Canvas 300×533 изображение 300×533 допустимо и практически 9:16. Тест требовал фиксированный размер 225×400 вместо проверки соотношения сторон.

**Исправления** (только `test_smart_crop_window.py`, без изменений в `smart_crop_window.py`):
- `test_crop_box_matches_default_segment`: добавлены моки `winfo_width=640`, `winfo_height=360` перед `_setup_display`.
- `test_configure_recenters_after_resize`: моки `winfo_width=900`, `winfo_height=450` перенесены перед `_setup_display`.
- `test_preview_frame_is_916_not_stretched`: убраны жёсткие `assertEqual(pw, 225)` / `assertEqual(ph, 400)`; оставлена проверка `pw/ph ≈ 9/16` и границ Canvas.

**Результат:** 50 тестов `test_smart_crop_window` OK.

### Итоги задачи 3d (центрирование, 9:16-предпросмотр, тайминг по кандидату)

**Изменённые файлы:** `smart_crop_window.py`, `review_ui.py`, `test_smart_crop_window.py`, `test_shorts_ui.py` (не изменён: `renderer.py`, `report.json`, approvals).

**1. Исходное видео 16:9 — центрирование.**
- Изображение кадра теперь рисуется через `anchor="center"` в координатах центра `(cx + dw/2, cy + dh/2)`; `fit_size` сохраняет пропорции 16:9 без растяжения.
- Crop-рамка строится от той же геометрии (`_apply_crop_from_settings`), поэтому совпадает с изображением после центрирования.
- Обработка мыши (`_on_drag_start`/`_on_drag_move`/`_on_zoom`, `_display_to_frame`, `_crop_box_to_normalized`) использует смещение и масштаб изображения (`_source_display = (cx, cy, dw, dh, scale)`).
- Новый модульный хелпер `display_geometry(fw, fh, max_w, max_h)` (чистый, тестируется без дисплея) + `clamp_box_in_area(x, y, ww, hh, area)` — drag/zoom зажимает рамку в пределах отцентрованного изображения, а не всего канваса (метод `_clamp_source_box`).

**2. Вертикальный предпросмотр 9:16.**
- `_refresh_preview` больше не растягивает вырез в фиксированные 300×533: `fit_size(crop_w, crop_h, PREVIEW_W, PREVIEW_H)` сохраняет пропорции 9:16, изображение центрируется через `anchor="center"`, целиком помещается в область.

**3. Тайминг Shorts — для ЛЮБОГО кандидата.**
- `review_ui.py` → `open_smart_crop()`: `start`/`end` берутся из данных выбранного кандидата в таблице Shorts (`current_shorts_candidate()` — `report["shorts_candidates"]`), передаются в `SmartCropWindow(start=..., end=...)` без повторного чтения `report.json` внутри окна и без хардкода.
- Внутри окна используется локальное время кандидата: `local_time ∈ [0, end - start]`; ползунок, Play/Pause (`_advance_playback`), переход по кадрам (`step_frame`) и таймкод работают в локальном времени.
- Декодер получает абсолютное время исходника: `absolute_time = start + local_time` (хелпер `local_to_absolute`); обратное преобразование — `absolute_to_local`. Двойного прибавления `start` нет (локальное 0 → `start`, а не `2*start`).
- При выборе другого Shorts открывается редактор с его собственными `start`/`end`; состояние предыдущего кандидата (включая тайминг, `_shown_segment_index`, `_crop_px`) не сохраняется между окнами.

**Тесты:**
- `test_smart_crop_window.py`:
  - `TestTimeConversion` — `local_to_absolute`/`absolute_to_local` для двух кандидатов с разными таймингами (sc001: 857.68–906.2 как тестовый пример; второй: 1200.0–1230.0), round-trip, отсутствие двойного смещения.
  - `TestFitSize.test_centered_geometry_16_9` / `test_centered_geometry_widescreen_letterboxed` / `test_centered_geometry_portrait_letterboxed_h` — центрирование и letterbox в `display_geometry`.
  - `test_box_clamped_to_letterboxed_image` — рамка зажимается в пределы отцентрованного изображения.
- `test_shorts_ui.py` не изменён (существующие проверки `open_smart_crop` и ползунка прошли).

**Результат:** `test_smart_crop_window` + `test_shorts_ui` — 62 теста OK (включая GUI-тесты на дисплее). Полный набор не запускался; реальный GUI пользователь проверяет вручную (sc001: 857.68–906.2 с).

### Итоги задачи 3c (исправление вечной загрузки)

**Симптом:** окно Smart Crop открывалось, но предпросмотр оставался чёрным; `FrameDecoder.request_frame()` никогда не вызывался.

**Причина:** `_poll()` (GUI) и `_worker_loop()` (worker) читали из одной и той же очереди `_events`. GUI вытаскивал сообщения `get_frame`, предназначенные worker'у, а worker вытаскивал `ready`/`frame`, предназначенные GUI. В результате ни один поток не получал своё сообщение.

**Исправление** (в `smart_crop_window.py`):
- Добавлена очередь `_commands` (`queue.Queue()`) для сообщений GUI→worker (`get_frame`, `None`).
- Очередь `_events` осталась только для worker→GUI (`ready`, `frame`, `error`).
- `_request_frame()` кладёт запрос в `_commands`.
- `_worker_loop()` читает из `_commands`, пишет результаты в `_events`.
- `_poll()` читает из `_events` (без изменений в логике).
- `close()` шлёт `None` в `_commands` (остановка worker) и отменяет отложенный `after(50, _start_worker)` через сохранённый ID `_worker_after_id`.
- Удалён весь диагностический код (`_diag`, `SMART_CROP_DEBUG`, `import os`, `import time`).

**Регрессионные тесты** (`test_smart_crop_window.py`, класс `TestQueueSeparation`):
- `test_commands_and_events_are_distinct_queues` — очереди не совпадают по объекту.
- `test_request_frame_goes_to_commands_not_events` — запрос идёт в `_commands`, `_events` остаётся пустой.
- `test_worker_reads_from_commands_not_events` — `_worker_loop` читает `self._commands.get()`, а не `self._events.get()`.
- `test_close_sends_stop_to_commands` — `close()` кладёт `None` в `_commands`.

**Результат:** 32 теста `test_smart_crop_window.py` — все OK.

### Итоги задачи 3b (интерактивное редактирование crop)

В `smart_crop_window.py` добавлено редактирование crop-рамки мышью.

**Механика:**
- **Drag** — захват зелёной рамки (`<ButtonPress-1>`), перемещение (`<B1-Motion>`), отпустил — рамка встаёт. Рамка не выходит за границы канваса (`clamp_drag`), курсор `fleur` во время перетаскивания, пунктир рамки становится сплошным.
- **Zoom** — колесо мыши (`<MouseWheel>` / `<Button-4>` / `<Button-5>`) масштабирует рамку относительно курсора (или центра, если курсор снаружи). Соотношение 9:16 фиксируется.
- **«Сбросить crop»** — возвращает сегменту центрированный 9:16 crop по короткой стороне (`centered_crop_916`), помечает `manual_override`.
- **Label** рядом с кнопками показывает нормализованные `x / y / w / h` текущего crop и пометку «(ручной)», если сегмент изменён вручную.
- **Кеш кадра** — последнее декодированное (упавшее в `MAX_DECODE_PIXELS`) изображение хранится в `self._frame_cached`; при drag/zoom предпросмотр 9:16 пересчитывается из кеша мгновенно, без повторного декодирования.
- Каждое изменение пишет `set_segment_crop(settings, index, normalized)` (валидация + `manual_override=True`) и перерисовывает рамку + предпросмотр.

**Геометрия (чистые хелперы, тестируются без дисплея):**
- `clamp_crop(x, y, w, h, ar=9/16)` — нормализованный 9:16 crop, зажатый в [0,1], мин. размер `_MIN_CROP`, aspect фиксирован.
- `centered_crop_916(width, height)` — центрированный 9:16 по короткой стороне (фолбэк crop).
- `clamp_drag(x, y, w, h, canvas_w, canvas_h)` — рамка целиком в пределах канваса.

**Letterbox-преобразования** (исходник 16:9 рисуется в канвас 640×360 с сохранением aspect):
- `_display = (cx, cy, dw, dh, scale)` — позиция/размер отображаемого кадра.
- `_display_to_frame` / `_crop_box_to_normalized` — canvas px ⇄ frame px ⇄ нормализованные координаты.
- Рамка рисуется по `_display`, crop хранится нормализованно — при смене времени/сегмента рамка берёт crop своего сегмента (`_set_time` отслеживает `_shown_segment_index`).

**Тесты:** добавлены `TestClampCrop`, `TestCenteredCrop916`, `TestClampDrag` (чистые) и GUI-тесты `test_crop_box_matches_default_segment`, `test_drag_moves_crop_and_sets_manual_override`, `test_reset_crop_restores_centered`, `test_commit_crop_persists_normalized_box`. Итого 28 тестов окна; весь discovery — 256 OK (1 skipped).

### Итоги задачи 3/7

- Создан `smart_crop_window.py`:
  - `SmartCropWindow(master, source_path, start, end, candidate_id, settings, own_toplevel=False)` — `ttk.Frame`, может сам создавать `Toplevel` (`own_toplevel=True`) или встраиваться в существующий `root`.
  - Слева исходный кадр 16:9 (`Canvas` 640×360), справа вертикальный предпросмотр 9:16 (300×533).
  - На исходном кадре — зелёная рамка crop текущей области (масштабируется из пространства декодированного кадра в пиксели канваса).
  - Снизу ползунок времени + таймкод (`job_store.timecode`), кнопки Play/Pause и переход по кадрам (1/fps).
  - Текущий сегмент берётся через `segment_at()`; crop — нормализованные координаты.
  - Декодирование через PyAV (`av`, уже в `requirements.txt`) на фоновом потоке; GUI обновляется в главном потоке через очередь + `after()`.
  - Быстрый скраб: запросам присваивается `seq`, устаревшие кадры (`seq < current`) отбрасываются.
  - Ограничение размера декодирования `MAX_DECODE_PIXELS` для 4K (crop-математика точная, в пикселях исходника).
  - `close()` — идемпотентный: останавливает worker, отменяет `after()`, освобождает контейнер PyAV.
  - Чистые хелперы: `crop_pixels`, `fit_size`, `segment_for`, `FrameDecoder`.
- Тесты: 15 в `test_smart_crop_window.py` (геометрия, сегменты, жизненный цикл декодера). GUI-тест вынесен отдельно и пропускается без дисплея. В сумме со storage — 50 тестов OK.

### Итоги задачи 2/7

- Создан `smart_crop_storage.py` с API:
  - `create_settings(candidate_id, source_start, source_end, source_width, source_height, segments=None)`
  - `load_settings(path)` → `(settings, warnings)`
  - `save_settings(settings, path)` (атомарно через `.tmp` + `os.replace`)
  - `validate_settings(settings)` / `validate_segments(segments, duration)`
  - `split_segment(settings, time, ...)` / `merge_segments(settings, index)`
  - `set_segment_target` / `set_segment_crop` / `clear_manual_override`
  - `segment_at(settings, time)` → индекс
  - `settings_path(job_dir, signature, candidate_id)` → путь к файлу
- Нормализованные crop-координаты `x, y, width, height` в `[0, 1]` (работает с 720p и 4K).
- Сегменты: полуоткрытые интервалы `[start, end)`, покрывают `[0, duration]`, без пересечений.
- `manual_override` сохраняется при load/save; ручные изменения имеют приоритет.
- Обработка старых/повреждённых файлов: `SmartCropError`, warning при `schema_version != 1`.
- Тесты: 35 unit-тестов, все проходят (`python -m unittest test_smart_crop_storage`).

## Файлы, которые потребуется создать или изменить

### Новые файлы
- `smart_crop_storage.py` — хранение и валидация настроек Smart Crop. **[создан]**
- `smart_crop_window.py` — `Toplevel` окно редактора. **[создан]**
- `smart_crop_renderer.py` — генерация FFmpeg-команд.
- `test_smart_crop.py` — тесты.
- `test_smart_crop_storage.py` — unit-тесты для storage. **[создан]**
- `test_smart_crop_window.py` — unit/GUI-тесты для окна. **[создан]**

### Изменения существующих файлов
- `renderer.py`:
  - `shorts_approval_key()`: учесть `smart_crop_file` или ключевые кадры, чтобы изменение кадрирования снимало утверждение.
  - `render_shorts_approved()`: использовать `smart_crop_renderer` при наличии сохранённых ключевых кадров.
- `review_ui.py`:
  - Добавить кнопку «Smart Crop» в таблицах Shorts.
  - Обработчик открытия `SmartCropWindow`.
  - Обновление `approval_key` после сохранения настроек.
- `job_store.py`:
  - При необходимости, константы путей/имён файлов Smart Crop.

## Ограничения текущей задачи

- Не изменялись `renderer.py`, `review_ui.py`, `job_store.py`, `shorts_approval_key()`.
- Не изменён статус утверждения Shorts.
- Не добавлялось редактирование crop мышью, редактирование интервалов и автораспознавание планов.
- Не запускался полный рендер.
- Не устанавливались новые зависимости (использован PyAV, уже в `requirements.txt`).
- Не сделан git commit.
- Не переходили к задаче 4/7.
