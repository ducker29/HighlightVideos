Всё необходимое я прочитал. Итоги, влияющие на план:
- renderer.py работает только через manifest["source_path"] (рендер хайлайтов и Shorts), плюс probe(source_path) — значит переключение качества = смена source_path+media в манифесте, ничего больше менять не надо.
- yt-dlp 2026.10.26, есть extract_info(download=False), progress_hooks.
- Существующий кеш data/<id>/source.mkv нужно мигрировать в новую схему.
ФИНАЛЬНЫЙ ПЛАН (к реализации)
1. Новая схема хранения
data/<video_id>/
├── source.json              # video_id, url, title, active_height, source_path, media, qualities{...}
├── sources/
│   ├── 1080p.mkv            # каждое качество отдельным файлом
│   ├── 1440p.mkv
│   └── 2160p.mkv
└── (старые analysis/, output/, transcript.json — не трогаем)
Миграция при первом обращении: если есть старый data/<id>/source.mkv без sources/ — переносим его в sources/<H>p.mkv и дополняем манифест (qualities, active_height, source_path). Старый корневой файл удаляем только после успешного переноса (или оставляем как есть при ошибке — отчёты не ломаем).
2. Бэкенд youtube_download.py — новые функции
- list_available_qualities(url, log) → yt_dlp extract_info(download=False), группировка по высоте: {height: {"format_id": "...", "fps":..., "tbr":..., "adaptive": bool, "has_audio": bool}}. Адаптивные (раздельные v/a) разрешаем (A1).
- download_quality(url, expected_id, height, root, log, on_progress) → скачивает bestvideo[height=H][vcodec!=none]+bestaudio/best[acodec!=none] в sources/<H>p.mkv, при необходимости мультиплексирует ffmpeg (видео+аудио) в sources/<H>p.mkv. Возвращает (path, media=probe(...)).
- publish_quality(manifest, height, media, backend, fmt_id, ...) → обновляет qualities[height], active_height=height, source_path, media; сохраняет source.json.
- set_active_quality(manifest_path, height) → переключает активное (проверка, что файл существует).
- migrate_legacy_source(job_dir, log) → миграция старого source.mkv.
- Старые select_combined_format / download оставляем для backward-compat (тесты зелёные).
3. Стейт-машина (единый модуль quality_state.py)
NEUTRAL ──click──► DOWNLOADING ──ok──► DOWNLOADED
                          └──err──► NEUTRAL
DOWNLOADED ──click──► SELECTED
SELECTED ──click──► DOWNLOADED
(выбор другого SELECTED ⇒ бывший SELECTED → DOWNLOADED)
QualityTableController: словарь height→state, референсы кнопок, блокировка всех кнопок при любом DOWNLOADING, гарантия одного SELECTED, публичные click(height), set_state(h,st), block_all(bool), selected().
4. UI: quality_window.py (QualityDialog)
- ttk.Treeview со столбцами [Разрешение, FPS, Состояние, Кнопка].
- В колонке «Кнопка» — реальный ttk.Button (через child_window), подпись = состояние: не загружено / загружается / загружено / выбрано. Клик = переход состояния (B1).
- Кнопка «Проверить качество» → list_available_qualities в фоновом треде → заполняет таблицу.
- Кнопки «Ок» / «Отмена» (C1: выбор применяется сразу при клике, кнопки просто закрывают окно).
- transient к основному, без grab_set (автономен), собственный queue.Queue + after-поллинг как в HighlightApp._poll.
- Выход через колбэк on_done(height_or_None, manifest) в основной UI.
5. Интеграция в review_ui.py
- Рядом с «Вставить» — кнопка «Загрузить» (всегда видна; без ссылки — сообщение).
- open_quality_window(): создаёт QualityDialog (повторное открытие — поднять), хранит в self.quality_window.
- _on_quality_done(height, manifest): применяет active_height к манифесту, обновляет self.context["manifest"], если контекст уже открыт.
- Не трогаем self.busy/start_worker — диалог автономен.
6. Тесты
- test_youtube_download.py:
- list_available_qualities (группировка по высоте, флаг adaptive).
- download_quality → файл в sources/, манифест обновлён.
- set_active_quality → source_path/media поменялись, старые файлы не тронуты.
- Миграция старого source.mkv.
- Новый test_quality_state.py: переходы, блокировка, инвариант «одно выбрано».
- Новый test_quality_window.py: создание диалога, заполнение таблицы, клик по кнопке меняет подпись и состояние (mock-загрузка).
7. Порядок реализации (блоки, каждый с подтверждением)
1. Блок 1 — Бэкенд: новая схема хранения + list_available_qualities + download_quality + publish_quality + set_active_quality + миграция. Тесты test_youtube_download.py расширяем.
2. Блок 2 — Стейт-машина: quality_state.py + юнит-тесты.
3. Блок 3 — Диалог: quality_window.py (QualityDialog + QualityTableController) + тесты.
4. Блок 4 — Интеграция: кнопка «Загрузить» в review_ui.py + колбэк + обновление LOCAL_SETUP.md.
Каждый блок — отдельный шаг с подтверждением.

## Статус реализации — 2026-11-18

- Блок 1 завершён: новая схема, офлайн-кеш, список качеств, загрузка точной высоты
  с проверкой аудио, публикация и переключение манифеста, миграция, тесты.
- Уточнение миграции: старый `source.mkv` сохраняется для путей в старых отчётах;
  создаётся копия в sources/, при ошибке записи манифеста копия убирается.
- `download_quality` возвращает `(path, media)`, `publish_quality` отдельно сохраняет
  source.json. Для первого качества без source_path передаётся manifest_path.
- Merge/remux делегирован штатным постпроцессорам yt-dlp с FFmpeg.
- Блок 2 завершён: `quality_state.py` содержит `QualityState` и
  `QualityTableController`, независимые от Tk и загрузчика. `click()` возвращает
  состояние, диалог запускает загрузку при DOWNLOADING и сообщает её результат
  через `set_state()`. После загрузки нужен отдельный клик для выбора качества.
- Контроллер хранит состояния и ссылки кнопок, обновляет подписи и disabled,
  допускает только одну загрузку и одно выбранное качество. Внешняя блокировка
  независима от блокировки на время загрузки. Все вызовы — из UI-потока.
- Проверка блока 2: `python -m unittest test_quality_state test_youtube_download -v`
  — 23 теста прошли (7 новых тестов стейт-машины, 16 тестов бэкенда).
- Блок 3 завершён: `quality_window.py` — немодальный `QualityDialog`, очередь событий,
  фоновые проверка/загрузка/сохранение выбора, callback при закрытии.
  `QualityTableController` используется из quality_state.py, без дублирования.
- Уточнение Treeview: у ttk нет child_window; настоящие ttk.Button размещены через
  place поверх ячеек, позиции и видимость обновляются при прокрутке/изменении размера.
- После загрузки качество публикуется с `activate=False` одним сохранением манифеста;
  выбор применяется следующим кликом. Снятие выбора сохраняет active_height=null,
  source_path/media оставляют последний валидный источник для совместимости.
- Ок, Отмена и крестик закрывают окно одинаково, без отката выбора. Во время операции
  закрытие откладывается до её завершения; основное окно остаётся доступным.
- Проверка блока 3: `python -m unittest test_quality_window test_quality_state test_youtube_download -v`
  — 30 тестов прошли, включая 6 тестов диалога на настоящем Tk с mock-загрузчиком
  и проверку публикации качества без смены активного источника.
- Блок 4 завершён: кнопка «Загрузить» рядом с «Вставить», singleton-открытие,
  `_on_quality_done`, немедленная синхронизация контекста через `on_change`.
  Обновляется только совпадающий YouTube ID, локальный источник не подменяется.
  busy/start_worker основного окна не менялись. Закрытие приложения ждёт завершения
  активной операции диалога. LOCAL_SETUP.md актуализирован.
- Добавлен `test_quality_integration.py`: 5 тестов открытия, URL, автономности,
  синхронизации контекста, сохранения ручных решений и закрытия.
- Итоговая проверка: `python -m unittest test_quality_integration test_quality_window test_quality_state test_youtube_download test_pipeline test_local_source -v`
  — 64 теста прошли. Реальная сеть и ручная визуальная проверка не выполнялись.
- Все четыре блока реализованы. Полный набор тестов проекта ранее обнаруживал
  проблемы Smart Crop и превышал тайм-аут; в рамках этих блоков они не исправлялись.
