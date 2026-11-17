from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, QUrl
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dramastill.config.defaults import (
    EFFECT_PRESETS,
    EMOTIONS,
    IMAGE_PRESETS,
    IMAGE_STYLES,
    LANGUAGES,
    LLM_PRESETS,
    RESOLUTIONS,
    STAGE_LABELS,
    STATUS_LABELS,
    THEMES,
    TTS_PRESETS,
    AppSettings,
)
from dramastill.core.database import Database
from dramastill.core.secrets import SecretStore
from dramastill.paths import data_dir, default_output_dir, font_path
from dramastill.pipeline.preflight import preflight
from dramastill.providers.factory import build_image, build_llm, build_tts
from dramastill.ui.runtime import CallWorker, Scheduler
from dramastill.ui.theme import STYLESHEET, load_font
from dramastill.util import format_clock, parse_display_time

EDGE_VOICES = {
    ("ru", "female"): "ru-RU-SvetlanaNeural",
    ("ru", "male"): "ru-RU-DmitryNeural",
    ("es", "female"): "es-ES-ElviraNeural",
    ("es", "male"): "es-ES-AlvaroNeural",
    ("uz", "female"): "uz-UZ-MadinaNeural",
    ("uz", "male"): "uz-UZ-SardorNeural",
    ("en", "female"): "en-US-JennyNeural",
    ("en", "male"): "en-US-GuyNeural",
}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DramaStill — драматические видео")
        self.resize(1240, 820)
        self.db = Database(data_dir() / "dramastill.sqlite3")
        self.secrets = SecretStore(data_dir() / "secrets")
        self.db.recover_interrupted()
        self.settings = self.db.load_settings()
        self._tasks: list[QThread] = []
        self._player = None
        self._audio_out = None
        family = load_font()
        self.setFont(QFont(family, 10))
        self.setStyleSheet(STYLESHEET)
        self._build()
        self._load(self.settings)
        self.scheduler = Scheduler(self.db, self.secrets)
        self.scheduler.log_line.connect(self._on_log)
        self.scheduler.progress.connect(self._on_progress)
        self.scheduler.changed.connect(self.refresh_projects)
        self.scheduler.start()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh_projects)
        self._timer.start(1000)
        self.refresh_projects()
        self.statusBar().showMessage("Готово. Одно изображение используется на всю длительность ролика.")

    def shutdown(self) -> None:
        self._timer.stop()
        self.scheduler.request_stop()
        self.scheduler.wait(2000)

    def closeEvent(self, event) -> None:
        self.shutdown()
        event.accept()

    def _build(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(230)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(16, 18, 16, 18)
        logo = QLabel("DramaStill")
        logo.setObjectName("logo")
        side.addWidget(logo)
        subtitle = QLabel("Одна картина. Вся история.")
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)
        side.addWidget(subtitle)
        side.addSpacing(16)
        self.stack = QStackedWidget()
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        pages = [
            ("Главная", self._build_home()),
            ("История", self._build_story()),
            ("Изображение", self._build_image()),
            ("Озвучка", self._build_voice()),
            ("Видео", self._build_video()),
            ("API и ключи", self._build_api()),
            ("Мониторинг", self._build_monitor()),
        ]
        for index, (title, page) in enumerate(pages):
            button = QPushButton(title)
            button.setObjectName("nav")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, number=index: self.stack.setCurrentIndex(number))
            self.nav_group.addButton(button, index)
            side.addWidget(button)
            self.stack.addWidget(page)
        self.nav_group.button(0).setChecked(True)
        side.addStretch(1)
        layout.addWidget(sidebar)
        layout.addWidget(self.stack, 1)
        self.setStatusBar(QStatusBar())

    def _scroll(self, body: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(body)
        return scroll

    def _page_body(self) -> tuple[QWidget, QVBoxLayout]:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(22, 18, 22, 22)
        layout.setSpacing(12)
        return body, layout

    def _title(self, layout: QVBoxLayout, text: str, hint: str) -> None:
        title = QLabel(text)
        title.setObjectName("section")
        layout.addWidget(title)
        note = QLabel(hint)
        note.setObjectName("hint")
        note.setWordWrap(True)
        layout.addWidget(note)

    def _group(self, title: str) -> tuple[QGroupBox, QFormLayout]:
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setSpacing(8)
        return box, form

    def _int(self, low: int, high: int, value: int) -> QSpinBox:
        box = QSpinBox()
        box.setRange(low, high)
        box.setValue(int(value))
        return box

    def _float(self, low: float, high: float, value: float, step: float = 0.01, decimals: int = 2) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(low, high)
        box.setDecimals(decimals)
        box.setSingleStep(step)
        box.setValue(float(value))
        return box

    def _combo(self, options: list[tuple[str, str]], current: str) -> QComboBox:
        box = QComboBox()
        for key, label in options:
            box.addItem(label, key)
        index = box.findData(current)
        if index >= 0:
            box.setCurrentIndex(index)
        return box

    def _build_home(self) -> QWidget:
        body, layout = self._page_body()
        self._title(
            layout,
            "Главная",
            "Одна кнопка запускает историю, единственное изображение, озвучку и сборку MP4. "
            "Текст пишет бесплатная Gemini, без ключа — LLM7 DeepSeek. Картинка — FLUX.1 Schnell, голос — Edge.",
        )
        self.create_button = QPushButton("Создать новое видео")
        self.create_button.setObjectName("primary")
        self.create_button.clicked.connect(self.create_video)
        layout.addWidget(self.create_button)
        stats = QHBoxLayout()
        self.stat_done = QLabel("Готово: 0")
        self.stat_active = QLabel("В работе: 0")
        self.stat_failed = QLabel("С ошибкой: 0")
        for label in (self.stat_done, self.stat_active, self.stat_failed):
            stats.addWidget(label)
        stats.addStretch(1)
        layout.addLayout(stats)
        self.projects = QTableWidget(0, 5)
        self.projects.setHorizontalHeaderLabels(["Название", "Статус", "Этап", "%", "Обновлено"])
        self.projects.horizontalHeader().setStretchLastSection(True)
        self.projects.setSelectionBehavior(QTableWidget.SelectRows)
        self.projects.setEditTriggers(QTableWidget.NoEditTriggers)
        self.projects.verticalHeader().setVisible(False)
        self.projects.cellDoubleClicked.connect(lambda *_: self._open_monitor())
        layout.addWidget(self.projects, 1)
        actions = QHBoxLayout()
        for text, slot in (
            ("Продолжить", self.resume_selected),
            ("Пауза", self.pause_selected),
            ("Остановить", self.cancel_selected),
            ("Открыть папку", self.open_selected),
            ("Удалить из списка", self.delete_selected),
            ("Проверить видеодвижок", self.engine_check),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            actions.addWidget(button)
        layout.addLayout(actions)
        return body

    def _build_story(self) -> QWidget:
        body, layout = self._page_body()
        self._title(
            layout,
            "История",
            "Бесплатная Gemini 3.8 Flash придумывает сюжет. Без ключа ту же работу делает LLM7 DeepSeek V4 Flash. Изображение остаётся одно.",
        )
        box, form = self._group("Сюжет")
        self.theme = self._combo(THEMES, self.settings.story.theme)
        self.language = self._combo(LANGUAGES, self.settings.story.language)
        self.custom_language = QLineEdit(self.settings.story.custom_language)
        self.custom_language.setPlaceholderText("Например: португальский")
        self.duration = self._int(1, 90, self.settings.story.duration_minutes)
        self.duration.setSuffix(" мин")
        self.chapters = self._int(1, 40, self.settings.story.chapters)
        self.wpm = self._int(90, 180, self.settings.story.words_per_minute)
        self.automatic = QCheckBox("Полностью автоматический режим")
        self.automatic.setChecked(self.settings.story.automatic)
        self.consistency = QCheckBox("Проверять последовательность событий")
        self.consistency.setChecked(self.settings.story.consistency_check)
        self.idea = QPlainTextEdit(self.settings.story.idea)
        self.idea.setPlaceholderText("Своя идея. В автоматическом режиме поле можно оставить пустым.")
        self.idea.setFixedHeight(110)
        self.estimate = QLabel("")
        self.estimate.setObjectName("hint")
        self.estimate.setWordWrap(True)
        form.addRow("Тематика", self.theme)
        form.addRow("Язык", self.language)
        form.addRow("Свой язык", self.custom_language)
        form.addRow("Длительность", self.duration)
        form.addRow("Глав", self.chapters)
        form.addRow("Слов в минуту", self.wpm)
        form.addRow("", self.automatic)
        form.addRow("", self.consistency)
        form.addRow("Идея", self.idea)
        form.addRow("", self.estimate)
        layout.addWidget(box)
        for widget in (self.duration, self.chapters, self.wpm):
            widget.valueChanged.connect(self._update_estimate)
        self._update_estimate()
        save = QPushButton("Сохранить настройки")
        save.clicked.connect(self.persist)
        layout.addWidget(save)
        layout.addStretch(1)
        return self._scroll(body)

    def _build_image(self) -> QWidget:
        body, layout = self._page_body()
        self._title(
            layout,
            "Изображение",
            "Одна картинка 16:9 рисуется бесплатной моделью FLUX.1 Schnell. Ключ не нужен. Внешность персонажей входит в этот единственный запрос.",
        )
        box, form = self._group("Провайдер")
        self.image_provider = self._combo([(key, label) for key, label, _url, _model in IMAGE_PRESETS], self.settings.image.provider)
        self.image_url = QLineEdit(self.settings.image.base_url)
        self.image_model = QLineEdit(self.settings.image.model)
        self.image_size = QComboBox()
        self.image_size.setEditable(True)
        for size in ("1280x720", "1024x576", "1792x1024", "1536x1024", "1024x1024"):
            self.image_size.addItem(size)
        self.image_size.setCurrentText(self.settings.image.size)
        self.image_style = self._combo([(key, label) for key, label, _prompt in IMAGE_STYLES], self.settings.image.style)
        self.image_extra = QLineEdit(self.settings.image.style_extra)
        self.image_lock = QCheckBox("Жёстко сохранять внешность в описании кадра")
        self.image_lock.setChecked(self.settings.image.consistency_lock)
        self.image_seed = self._int(-1, 2_000_000_000, self.settings.image.seed)
        self.image_seed.setToolTip("-1 означает, что сервис выберет seed сам")
        form.addRow("Сервис", self.image_provider)
        form.addRow("Адрес API", self.image_url)
        form.addRow("Модель", self.image_model)
        form.addRow("Размер запроса", self.image_size)
        form.addRow("Стиль", self.image_style)
        form.addRow("Дополнение к стилю", self.image_extra)
        form.addRow("", self.image_lock)
        form.addRow("Seed", self.image_seed)
        self.image_provider.currentIndexChanged.connect(self._apply_image_preset)
        layout.addWidget(box)
        note = QLabel("Разрешение ролика задаётся отдельно. Кадр API будет вписан в 1080p, 1440p или 4K.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        layout.addWidget(note)
        save = QPushButton("Сохранить настройки")
        save.clicked.connect(self.persist)
        layout.addWidget(save)
        layout.addStretch(1)
        return self._scroll(body)

    def _build_voice(self) -> QWidget:
        body, layout = self._page_body()
        self._title(
            layout,
            "Озвучка",
            "Голос по умолчанию — бесплатная нейросеть Edge: русский, испанский, узбекский и английский. Длинный текст читается частями и склеивается в одну дорожку.",
        )
        box, form = self._group("Голос")
        self.tts_provider = self._combo([(key, label) for key, label, _url, _model in TTS_PRESETS], self.settings.voice.provider)
        self.tts_url = QLineEdit(self.settings.voice.base_url)
        self.tts_model = QLineEdit(self.settings.voice.model)
        self.voice_language = self._combo(LANGUAGES, self.settings.story.language)
        self.gender = self._combo([("female", "Женский"), ("male", "Мужской")], self.settings.voice.gender)
        self.voice = QComboBox()
        self.voice.setEditable(True)
        self.voice.setCurrentText(self.settings.voice.voice)
        self.speed = self._float(0.5, 1.6, self.settings.voice.speed, 0.05)
        self.emotion = self._combo(EMOTIONS, self.settings.voice.emotion)
        self.sentence_pause = self._int(0, 2000, self.settings.voice.sentence_pause_ms)
        self.sentence_pause.setSuffix(" мс")
        self.chapter_pause = self._int(0, 4000, self.settings.voice.chapter_pause_ms)
        self.chapter_pause.setSuffix(" мс")
        self.chunk_chars = self._int(400, 4500, self.settings.voice.chunk_chars)
        self.tts_parallel = self._int(1, 4, self.settings.voice.parallelism)
        form.addRow("Сервис", self.tts_provider)
        form.addRow("Адрес API", self.tts_url)
        form.addRow("Модель", self.tts_model)
        form.addRow("Язык", self.voice_language)
        form.addRow("Пол голоса", self.gender)
        form.addRow("Голос", self.voice)
        form.addRow("Скорость", self.speed)
        form.addRow("Характер", self.emotion)
        form.addRow("Пауза между фразами", self.sentence_pause)
        form.addRow("Пауза между главами", self.chapter_pause)
        form.addRow("Размер фрагмента", self.chunk_chars)
        form.addRow("Потоков озвучки", self.tts_parallel)
        layout.addWidget(box)
        row = QHBoxLayout()
        pick = QPushButton("Подобрать голос")
        pick.clicked.connect(self._pick_voice)
        refresh = QPushButton("Обновить список")
        refresh.clicked.connect(self._refresh_voices)
        sample = QPushButton("Произнести пробную фразу")
        sample.clicked.connect(self._sample_voice)
        row.addWidget(pick)
        row.addWidget(refresh)
        row.addWidget(sample)
        layout.addLayout(row)
        hint = QLabel("Пауза 0 оставляет естественные паузы синтеза. Большее значение читает фразы отдельно и вставляет тишину.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.language.currentIndexChanged.connect(lambda *_: self._mirror_language(self.language, self.voice_language))
        self.voice_language.currentIndexChanged.connect(lambda *_: self._mirror_language(self.voice_language, self.language))
        self.tts_provider.currentIndexChanged.connect(self._apply_tts_preset)
        save = QPushButton("Сохранить настройки")
        save.clicked.connect(self.persist)
        layout.addWidget(save)
        layout.addStretch(1)
        return self._scroll(body)

    def _build_video(self) -> QWidget:
        body, layout = self._page_body()
        self._title(layout, "Видео", "Единственный кадр медленно дышит зумом и сдвигом. Искры и полоски настраиваются отдельно.")
        video = self.settings.video
        box, form = self._group("Файл")
        self.resolution = self._combo([(name, name) for name, _w, _h in RESOLUTIONS], _resolution_name(video.width, video.height))
        self.fps = self._int(8, 60, video.fps)
        self.video_bitrate = QLineEdit(video.video_bitrate)
        self.audio_bitrate = QLineEdit(video.audio_bitrate)
        self.encoder = self._combo(
            [("auto", "Авто: NVENC, если карта отвечает"), ("libx264", "Процессор (libx264)"), ("h264_nvenc", "NVIDIA NVENC")],
            video.encoder,
        )
        self.x264_preset = self._combo(
            [(name, name) for name in ("ultrafast", "veryfast", "faster", "fast", "medium")],
            video.x264_preset,
        )
        self.chunk_seconds = self._int(10, 300, video.chunk_seconds)
        self.chunk_seconds.setSuffix(" с")
        self.parallel_projects = self._int(1, 3, video.max_parallel_projects)
        self.cleanup = QCheckBox("Удалять временные фрагменты после успеха")
        self.cleanup.setChecked(video.cleanup_temp)
        self.output_dir = QLineEdit(video.output_dir or str(default_output_dir()))
        browse = QPushButton("Папка")
        browse.clicked.connect(self._browse_output)
        output_row = QWidget()
        output_layout = QHBoxLayout(output_row)
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.addWidget(self.output_dir)
        output_layout.addWidget(browse)
        self.ffmpeg_path = QLineEdit(video.ffmpeg_path)
        self.ffprobe_path = QLineEdit(video.ffprobe_path)
        form.addRow("Разрешение", self.resolution)
        form.addRow("FPS", self.fps)
        form.addRow("Битрейт видео", self.video_bitrate)
        form.addRow("Битрейт аудио", self.audio_bitrate)
        form.addRow("Кодировщик", self.encoder)
        form.addRow("Пресет x264", self.x264_preset)
        form.addRow("Длина фрагмента", self.chunk_seconds)
        form.addRow("Проектов сразу", self.parallel_projects)
        form.addRow("", self.cleanup)
        form.addRow("Папка экспорта", output_row)
        form.addRow("ffmpeg", self.ffmpeg_path)
        form.addRow("ffprobe", self.ffprobe_path)
        layout.addWidget(box)

        motion, motion_form = self._group("Движение камеры")
        self.effect_preset = self._combo(
            [
                ("subtle", "Спокойный"),
                ("cinematic", "Кинематографический"),
                ("dramatic", "Драматический"),
                ("warm_dust", "Тёплая пыль"),
                ("minimal", "Минимальный"),
            ],
            video.preset,
        )
        self.zoom_base = self._float(1.02, 1.2, video.zoom_base, 0.005, 3)
        self.zoom_amp = self._float(0.0, 0.08, video.zoom_amplitude, 0.002, 3)
        self.zoom_period = self._float(20, 180, video.zoom_period, 1, 1)
        self.pan_x = self._float(0, 80, video.pan_x_px, 1, 1)
        self.pan_y = self._float(0, 50, video.pan_y_px, 1, 1)
        self.pan_period_x = self._float(15, 180, video.pan_period_x, 1, 1)
        self.pan_period_y = self._float(15, 180, video.pan_period_y, 1, 1)
        self.shake = self._float(0, 8, video.shake_px, 0.1, 2)
        self.shake_period = self._float(0.8, 8, video.shake_period, 0.1, 2)
        motion_form.addRow("Готовый пресет", self.effect_preset)
        motion_form.addRow("Базовый зум", self.zoom_base)
        motion_form.addRow("Амплитуда зума", self.zoom_amp)
        motion_form.addRow("Период зума, с", self.zoom_period)
        motion_form.addRow("Сдвиг влево-вправо, px", self.pan_x)
        motion_form.addRow("Сдвиг вверх-вниз, px", self.pan_y)
        motion_form.addRow("Период горизонтали, с", self.pan_period_x)
        motion_form.addRow("Период вертикали, с", self.pan_period_y)
        motion_form.addRow("Покачивание, px", self.shake)
        motion_form.addRow("Период покачивания, с", self.shake_period)
        self.effect_preset.currentIndexChanged.connect(self._apply_effect_preset)
        layout.addWidget(motion)

        effects, effect_form = self._group("Искры, свет и визуализатор")
        self.sparks = QCheckBox("Искры")
        self.sparks.setChecked(video.sparks)
        self.dust = QCheckBox("Пыль")
        self.dust.setChecked(video.dust)
        self.lights = QCheckBox("Мягкий свет")
        self.lights.setChecked(video.lights)
        flags = QWidget()
        flag_row = QHBoxLayout(flags)
        flag_row.setContentsMargins(0, 0, 0, 0)
        flag_row.addWidget(self.sparks)
        flag_row.addWidget(self.dust)
        flag_row.addWidget(self.lights)
        self.spark_density = self._float(0.1, 1.5, video.spark_density, 0.05)
        self.spark_opacity = self._float(0.05, 1.0, video.spark_opacity, 0.05)
        self.dust_opacity = self._float(0.0, 1.0, video.dust_opacity, 0.05)
        self.light_opacity = self._float(0.0, 0.6, video.light_opacity, 0.02)
        self.vignette = self._float(0.0, 0.8, video.vignette, 0.05)
        self.contrast = self._float(0.9, 1.3, video.grade_contrast, 0.01)
        self.saturation = self._float(0.8, 1.4, video.grade_saturation, 0.01)
        self.visualizer = QCheckBox("Показывать полоски по громкости голоса")
        self.visualizer.setChecked(video.visualizer)
        self.bars = self._int(8, 96, video.visualizer_bars)
        self.bar_color = QLineEdit(video.visualizer_color)
        self.bar_opacity = self._float(0.1, 1.0, video.visualizer_opacity, 0.05)
        self.bar_height = self._int(60, 280, video.visualizer_height)
        self.bar_gap = self._float(0.1, 0.7, video.visualizer_gap, 0.02)
        self.bar_smooth = self._float(0.0, 1.0, video.visualizer_smooth, 0.02)
        effect_form.addRow("Слои", flags)
        effect_form.addRow("Плотность частиц", self.spark_density)
        effect_form.addRow("Яркость искр", self.spark_opacity)
        effect_form.addRow("Яркость пыли", self.dust_opacity)
        effect_form.addRow("Яркость света", self.light_opacity)
        effect_form.addRow("Виньетка", self.vignette)
        effect_form.addRow("Контраст", self.contrast)
        effect_form.addRow("Насыщенность", self.saturation)
        effect_form.addRow("", self.visualizer)
        effect_form.addRow("Число полосок", self.bars)
        effect_form.addRow("Цвет полосок", self.bar_color)
        effect_form.addRow("Прозрачность полосок", self.bar_opacity)
        effect_form.addRow("Высота зоны", self.bar_height)
        effect_form.addRow("Промежуток", self.bar_gap)
        effect_form.addRow("Плавность", self.bar_smooth)
        layout.addWidget(effects)
        save = QPushButton("Сохранить настройки")
        save.clicked.connect(self.persist)
        layout.addWidget(save)
        layout.addStretch(1)
        return self._scroll(body)

    def _build_api(self) -> QWidget:
        body, layout = self._page_body()
        self._title(
            layout,
            "API и ключи",
            "Бесплатный маршрут ключей не требует: текст пишет LLM7 DeepSeek, картинку рисует FLUX.1 Schnell. "
            "Ключ Gemini с aistudio.google.com/apikey включает более сильный текст. Ключи не пишутся в журнал.",
        )
        self.llm_key = QLineEdit(self.secrets.get("llm"))
        self.llm_key.setEchoMode(QLineEdit.Password)
        self.llm_key.setPlaceholderText("Необязательно: бесплатный ключ Gemini")
        self.image_key = QLineEdit(self.secrets.get("image"))
        self.image_key.setEchoMode(QLineEdit.Password)
        self.tts_key = QLineEdit(self.secrets.get("tts"))
        self.tts_key.setEchoMode(QLineEdit.Password)
        for title, field, slot in (
            ("Языковая модель", self.llm_key, self._test_llm),
            ("Изображения", self.image_key, self._test_image),
            ("Озвучка", self.tts_key, self._test_tts_key),
        ):
            box, form = self._group(title)
            form.addRow("Ключ", field)
            button = QPushButton("Проверить соединение")
            button.clicked.connect(slot)
            form.addRow("", button)
            layout.addWidget(box)
        self.llm_provider = self._combo([(key, label) for key, label, _url, _model in LLM_PRESETS], self.settings.llm.provider)
        self.llm_url = QLineEdit(self.settings.llm.base_url)
        self.llm_model = QLineEdit(self.settings.llm.model)
        self.llm_temp = self._float(0.1, 1.4, self.settings.llm.temperature, 0.05)
        self.llm_tokens = self._int(1000, 16000, self.settings.llm.max_tokens)
        box, form = self._group("Языковая модель")
        form.addRow("Сервис", self.llm_provider)
        form.addRow("Адрес API", self.llm_url)
        form.addRow("Модель", self.llm_model)
        form.addRow("Температура", self.llm_temp)
        form.addRow("Лимит ответа", self.llm_tokens)
        self.llm_provider.currentIndexChanged.connect(self._apply_llm_preset)
        layout.addWidget(box)
        save = QPushButton("Сохранить ключи и настройки")
        save.clicked.connect(self.persist)
        layout.addWidget(save)
        layout.addStretch(1)
        return self._scroll(body)

    def _build_monitor(self) -> QWidget:
        body, layout = self._page_body()
        self._title(layout, "Мониторинг", "Прогресс идёт по реально завершённым главам, фрагментам речи и кускам видео.")
        self.monitor_project = QComboBox()
        self.monitor_project.currentIndexChanged.connect(self._show_selected_log)
        self.stage_label = QLabel("Этап: —")
        self.elapsed_label = QLabel("Время: —")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.monitor_project)
        layout.addWidget(self.stage_label)
        layout.addWidget(self.elapsed_label)
        layout.addWidget(self.progress)
        layout.addWidget(self.error_label)
        row = QHBoxLayout()
        for text, slot in (("Пауза", self.pause_selected), ("Продолжить", self.resume_selected), ("Остановить", self.cancel_selected)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)
        layout.addLayout(row)
        layout.addWidget(self.log, 1)
        return body

    def collect(self) -> AppSettings:
        settings = AppSettings()
        settings.story.theme = self.theme.currentData()
        settings.story.language = self.language.currentData()
        settings.story.custom_language = self.custom_language.text().strip()
        settings.story.duration_minutes = self.duration.value()
        settings.story.chapters = self.chapters.value()
        settings.story.words_per_minute = self.wpm.value()
        settings.story.automatic = self.automatic.isChecked()
        settings.story.consistency_check = self.consistency.isChecked()
        settings.story.idea = self.idea.toPlainText().strip()
        settings.llm.provider = self.llm_provider.currentData()
        settings.llm.base_url = self.llm_url.text().strip()
        settings.llm.model = self.llm_model.text().strip()
        settings.llm.temperature = self.llm_temp.value()
        settings.llm.max_tokens = self.llm_tokens.value()
        settings.image.provider = self.image_provider.currentData()
        settings.image.base_url = self.image_url.text().strip()
        settings.image.model = self.image_model.text().strip()
        settings.image.size = self.image_size.currentText().strip() or "1792x1024"
        settings.image.style = self.image_style.currentData()
        settings.image.style_extra = self.image_extra.text().strip()
        settings.image.consistency_lock = self.image_lock.isChecked()
        settings.image.seed = self.image_seed.value()
        settings.voice.provider = self.tts_provider.currentData()
        settings.voice.base_url = self.tts_url.text().strip()
        settings.voice.model = self.tts_model.text().strip()
        settings.voice.voice = self.voice.currentText().strip()
        settings.voice.gender = self.gender.currentData()
        settings.voice.speed = self.speed.value()
        settings.voice.emotion = self.emotion.currentData()
        settings.voice.sentence_pause_ms = self.sentence_pause.value()
        settings.voice.chapter_pause_ms = self.chapter_pause.value()
        settings.voice.chunk_chars = self.chunk_chars.value()
        settings.voice.parallelism = self.tts_parallel.value()
        if settings.voice.provider == "elevenlabs":
            settings.voice.eleven_model = settings.voice.model
        width, height = _resolution_size(self.resolution.currentData())
        settings.video.width = width
        settings.video.height = height
        settings.video.fps = self.fps.value()
        settings.video.video_bitrate = self.video_bitrate.text().strip() or "8M"
        settings.video.audio_bitrate = self.audio_bitrate.text().strip() or "192k"
        settings.video.encoder = self.encoder.currentData()
        settings.video.x264_preset = self.x264_preset.currentData()
        settings.video.chunk_seconds = self.chunk_seconds.value()
        settings.video.max_parallel_projects = self.parallel_projects.value()
        settings.video.cleanup_temp = self.cleanup.isChecked()
        settings.video.output_dir = self.output_dir.text().strip()
        settings.video.ffmpeg_path = self.ffmpeg_path.text().strip()
        settings.video.ffprobe_path = self.ffprobe_path.text().strip()
        settings.video.preset = self.effect_preset.currentData()
        settings.video.zoom_base = self.zoom_base.value()
        settings.video.zoom_amplitude = self.zoom_amp.value()
        settings.video.zoom_period = self.zoom_period.value()
        settings.video.pan_x_px = self.pan_x.value()
        settings.video.pan_y_px = self.pan_y.value()
        settings.video.pan_period_x = self.pan_period_x.value()
        settings.video.pan_period_y = self.pan_period_y.value()
        settings.video.shake_px = self.shake.value()
        settings.video.shake_period = self.shake_period.value()
        settings.video.sparks = self.sparks.isChecked()
        settings.video.dust = self.dust.isChecked()
        settings.video.lights = self.lights.isChecked()
        settings.video.spark_density = self.spark_density.value()
        settings.video.spark_opacity = self.spark_opacity.value()
        settings.video.dust_opacity = self.dust_opacity.value()
        settings.video.light_opacity = self.light_opacity.value()
        settings.video.vignette = self.vignette.value()
        settings.video.grade_contrast = self.contrast.value()
        settings.video.grade_saturation = self.saturation.value()
        settings.video.visualizer = self.visualizer.isChecked()
        settings.video.visualizer_bars = self.bars.value()
        settings.video.visualizer_color = self.bar_color.text().strip() or "#E6C36A"
        settings.video.visualizer_opacity = self.bar_opacity.value()
        settings.video.visualizer_height = self.bar_height.value()
        settings.video.visualizer_gap = self.bar_gap.value()
        settings.video.visualizer_smooth = self.bar_smooth.value()
        return settings

    def _load(self, settings: AppSettings) -> None:
        self._set_combo(self.theme, settings.story.theme)
        self._set_combo(self.language, settings.story.language)
        self._set_combo(self.voice_language, settings.story.language)
        self.custom_language.setText(settings.story.custom_language)
        self.duration.setValue(settings.story.duration_minutes)
        self.chapters.setValue(settings.story.chapters)
        self.wpm.setValue(settings.story.words_per_minute)
        self.automatic.setChecked(settings.story.automatic)
        self.consistency.setChecked(settings.story.consistency_check)
        self.idea.setPlainText(settings.story.idea)
        self._update_estimate()

    def _set_combo(self, box: QComboBox, value: str) -> None:
        index = box.findData(value)
        if index >= 0:
            box.setCurrentIndex(index)

    def persist(self) -> None:
        settings = self.collect()
        self.db.save_settings(settings)
        self.secrets.set_many({"llm": self.llm_key.text().strip(), "image": self.image_key.text().strip(), "tts": self.tts_key.text().strip()})
        self.settings = settings
        self.statusBar().showMessage("Настройки сохранены")

    def create_video(self) -> None:
        self.persist()
        settings = self.collect()
        problems = preflight(settings, self.secrets)
        if problems:
            QMessageBox.warning(self, "Нельзя начать", "\n".join(problems))
            return
        title = settings.story.idea.strip()[:80] or "Новая история"
        project = self.db.create_project(settings, title)
        self.scheduler.wake()
        self.refresh_projects()
        self._select_project(project.id)
        self.stack.setCurrentIndex(6)
        self.nav_group.button(6).setChecked(True)
        self.statusBar().showMessage("Проект поставлен в очередь")

    def refresh_projects(self) -> None:
        projects = self.db.list_projects()
        self.stat_done.setText(f"Готово: {sum(item.status == 'completed' for item in projects)}")
        self.stat_active.setText(f"В работе: {sum(item.status in {'queued', 'running'} for item in projects)}")
        self.stat_failed.setText(f"С ошибкой: {sum(item.status == 'failed' for item in projects)}")
        selected = self._current_project_id()
        signature = [
            (item.id, item.status, item.stage, round(item.progress, 3), item.title, item.error, item.updated_at)
            for item in projects
        ]
        if signature == getattr(self, "_project_signature", None):
            self._show_current_labels()
            return
        self._project_signature = signature
        self.projects.setRowCount(len(projects))
        for row, project in enumerate(projects):
            values = [
                project.title,
                STATUS_LABELS.get(project.status, project.status),
                STAGE_LABELS.get(project.stage, project.stage),
                f"{int(project.progress * 100)}%",
                parse_display_time(project.updated_at),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, project.id)
                self.projects.setItem(row, column, item)
        if selected:
            for row in range(self.projects.rowCount()):
                item = self.projects.item(row, 0)
                if item is not None and item.data(Qt.UserRole) == selected:
                    self.projects.selectRow(row)
                    break
        self._fill_monitor(projects, selected)
        self._show_current_labels()

    def _show_current_labels(self) -> None:
        current = self.db.get(self._current_project_id() or "")
        if current is not None:
            self.stage_label.setText(f"Этап: {STAGE_LABELS.get(current.stage, current.stage or '—')}")
            self.elapsed_label.setText(f"Время: {format_clock(_elapsed_seconds(current))}")
            self.progress.setValue(int(current.progress * 1000))
            self.error_label.setText(current.error or "")

    def _fill_monitor(self, projects, selected: str | None) -> None:
        self.monitor_project.blockSignals(True)
        self.monitor_project.clear()
        for project in projects:
            self.monitor_project.addItem(f"{project.title} — {STATUS_LABELS.get(project.status, project.status)}", project.id)
        if selected:
            index = self.monitor_project.findData(selected)
            if index >= 0:
                self.monitor_project.setCurrentIndex(index)
        self.monitor_project.blockSignals(False)

    def _current_project_id(self) -> str | None:
        if self.stack.currentIndex() == 6 and self.monitor_project.currentData():
            return self.monitor_project.currentData()
        row = self.projects.currentRow()
        if row < 0 or self.projects.item(row, 0) is None:
            if self.monitor_project.currentData():
                return self.monitor_project.currentData()
            return None
        return self.projects.item(row, 0).data(Qt.UserRole)

    def _select_project(self, project_id: str) -> None:
        index = self.monitor_project.findData(project_id)
        if index >= 0:
            self.monitor_project.setCurrentIndex(index)
        self._show_selected_log()

    def _on_log(self, project_id: str, level: str, message: str) -> None:
        if project_id == self.monitor_project.currentData():
            self.log.appendPlainText(f"[{level}] {message}")

    def _on_progress(self, project_id: str, fraction: float, stage: str) -> None:
        if project_id == self.monitor_project.currentData():
            self.progress.setValue(int(fraction * 1000))
            self.stage_label.setText(f"Этап: {STAGE_LABELS.get(stage, stage)}")

    def _show_selected_log(self) -> None:
        project_id = self.monitor_project.currentData()
        self.log.clear()
        if not project_id:
            return
        for row in self.db.logs(project_id):
            self.log.appendPlainText(f"[{row['level']}] {row['message']}")
        project = self.db.get(project_id)
        if project is not None:
            self.error_label.setText(project.error or "")
            self.progress.setValue(int(project.progress * 1000))
            self.stage_label.setText(f"Этап: {STAGE_LABELS.get(project.stage, project.stage or '—')}")

    def resume_selected(self) -> None:
        project_id = self._current_project_id()
        if project_id:
            self.scheduler.resume(project_id)

    def pause_selected(self) -> None:
        project_id = self._current_project_id()
        if project_id:
            self.scheduler.pause(project_id)

    def cancel_selected(self) -> None:
        project_id = self._current_project_id()
        if project_id:
            self.scheduler.cancel(project_id)

    def open_selected(self) -> None:
        project = self.db.get(self._current_project_id() or "")
        if project is None:
            return
        path = Path(project.output_path).parent if project.output_path else Path(project.work_dir)
        if not path.exists():
            QMessageBox.information(self, "Папка", "Папка проекта ещё не создана")
            return
        _open_folder(path)

    def delete_selected(self) -> None:
        project = self.db.get(self._current_project_id() or "")
        if project is None:
            return
        if project.status == "running":
            QMessageBox.information(self, "Удаление", "Сначала поставьте проект на паузу")
            return
        answer = QMessageBox.question(self, "Удаление", "Удалить проект из списка и его рабочие файлы? Готовый ролик в папке экспорта останется.")
        if answer != QMessageBox.Yes:
            return
        shutil.rmtree(project.work_dir, ignore_errors=True)
        self.db.delete_project(project.id)
        self.refresh_projects()

    def _open_monitor(self) -> None:
        project_id = self._current_project_id()
        self.stack.setCurrentIndex(6)
        self.nav_group.button(6).setChecked(True)
        if project_id:
            self._select_project(project_id)

    def engine_check(self) -> None:
        self.persist()
        settings = self.collect()

        def action() -> str:
            from dramastill.media.engine_check import run_engine_check

            work = data_dir() / "engine-check"
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)
            destination = run_engine_check(
                settings.video,
                Path(settings.video.output_dir or default_output_dir()),
                work,
                lambda _level, message: None,
            )
            return str(destination)

        self.statusBar().showMessage("Идёт проверка видеодвижка")
        self._run_call(action, lambda path: QMessageBox.information(self, "Видеодвижок", f"Проверочный ролик готов:\n{path}"))

    def _update_estimate(self) -> None:
        words = self.duration.value() * self.wpm.value()
        chapters = max(1, self.chapters.value())
        text = (
            f"Ориентир текста: {words} слов, около {words // chapters} на главу. "
            "Длина ролика будет равна длине озвучки. Изображение одно на весь ролик."
        )
        if words / chapters > 900:
            text += " Для такой длины лучше добавить глав."
        self.estimate.setText(text)

    def _apply_llm_preset(self) -> None:
        key = self.llm_provider.currentData()
        for preset, _label, url, model in LLM_PRESETS:
            if preset == key:
                self.llm_url.setText(url)
                self.llm_model.setText(model)

    def _apply_image_preset(self) -> None:
        key = self.image_provider.currentData()
        for preset, _label, url, model in IMAGE_PRESETS:
            if preset == key:
                self.image_url.setText(url)
                self.image_model.setText(model)

    def _apply_tts_preset(self) -> None:
        key = self.tts_provider.currentData()
        for preset, _label, url, model in TTS_PRESETS:
            if preset == key:
                self.tts_url.setText(url)
                self.tts_model.setText(model)
        self._pick_voice()

    def _apply_effect_preset(self) -> None:
        values = EFFECT_PRESETS.get(self.effect_preset.currentData() or "")
        if not values:
            return
        self.zoom_base.setValue(values["zoom_base"])
        self.zoom_amp.setValue(values["zoom_amplitude"])
        self.zoom_period.setValue(values["zoom_period"])
        self.pan_x.setValue(values["pan_x_px"])
        self.pan_y.setValue(values["pan_y_px"])
        self.pan_period_x.setValue(values["pan_period_x"])
        self.pan_period_y.setValue(values["pan_period_y"])
        self.shake.setValue(values["shake_px"])
        self.shake_period.setValue(values["shake_period"])
        self.spark_density.setValue(values["spark_density"])
        self.spark_opacity.setValue(values["spark_opacity"])
        self.dust_opacity.setValue(values["dust_opacity"])
        self.light_opacity.setValue(values["light_opacity"])
        self.bar_opacity.setValue(values["visualizer_opacity"])
        self.bar_height.setValue(values["visualizer_height"])
        self.vignette.setValue(values["vignette"])

    def _mirror_language(self, source: QComboBox, target: QComboBox) -> None:
        if source.currentData() == target.currentData():
            return
        target.blockSignals(True)
        self._set_combo(target, source.currentData())
        target.blockSignals(False)

    def _pick_voice(self) -> None:
        provider = self.tts_provider.currentData()
        gender = self.gender.currentData()
        language = self.language.currentData()
        if provider == "openai":
            self.voice.setCurrentText("nova" if gender == "female" else "onyx")
            return
        if provider == "edge":
            self.voice.setCurrentText(EDGE_VOICES.get((language, gender), "ru-RU-SvetlanaNeural"))

    def _refresh_voices(self) -> None:
        self.persist()
        settings = self.collect()

        def action() -> str:
            import json

            voices = build_tts(settings, self.secrets).list_voices(settings.story.language)
            return json.dumps(
                [{"id": voice.id, "label": voice.label} for voice in voices],
                ensure_ascii=False,
            )

        def done(payload: str) -> None:
            import json

            voices = json.loads(payload)
            current = self.voice.currentText()
            self.voice.clear()
            for voice in voices:
                self.voice.addItem(f"{voice['label']} [{voice['id']}]", voice["id"])
            if current:
                self.voice.setCurrentText(current)
            elif voices:
                self.voice.setCurrentText(voices[0]["id"])
            QMessageBox.information(self, "Голоса", f"Голосов: {len(voices)}")

        self._run_call(action, done)

    def _sample_voice(self) -> None:
        self.persist()
        settings = self.collect()
        phrase = {
            "ru": "Это короткая проверка голоса для драматической истории.",
            "es": "Esta es una prueba breve de la voz.",
            "uz": "Bu ovoz uchun qisqa sinov.",
            "en": "This is a short voice test.",
        }.get(settings.story.language, "This is a short voice test.")

        def action() -> str:
            dest = data_dir() / "voice-test.mp3"
            build_tts(settings, self.secrets).synthesize(
                text=phrase,
                dest=dest,
                voice=settings.voice.voice,
                speed=settings.voice.speed,
                emotion=settings.voice.emotion,
                language=settings.story.language,
            )
            return str(dest)

        def done(path: str) -> None:
            self.statusBar().showMessage(f"Фраза сохранена: {path}")
            self._play(path)

        self._run_call(action, done)

    def _test_llm(self) -> None:
        self.persist()
        settings = self.collect()

        def action() -> str:
            provider = build_llm(settings, self.secrets)
            ids = provider.list_model_ids()
            return f"Соединение есть. Моделей в списке: {len(ids)}"

        self._run_call(action)

    def _test_image(self) -> None:
        self.persist()
        settings = self.collect()

        def action() -> str:
            provider = build_image(settings, self.secrets)
            if settings.image.provider == "flux":
                import httpx

                response = httpx.get(settings.image.base_url.rstrip("/") + "/config", timeout=30)
                if response.status_code >= 400:
                    raise RuntimeError(f"FLUX ответил {response.status_code}")
                return "FLUX.1 Schnell отвечает. Ключ не нужен, картинка при проверке не генерировалась."
            if settings.image.provider == "stability":
                import httpx

                response = httpx.get(
                    "https://api.stability.ai/v1/user/account",
                    headers={"authorization": f"Bearer {self.secrets.get('image')}"},
                    timeout=30,
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"Stability ответил {response.status_code}")
                return "Ключ Stability принят. Картинка не генерировалась."
            if hasattr(provider, "list_model_ids"):
                return "Для изображений OpenAI проверьте ключ через список моделей языкового API тем же ключом, если сервисы разные."
            import httpx

            response = httpx.get(
                settings.image.base_url.rstrip("/") + "/models",
                headers={"Authorization": f"Bearer {self.secrets.get('image')}"},
                timeout=30,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Сервис ответил {response.status_code}")
            return "Ключ изображений принят. Генерация не запускалась."

        self._run_call(action)

    def _test_tts_key(self) -> None:
        self.persist()
        settings = self.collect()

        def action() -> str:
            provider = build_tts(settings, self.secrets)
            voices = provider.list_voices(settings.story.language)
            return f"Озвучка доступна. Голосов: {len(voices)}"

        self._run_call(action)

    def _run_call(self, action, done=None) -> None:
        thread = CallWorker(action, self.secrets.values())
        thread.succeeded.connect(done or (lambda message: QMessageBox.information(self, "Проверка", message)))
        thread.failed.connect(lambda message: QMessageBox.warning(self, "Проверка", message))
        thread.start()
        self._tasks.append(thread)

    def _browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Папка для видео", self.output_dir.text())
        if path:
            self.output_dir.setText(path)

    def _play(self, path: str) -> None:
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        except Exception:
            _open_folder(Path(path).parent)
            return
        self._player = QMediaPlayer(self)
        self._audio_out = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_out)
        self._audio_out.setVolume(1.0)
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()


def _elapsed_seconds(project) -> float:
    from datetime import datetime, timezone

    extra = 0.0
    if project.status == "running" and project.started_at:
        try:
            started = datetime.fromisoformat(project.started_at)
        except ValueError:
            started = None
        if started is not None:
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            extra = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
    return project.elapsed_seconds + extra


def _resolution_name(width: int, height: int) -> str:
    for name, item_width, item_height in RESOLUTIONS:
        if item_width == width and item_height == height:
            return name
    return "1080p"


def _resolution_size(name: str) -> tuple[int, int]:
    for item_name, width, height in RESOLUTIONS:
        if item_name == name:
            return width, height
    return 1920, 1080


def _open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
