"""Main standalone Windows application window for wlz-yt."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

import requests
from PyQt6.QtCore import QEvent, QTimer, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QCursor, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.engine import (
    CookieStatus,
    DownloadCancelledError,
    DownloadResult,
    ProgressUpdate,
    VideoMetadata,
    YouTubeEngine,
    format_resolution_badge,
    is_likely_youtube_url,
    normalize_youtube_url,
)
from ui.components import (
    FlowLayout,
    HistoryItemRow,
    ModeSelectorCard,
    PillOptionButton,
    RoundedThumbnailWidget,
    SmoothProgressBar,
    StyledComboBox,
)
from ui.icons import create_action_icon, create_app_icon, create_app_logo_pixmap
from ui.theme import (
    ACCENT_PRIMARY,
    ERROR_COLOR,
    GLOBAL_STYLESHEET,
    GPU_COLOR,
    SUCCESS_COLOR,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    WARNING_COLOR,
    apply_windows_dark_titlebar,
)


class ProbeWorker(QThread):
    """Background worker that fetches video metadata and thumbnail image."""

    finished_probe = pyqtSignal(object, bytes)  # (VideoMetadata, thumbnail_bytes)
    failed_probe = pyqtSignal(str, str)  # (url, error_message)

    def __init__(self, engine: YouTubeEngine, url: str) -> None:
        super().__init__()
        self.engine = engine
        self.url = url

    def run(self) -> None:
        try:
            meta = self.engine.probe_video(self.url)
            thumb_bytes = b""
            if meta.thumbnail_url:
                try:
                    resp = requests.get(meta.thumbnail_url, timeout=6)
                    if resp.status_code == 200:
                        thumb_bytes = resp.content
                except Exception:
                    pass
            self.finished_probe.emit(meta, thumb_bytes)
        except Exception as exc:
            self.failed_probe.emit(self.url, str(exc))


class DownloadWorker(QThread):
    """Background worker that executes download + editor-safe encoding."""

    progress_changed = pyqtSignal(object)  # ProgressUpdate
    download_completed = pyqtSignal(object)  # DownloadResult
    download_failed = pyqtSignal(str, bool)  # (error_msg, was_cancelled)

    def __init__(
        self,
        engine: YouTubeEngine,
        url: str,
        output_dir: str,
        mode: str,
        max_height: Optional[int],
        audio_format: str,
        prefetched_meta: Optional[VideoMetadata],
    ) -> None:
        super().__init__()
        self.engine = engine
        self.url = url
        self.output_dir = output_dir
        self.mode = mode
        self.max_height = max_height
        self.audio_format = audio_format
        self.prefetched_meta = prefetched_meta
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            res = self.engine.download_and_encode(
                raw_url=self.url,
                output_dir=self.output_dir,
                mode=self.mode,
                max_height=self.max_height,
                audio_format=self.audio_format,
                progress_callback=lambda upd: self.progress_changed.emit(upd),
                cancel_event=self.cancel_event,
                prefetched_meta=self.prefetched_meta,
            )
            self.download_completed.emit(res)
        except DownloadCancelledError as exc:
            self.download_failed.emit(str(exc), True)
        except Exception as exc:
            self.download_failed.emit(str(exc), False)


AUDIO_FORMAT_HINTS = {
    "original": "Direct stream remux (-c:a copy) into native Opus/M4A — zero generation loss & instant speed.",
    "flac": "24-bit Lossless FLAC (level 8) preserving native sample rate — ~45% smaller than WAV, zero lossy re-encoding.",
    "wav": "24-bit 48kHz uncompressed PCM with 64-tap Kaiser Sinc resampling — universal DAW & NLE master.",
    "m4a": "Zero-loss copy for native 48kHz AAC, or AAC-LC 320k Two-Loop (20kHz cutoff) + Kaiser Sinc 48kHz.",
    "mp3": "320kbps CBR via LAME Extreme (-q 0) + 64-tap Kaiser Sinc 48kHz + embedded ID3v2 tags.",
}


class MainWindow(QMainWindow):
    """Standalone Windows GUI for wlz-yt."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("wlz-yt — Universal Editor-Ready Downloader")
        self.setWindowIcon(create_app_icon())
        self.resize(980, 840)
        self.setMinimumSize(640, 560)
        self.setStyleSheet(GLOBAL_STYLESHEET)

        # Default Downloads directory inside workspace (or persisted user path)
        self.project_root = Path(__file__).resolve().parent.parent
        self.settings_path = self.project_root / "settings.json"
        self.default_app_downloads = str(self.project_root / "Downloads")

        # Load persisted settings
        settings = self._load_settings()
        self.output_dir = settings["output_dir"]
        self.cookie_source = settings.get("cookie_source", "auto")
        self.cookie_file = settings.get("cookie_file", "")
        saved_audio_fmt = settings.get("audio_format", "mp3")
        os.makedirs(self.output_dir, exist_ok=True)

        # Initialize Core Engine with cookie configuration
        self.engine = YouTubeEngine(
            cookie_source=self.cookie_source,
            cookie_file=self.cookie_file,
        )

        # Current state
        self.selected_mode: str = "both"  # "both" | "audio" | "video"
        self.selected_res_key: str = "max"  # "max" | "2160" | "1440" | "1080" | "720"
        self.selected_audio_fmt: str = (
            saved_audio_fmt if saved_audio_fmt in AUDIO_FORMAT_HINTS else "mp3"
        )
        self.current_meta: Optional[VideoMetadata] = None
        self.last_probed_url: str = ""

        self.probe_worker: Optional[ProbeWorker] = None
        self.download_worker: Optional[DownloadWorker] = None

        # Debounce timer for automatic link probing
        self.probe_timer = QTimer(self)
        self.probe_timer.setSingleShot(True)
        self.probe_timer.setInterval(320)
        self.probe_timer.timeout.connect(self._trigger_probe_from_input)

        self._build_ui()
        self._refresh_cookie_status()

        # Apply Windows 11 native dark titlebar after window handle creation
        QTimer.singleShot(0, lambda: apply_windows_dark_titlebar(int(self.winId())))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        apply_windows_dark_titlebar(int(self.winId()))
        self._sync_scroll_width()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_scroll_width()

    def eventFilter(self, obj, event) -> bool:
        if hasattr(self, "scroll_area") and obj is self.scroll_area.viewport():
            if event.type() == QEvent.Type.Resize:
                self._sync_scroll_width()
        return super().eventFilter(obj, event)

    def _sync_scroll_width(self) -> None:
        if not (hasattr(self, "scroll_area") and hasattr(self, "scroll_content")):
            return
        vw = self.scroll_area.viewport().width()
        if vw <= 100:
            return
        target_w = min(vw, 1040)
        if self.scroll_content.width() != target_w:
            self.scroll_content.setFixedWidth(target_w)
        if hasattr(self, "header_layout"):
            self.header_layout.setDirection(
                QBoxLayout.Direction.TopToBottom
                if target_w < 840
                else QBoxLayout.Direction.LeftToRight
            )
        if hasattr(self, "modes_layout"):
            self.modes_layout.setDirection(
                QBoxLayout.Direction.TopToBottom
                if target_w < 700
                else QBoxLayout.Direction.LeftToRight
            )
        if hasattr(self, "folder_hdr_layout"):
            self.folder_hdr_layout.setDirection(
                QBoxLayout.Direction.TopToBottom
                if target_w < 700
                else QBoxLayout.Direction.LeftToRight
            )

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("RootWidget")
        self.setCentralWidget(root)

        outer_layout = QVBoxLayout(root)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.viewport().installEventFilter(self)
        outer_layout.addWidget(self.scroll_area)

        # Centering host keeps the UI balanced on ultrawide screens and full-width on narrow ones
        self.scroll_host = QWidget()
        self.scroll_host.setObjectName("RootWidget")
        host_layout = QVBoxLayout(self.scroll_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        self.scroll_content = QWidget()
        self.scroll_content.setObjectName("RootWidget")
        self.scroll_content.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        host_layout.addWidget(self.scroll_content)
        self.scroll_area.setWidget(self.scroll_host)

        main_layout = QVBoxLayout(self.scroll_content)
        main_layout.setContentsMargins(20, 14, 20, 16)
        main_layout.setSpacing(10)

        # ------------------------------------------------------------------
        # 1. HEADER BAR (Responsive left-to-right / stacked on narrow)
        # ------------------------------------------------------------------
        self.header_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.header_layout.setContentsMargins(0, 0, 0, 2)
        self.header_layout.setSpacing(12)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(0, 0, 0, 0)
        brand_row.setSpacing(12)
        brand_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        logo_lbl = QLabel()
        logo_lbl.setPixmap(create_app_logo_pixmap(40))
        logo_lbl.setFixedSize(40, 40)
        brand_row.addWidget(logo_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(1)

        app_title = QLabel("wlz-yt")
        app_title.setStyleSheet(f"font-size: 19px; font-weight: 800; color: {TEXT_PRIMARY};")
        title_col.addWidget(app_title)

        app_sub = QLabel("Editor-Safe H.264 + AAC 320k & Zero-Loss Studio Audio")
        app_sub.setWordWrap(True)
        app_sub.setMinimumWidth(0)
        app_sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        app_sub.setStyleSheet(f"font-size: 11.5px; color: {TEXT_SECONDARY};")
        title_col.addWidget(app_sub)

        brand_row.addLayout(title_col, 1)
        self.header_layout.addLayout(brand_row, 1)

        # Wrapping badge strip
        badges_widget = QWidget()
        badges_widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        badges_flow = FlowLayout(badges_widget, margin=0, h_spacing=6, v_spacing=6)

        gpu_text = (
            "GPU • NVENC"
            if self.engine.nvenc_available
            else "CPU • x264 High"
        )
        self.gpu_badge = QLabel(gpu_text)
        self.gpu_badge.setObjectName("BadgeGpu")
        badges_flow.addWidget(self.gpu_badge)

        editor_badge = QLabel("Premiere • AE • Resolve")
        editor_badge.setObjectName("BadgeEditor")
        badges_flow.addWidget(editor_badge)

        self.cookie_header_badge = QLabel("YouTube: Checking...")
        self.cookie_header_badge.setObjectName("BadgeCookie")
        badges_flow.addWidget(self.cookie_header_badge)

        self.header_layout.addWidget(badges_widget, 0, Qt.AlignmentFlag.AlignVCenter)
        main_layout.addLayout(self.header_layout)

        # ------------------------------------------------------------------
        # 2. URL INPUT CARD
        # ------------------------------------------------------------------
        url_card = QFrame()
        url_card.setObjectName("CardFrame")
        url_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        url_card_layout = QVBoxLayout(url_card)
        url_card_layout.setContentsMargins(16, 14, 16, 14)
        url_card_layout.setSpacing(10)

        input_row = QHBoxLayout()
        input_row.setSpacing(10)
        input_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.url_input = QLineEdit()
        self.url_input.setObjectName("UrlInput")
        self.url_input.setFixedHeight(42)
        self.url_input.setPlaceholderText(
            "Paste YouTube video, Shorts, or music link (https://youtube.com/watch?v=...)"
        )
        self.url_input.setClearButtonEnabled(True)
        self.url_input.textChanged.connect(self._on_url_text_changed)
        self.url_input.returnPressed.connect(self._start_download)
        input_row.addWidget(self.url_input, 1)

        self.paste_btn = QPushButton("Paste Link")
        self.paste_btn.setObjectName("PasteBtn")
        self.paste_btn.setFixedHeight(42)
        self.paste_btn.setIcon(create_action_icon("paste", TEXT_PRIMARY, 16))
        self.paste_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.paste_btn.clicked.connect(self._paste_from_clipboard)
        input_row.addWidget(self.paste_btn)

        url_card_layout.addLayout(input_row)

        # Sub-row with status hint + Auto-download toggle
        url_sub_row = QHBoxLayout()
        url_sub_row.setContentsMargins(2, 0, 2, 0)
        url_sub_row.setSpacing(10)
        url_sub_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.probe_status_lbl = QLabel("Ready — Paste a YouTube link and press Download.")
        self.probe_status_lbl.setWordWrap(True)
        self.probe_status_lbl.setMinimumWidth(0)
        self.probe_status_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.probe_status_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        url_sub_row.addWidget(self.probe_status_lbl, 1)

        self.auto_dl_checkbox = QCheckBox("Instant Download on Paste")
        self.auto_dl_checkbox.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.auto_dl_checkbox.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {TEXT_SECONDARY};"
        )
        url_sub_row.addWidget(self.auto_dl_checkbox, 0, Qt.AlignmentFlag.AlignVCenter)

        url_card_layout.addLayout(url_sub_row)

        # Live Video Preview Banner (inside URL card, shown when video metadata arrives)
        self.preview_frame = QFrame()
        self.preview_frame.setObjectName("PreviewCard")
        self.preview_frame.setVisible(False)
        preview_layout = QHBoxLayout(self.preview_frame)
        preview_layout.setContentsMargins(12, 10, 14, 10)
        preview_layout.setSpacing(14)
        preview_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.thumb_widget = RoundedThumbnailWidget(156, 88)
        preview_layout.addWidget(self.thumb_widget, 0, Qt.AlignmentFlag.AlignVCenter)

        meta_col = QVBoxLayout()
        meta_col.setContentsMargins(0, 2, 0, 2)
        meta_col.setSpacing(4)

        self.preview_title_lbl = QLabel("Video Title")
        self.preview_title_lbl.setWordWrap(True)
        self.preview_title_lbl.setMinimumWidth(0)
        self.preview_title_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.preview_title_lbl.setStyleSheet(
            f"font-size: 13.5px; font-weight: 700; color: {TEXT_PRIMARY};"
        )
        meta_col.addWidget(self.preview_title_lbl)

        self.preview_channel_lbl = QLabel("Channel")
        self.preview_channel_lbl.setWordWrap(True)
        self.preview_channel_lbl.setMinimumWidth(0)
        self.preview_channel_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.preview_channel_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {TEXT_SECONDARY};"
        )
        meta_col.addWidget(self.preview_channel_lbl)

        meta_col.addStretch()

        self.preview_specs_lbl = QLabel("")
        self.preview_specs_lbl.setWordWrap(True)
        self.preview_specs_lbl.setMinimumWidth(0)
        self.preview_specs_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.preview_specs_lbl.setStyleSheet(
            f"font-size: 11.5px; font-weight: 700; color: {SUCCESS_COLOR};"
        )
        meta_col.addWidget(self.preview_specs_lbl)

        preview_layout.addLayout(meta_col, 1)
        url_card_layout.addWidget(self.preview_frame)

        main_layout.addWidget(url_card)

        # ------------------------------------------------------------------
        # 3. MODE & QUALITY CONFIGURATION CARD
        # ------------------------------------------------------------------
        config_card = QFrame()
        config_card.setObjectName("CardFrame")
        config_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        config_layout = QVBoxLayout(config_card)
        config_layout.setContentsMargins(16, 12, 16, 12)
        config_layout.setSpacing(9)

        mode_hdr = QLabel("DOWNLOAD MODE")
        mode_hdr.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {TEXT_MUTED};"
        )
        config_layout.addWidget(mode_hdr)

        modes_widget = QWidget()
        modes_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.modes_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, modes_widget)
        self.modes_layout.setContentsMargins(0, 0, 0, 0)
        self.modes_layout.setSpacing(10)

        self.card_both = ModeSelectorCard(
            mode_key="both",
            title="Video + Audio",
            subtitle="H.264 + AAC 320k MP4",
            badge_text="EDITOR",
        )
        self.card_audio = ModeSelectorCard(
            mode_key="audio",
            title="Audio Only",
            subtitle="Zero-Loss Remux, FLAC, WAV, M4A, MP3",
            badge_text="MAX AUDIO",
        )
        self.card_video = ModeSelectorCard(
            mode_key="video",
            title="Video Only",
            subtitle="Muted max-bitrate H.264 MP4",
            badge_text="B-ROLL",
        )

        self.mode_cards = {
            "both": self.card_both,
            "audio": self.card_audio,
            "video": self.card_video,
        }
        for key, card in self.mode_cards.items():
            card.clicked.connect(self._on_mode_selected)
            self.modes_layout.addWidget(card, 1)

        config_layout.addWidget(modes_widget)

        # Dynamic Quality / Audio Format selector section
        self.quality_hdr = QLabel("TARGET VIDEO RESOLUTION (AUTO-CONVERTS VP9/AV1 TO H.264)")
        self.quality_hdr.setWordWrap(True)
        self.quality_hdr.setMinimumWidth(0)
        self.quality_hdr.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {TEXT_MUTED};"
        )
        config_layout.addWidget(self.quality_hdr)

        # Video Resolution Pills Container (wrapping FlowLayout)
        self.res_container = QWidget()
        res_flow = FlowLayout(self.res_container, margin=0, h_spacing=8, v_spacing=8)

        self.res_buttons: dict[str, PillOptionButton] = {}
        res_definitions = [
            ("max", "MAX Available (Auto)"),
            ("2160", "4K (2160p)"),
            ("1440", "1440p (2K)"),
            ("1080", "1080p (FHD)"),
            ("720", "720p (HD)"),
        ]
        for r_key, r_label in res_definitions:
            btn = PillOptionButton(r_key, r_label)
            btn.clicked.connect(lambda checked=False, k=r_key: self._on_res_selected(k))
            self.res_buttons[r_key] = btn
            res_flow.addWidget(btn)

        config_layout.addWidget(self.res_container)

        # Audio Format Pills Container (shown when mode == 'audio')
        self.audio_fmt_container = QWidget()
        audio_outer = QVBoxLayout(self.audio_fmt_container)
        audio_outer.setContentsMargins(0, 0, 0, 0)
        audio_outer.setSpacing(6)

        audio_pills_host = QWidget()
        audio_fmt_flow = FlowLayout(audio_pills_host, margin=0, h_spacing=8, v_spacing=8)

        self.audio_fmt_buttons: dict[str, PillOptionButton] = {}
        audio_fmt_defs = [
            ("original", "Original Master (Zero-Loss)"),
            ("flac", "FLAC • 24-Bit Lossless"),
            ("wav", "WAV • 24-Bit 48kHz PCM"),
            ("m4a", "M4A • AAC 320k"),
            ("mp3", "MP3 • 320k LAME Extreme"),
        ]
        for a_key, a_label in audio_fmt_defs:
            btn = PillOptionButton(a_key, a_label)
            btn.clicked.connect(lambda checked=False, k=a_key: self._on_audio_fmt_selected(k))
            self.audio_fmt_buttons[a_key] = btn
            audio_fmt_flow.addWidget(btn)

        audio_outer.addWidget(audio_pills_host)

        self.audio_algo_hint = QLabel("")
        self.audio_algo_hint.setWordWrap(True)
        self.audio_algo_hint.setMinimumWidth(0)
        self.audio_algo_hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.audio_algo_hint.setStyleSheet(f"font-size: 11.5px; color: {GPU_COLOR};")
        audio_outer.addWidget(self.audio_algo_hint)

        self.audio_fmt_container.setVisible(False)
        config_layout.addWidget(self.audio_fmt_container)

        # Output Folder Section (Editable Path + Browse Dialog + Quick Presets)
        self.folder_hdr_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.folder_hdr_layout.setContentsMargins(0, 2, 0, 0)
        self.folder_hdr_layout.setSpacing(8)

        folder_hdr = QLabel("DOWNLOAD LOCATION")
        folder_hdr.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {TEXT_MUTED};"
        )
        self.folder_hdr_layout.addWidget(folder_hdr, 0, Qt.AlignmentFlag.AlignVCenter)
        self.folder_hdr_layout.addStretch(1)

        quick_widget = QWidget()
        quick_flow = FlowLayout(quick_widget, margin=0, h_spacing=6, v_spacing=6)

        quick_lbl = QLabel("Quick:")
        quick_lbl.setStyleSheet(f"font-size: 11px; font-weight: 600; color: {TEXT_MUTED};")
        quick_flow.addWidget(quick_lbl)

        home_dir = Path.home()
        quick_presets = [
            ("Desktop", str(home_dir / "Desktop")),
            ("Downloads", str(home_dir / "Downloads")),
            ("Videos", str(home_dir / "Videos")),
            ("App Folder", self.default_app_downloads),
        ]
        for preset_name, preset_path in quick_presets:
            q_btn = QPushButton(preset_name)
            q_btn.setObjectName("QuickPathBtn")
            q_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            q_btn.clicked.connect(
                lambda checked=False, p=preset_path: self._set_output_dir(p)
            )
            quick_flow.addWidget(q_btn)

        self.folder_hdr_layout.addWidget(quick_widget, 0, Qt.AlignmentFlag.AlignVCenter)
        config_layout.addLayout(self.folder_hdr_layout)

        folder_row = QHBoxLayout()
        folder_row.setContentsMargins(0, 0, 0, 0)
        folder_row.setSpacing(8)
        folder_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.folder_input = QLineEdit(self.output_dir)
        self.folder_input.setObjectName("FolderInput")
        self.folder_input.setFixedHeight(38)
        self.folder_input.setPlaceholderText("Enter or paste destination folder path (e.g. D:\\Videos)...")
        self.folder_input.editingFinished.connect(self._on_folder_input_edited)
        folder_row.addWidget(self.folder_input, 1)

        change_folder_btn = QPushButton("Browse...")
        change_folder_btn.setObjectName("BrowseFolderBtn")
        change_folder_btn.setFixedHeight(38)
        change_folder_btn.setIcon(create_action_icon("folder", TEXT_PRIMARY, 15))
        change_folder_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        change_folder_btn.clicked.connect(self._choose_output_folder)
        folder_row.addWidget(change_folder_btn)

        open_folder_btn = QPushButton("Open")
        open_folder_btn.setObjectName("SecondaryBtn")
        open_folder_btn.setFixedHeight(38)
        open_folder_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        open_folder_btn.clicked.connect(self._open_output_folder)
        folder_row.addWidget(open_folder_btn)

        config_layout.addLayout(folder_row)

        # ------------------------------------------------------------------
        # YouTube Authentication & 18+ Age Verification (Cookies) Section
        # ------------------------------------------------------------------
        auth_hdr_row = QHBoxLayout()
        auth_hdr_row.setContentsMargins(0, 2, 0, 0)
        auth_hdr_row.setSpacing(8)
        auth_hdr_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        auth_hdr = QLabel("YOUTUBE AUTHENTICATION (18+ & HIGH-QUALITY STREAMS)")
        auth_hdr.setWordWrap(True)
        auth_hdr.setMinimumWidth(0)
        auth_hdr.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {TEXT_MUTED};"
        )
        auth_hdr_row.addWidget(auth_hdr, 1)

        self.cookie_status_badge = QLabel("Checking...")
        self.cookie_status_badge.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {SUCCESS_COLOR};"
        )
        auth_hdr_row.addWidget(self.cookie_status_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        config_layout.addLayout(auth_hdr_row)

        cookie_row = QHBoxLayout()
        cookie_row.setContentsMargins(0, 0, 0, 0)
        cookie_row.setSpacing(8)
        cookie_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.cookie_combo = StyledComboBox()
        self.cookie_combo.setObjectName("CookieCombo")
        self.cookie_combo.setFixedHeight(38)
        self.cookie_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.cookie_combo.addItem("Auto-Detect Browser (Firefox, Chrome, Brave, Edge)", "auto")
        self.cookie_combo.addItem("Mozilla Firefox (Recommended)", "firefox")
        self.cookie_combo.addItem("Brave Browser", "brave")
        self.cookie_combo.addItem("Google Chrome", "chrome")
        self.cookie_combo.addItem("Microsoft Edge", "edge")
        self.cookie_combo.addItem("Custom cookies.txt File...", "file")
        self.cookie_combo.addItem("Disabled (No Cookies)", "none")

        # Select saved source
        idx = self.cookie_combo.findData(self.cookie_source)
        if idx >= 0:
            self.cookie_combo.setCurrentIndex(idx)
        self.cookie_combo.currentIndexChanged.connect(self._on_cookie_source_changed)
        cookie_row.addWidget(self.cookie_combo, 1)

        self.cookie_browse_btn = QPushButton("Load cookies.txt...")
        self.cookie_browse_btn.setObjectName("SecondaryBtn")
        self.cookie_browse_btn.setFixedHeight(38)
        self.cookie_browse_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.cookie_browse_btn.clicked.connect(self._choose_cookie_file)
        self.cookie_browse_btn.setVisible(self.cookie_source == "file")
        cookie_row.addWidget(self.cookie_browse_btn)

        self.cookie_refresh_btn = QPushButton("Verify Login")
        self.cookie_refresh_btn.setObjectName("SecondaryBtn")
        self.cookie_refresh_btn.setFixedHeight(38)
        self.cookie_refresh_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.cookie_refresh_btn.clicked.connect(self._refresh_cookie_status)
        cookie_row.addWidget(self.cookie_refresh_btn)

        config_layout.addLayout(cookie_row)

        self.cookie_detail_lbl = QLabel("")
        self.cookie_detail_lbl.setWordWrap(True)
        self.cookie_detail_lbl.setMinimumWidth(0)
        self.cookie_detail_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.cookie_detail_lbl.setStyleSheet(f"font-size: 11.5px; color: {TEXT_MUTED};")
        config_layout.addWidget(self.cookie_detail_lbl)

        main_layout.addWidget(config_card)

        # ------------------------------------------------------------------
        # 4. DOWNLOAD ACTION & LIVE PROGRESS CARD
        # ------------------------------------------------------------------
        action_card = QFrame()
        action_card.setObjectName("CardFrame")
        action_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        action_layout = QVBoxLayout(action_card)
        action_layout.setContentsMargins(16, 14, 16, 14)
        action_layout.setSpacing(10)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self.download_btn = QPushButton("Download && Encode Now")
        self.download_btn.setObjectName("PrimaryDownloadBtn")
        self.download_btn.setIcon(create_action_icon("download", "#FFFFFF", 18))
        self.download_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.download_btn.setFixedHeight(46)
        self.download_btn.clicked.connect(self._start_download)
        btn_row.addWidget(self.download_btn, 1)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("CancelBtn")
        self.cancel_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.cancel_btn.setFixedHeight(46)
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self._cancel_download)
        btn_row.addWidget(self.cancel_btn)

        action_layout.addLayout(btn_row)

        # Progress bar & status telemetry
        self.progress_bar = SmoothProgressBar()
        action_layout.addWidget(self.progress_bar)

        telemetry_col = QVBoxLayout()
        telemetry_col.setContentsMargins(2, 0, 2, 0)
        telemetry_col.setSpacing(3)

        telemetry_row = QHBoxLayout()
        telemetry_row.setContentsMargins(0, 0, 0, 0)
        telemetry_row.setSpacing(10)

        self.status_main_lbl = QLabel("Idle • Ready to download")
        self.status_main_lbl.setWordWrap(True)
        self.status_main_lbl.setMinimumWidth(0)
        self.status_main_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.status_main_lbl.setStyleSheet(
            f"font-size: 12.5px; font-weight: 600; color: {TEXT_SECONDARY};"
        )
        telemetry_row.addWidget(self.status_main_lbl, 1)

        self.telemetry_right_lbl = QLabel("")
        self.telemetry_right_lbl.setWordWrap(True)
        self.telemetry_right_lbl.setMinimumWidth(0)
        self.telemetry_right_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.telemetry_right_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.telemetry_right_lbl.setVisible(False)
        self.telemetry_right_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 700; color: {GPU_COLOR};"
        )
        telemetry_row.addWidget(self.telemetry_right_lbl, 1)

        telemetry_col.addLayout(telemetry_row)
        action_layout.addLayout(telemetry_col)
        main_layout.addWidget(action_card)

        # ------------------------------------------------------------------
        # 5. COMPLETED DOWNLOADS SECTION
        # ------------------------------------------------------------------
        history_hdr_row = QHBoxLayout()
        history_hdr_row.setContentsMargins(4, 2, 4, 0)
        history_hdr = QLabel("COMPLETED DOWNLOADS")
        history_hdr.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {TEXT_MUTED};"
        )
        history_hdr_row.addWidget(history_hdr)
        history_hdr_row.addStretch()
        main_layout.addLayout(history_hdr_row)

        self.history_container = QWidget()
        self.history_layout = QVBoxLayout(self.history_container)
        self.history_layout.setContentsMargins(0, 0, 0, 0)
        self.history_layout.setSpacing(8)

        self.empty_history_lbl = QLabel(
            "Downloaded files will appear here with instant Play and Show in Folder buttons."
        )
        self.empty_history_lbl.setWordWrap(True)
        self.empty_history_lbl.setMinimumWidth(0)
        self.empty_history_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.empty_history_lbl.setStyleSheet(
            f"font-size: 12px; color: {TEXT_MUTED}; padding: 6px 4px;"
        )
        self.history_layout.addWidget(self.empty_history_lbl)

        main_layout.addWidget(self.history_container)
        main_layout.addStretch()

        # Set initial active states
        self._on_mode_selected("both")
        self._on_res_selected("max")
        self._on_audio_fmt_selected(self.selected_audio_fmt)

    # ----------------------------------------------------------------------
    # UI INTERACTION HANDLERS
    # ----------------------------------------------------------------------
    def _on_mode_selected(self, mode_key: str) -> None:
        self.selected_mode = mode_key
        for k, card in self.mode_cards.items():
            card.set_selected(k == mode_key)

        if mode_key == "audio":
            self.quality_hdr.setText("AUDIO OUTPUT VARIANT (OPUS / HIGH-BITRATE MASTER PIPELINE)")
            self.res_container.setVisible(False)
            self.audio_fmt_container.setVisible(True)
        else:
            self.quality_hdr.setText(
                "TARGET VIDEO RESOLUTION (AUTO-ENCODES VP9/AV1 TO H.264 FOR EDITORS)"
            )
            self.audio_fmt_container.setVisible(False)
            self.res_container.setVisible(True)

    def _on_res_selected(self, res_key: str) -> None:
        self.selected_res_key = res_key
        for k, btn in self.res_buttons.items():
            btn.set_active(k == res_key)

    def _on_audio_fmt_selected(self, fmt_key: str) -> None:
        self.selected_audio_fmt = fmt_key
        for k, btn in self.audio_fmt_buttons.items():
            btn.set_active(k == fmt_key)
        self.audio_algo_hint.setText(AUDIO_FORMAT_HINTS.get(fmt_key, ""))
        self._save_settings()

    def _paste_from_clipboard(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard:
            text = (clipboard.text() or "").strip()
            if text:
                cleaned = normalize_youtube_url(text)
                self.url_input.setText(cleaned)
                if self.auto_dl_checkbox.isChecked() and is_likely_youtube_url(cleaned):
                    self._start_download()

    def _load_settings(self) -> dict:
        defaults = {
            "output_dir": self.default_app_downloads,
            "cookie_source": "auto",
            "cookie_file": "",
            "audio_format": "mp3",
        }
        try:
            if self.settings_path.is_file():
                data = json.loads(self.settings_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    saved_dir = (data.get("output_dir") or "").strip()
                    if saved_dir:
                        defaults["output_dir"] = os.path.normpath(saved_dir)
                    if "cookie_source" in data:
                        defaults["cookie_source"] = str(data["cookie_source"])
                    if "cookie_file" in data:
                        defaults["cookie_file"] = str(data["cookie_file"])
                    if "audio_format" in data:
                        defaults["audio_format"] = str(data["audio_format"])
        except Exception:
            pass
        return defaults

    def _save_settings(self) -> None:
        try:
            payload = {
                "output_dir": self.output_dir,
                "cookie_source": self.cookie_source,
                "cookie_file": self.cookie_file,
                "audio_format": getattr(self, "selected_audio_fmt", "mp3"),
            }
            self.settings_path.write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _on_cookie_source_changed(self, index: int) -> None:
        source_key = self.cookie_combo.currentData() or "auto"
        self.cookie_source = source_key
        self.cookie_browse_btn.setVisible(source_key == "file")
        if source_key == "file" and not self.cookie_file:
            self._choose_cookie_file()
        else:
            self.engine.set_cookies(self.cookie_source, self.cookie_file)
            self._save_settings()
            self._refresh_cookie_status()

    def _choose_cookie_file(self) -> None:
        start_dir = os.path.dirname(self.cookie_file) if self.cookie_file else str(self.project_root)
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Select Netscape/YouTube cookies.txt file",
            start_dir,
            "Text files (*.txt);;All files (*.*)",
        )
        if chosen:
            self.cookie_file = os.path.normpath(chosen)
            self.cookie_source = "file"
            idx = self.cookie_combo.findData("file")
            if idx >= 0:
                self.cookie_combo.blockSignals(True)
                self.cookie_combo.setCurrentIndex(idx)
                self.cookie_combo.blockSignals(False)
            self.cookie_browse_btn.setVisible(True)
            self.engine.set_cookies("file", self.cookie_file)
            self._save_settings()
            self._refresh_cookie_status()

    def _refresh_cookie_status(self) -> None:
        self.engine.set_cookies(self.cookie_source, self.cookie_file)
        status = self.engine.get_cookie_status()
        self._update_cookie_ui(status)

    def _update_cookie_ui(self, status: CookieStatus) -> None:
        if status.available and status.is_logged_in:
            badge_text = f"YouTube: {status.resolved_source.capitalize()} ✓"
            self.cookie_header_badge.setText(badge_text)
            self.cookie_header_badge.setStyleSheet(
                f"background-color: #0D2821; color: {SUCCESS_COLOR}; border: 1px solid #18493B; "
                f"border-radius: 6px; padding: 4px 9px; font-size: 11px; font-weight: 700;"
            )
            self.cookie_status_badge.setText("Unlocked ✓")
            self.cookie_status_badge.setStyleSheet(f"font-size: 11px; font-weight: 700; color: {SUCCESS_COLOR};")
            self.cookie_detail_lbl.setText(status.detail_msg)
            self.cookie_detail_lbl.setStyleSheet(f"font-size: 11.5px; font-weight: 600; color: {SUCCESS_COLOR};")
        elif status.available:
            badge_text = f"YouTube: {status.resolved_source.capitalize()} (Active)"
            self.cookie_header_badge.setText(badge_text)
            self.cookie_header_badge.setStyleSheet(
                f"background-color: #0F2437; color: {GPU_COLOR}; border: 1px solid #1B3F5E; "
                f"border-radius: 6px; padding: 4px 9px; font-size: 11px; font-weight: 700;"
            )
            self.cookie_status_badge.setText("Active")
            self.cookie_status_badge.setStyleSheet(f"font-size: 11px; font-weight: 700; color: {GPU_COLOR};")
            self.cookie_detail_lbl.setText(status.detail_msg)
            self.cookie_detail_lbl.setStyleSheet(f"font-size: 11.5px; color: {TEXT_SECONDARY};")
        else:
            badge_text = "YouTube: No Login"
            self.cookie_header_badge.setText(badge_text)
            self.cookie_header_badge.setStyleSheet(
                f"background-color: #2B1620; color: {ERROR_COLOR}; border: 1px solid #592234; "
                f"border-radius: 6px; padding: 4px 9px; font-size: 11px; font-weight: 700;"
            )
            self.cookie_status_badge.setText("18+ Restricted")
            self.cookie_status_badge.setStyleSheet(f"font-size: 11px; font-weight: 700; color: {WARNING_COLOR};")
            self.cookie_detail_lbl.setText(status.detail_msg)
            self.cookie_detail_lbl.setStyleSheet(f"font-size: 11.5px; color: {WARNING_COLOR};")

    def _set_output_dir(self, new_dir: str) -> bool:
        cleaned = (new_dir or "").strip().strip('"')
        if not cleaned:
            self.folder_input.setText(self.output_dir)
            return False
        norm_dir = os.path.normpath(os.path.expanduser(cleaned))
        try:
            os.makedirs(norm_dir, exist_ok=True)
            self.output_dir = norm_dir
            self.folder_input.setText(self.output_dir)
            self._save_settings()
            self.status_main_lbl.setText(f"Download path set to: {self.output_dir}")
            self.status_main_lbl.setStyleSheet(
                f"font-size: 12.5px; font-weight: 600; color: {SUCCESS_COLOR};"
            )
            return True
        except Exception as exc:
            self.folder_input.setText(self.output_dir)
            self.status_main_lbl.setText(f"Invalid folder path: {exc}")
            self.status_main_lbl.setStyleSheet(
                f"font-size: 12.5px; font-weight: 700; color: {ERROR_COLOR};"
            )
            return False

    def _on_folder_input_edited(self) -> None:
        entered = self.folder_input.text().strip()
        if entered and os.path.normpath(entered) != self.output_dir:
            self._set_output_dir(entered)
        elif not entered:
            self.folder_input.setText(self.output_dir)

    def _choose_output_folder(self) -> None:
        start_dir = self.output_dir if os.path.isdir(self.output_dir) else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Select Download Destination Folder",
            start_dir,
        )
        if chosen:
            self._set_output_dir(chosen)

    def _open_output_folder(self) -> None:
        entered = self.folder_input.text().strip()
        if entered and os.path.normpath(entered) != self.output_dir:
            self._set_output_dir(entered)
        os.makedirs(self.output_dir, exist_ok=True)
        try:
            os.startfile(self.output_dir)  # type: ignore[attr-defined]
        except Exception:
            pass

    def _on_url_text_changed(self, text: str) -> None:
        cleaned = normalize_youtube_url(text)
        if not cleaned:
            self.preview_frame.setVisible(False)
            self.current_meta = None
            self.last_probed_url = ""
            self.probe_status_lbl.setText("Ready — Paste a YouTube link and press Download.")
            self.probe_status_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
            self._reset_res_pill_labels()
            return

        if is_likely_youtube_url(cleaned):
            if cleaned != self.last_probed_url:
                self.probe_status_lbl.setText("Fetching video info & available resolutions...")
                self.probe_status_lbl.setStyleSheet(f"font-size: 12px; color: {GPU_COLOR};")
                self.probe_timer.start()
        else:
            self.probe_status_lbl.setText("Enter a full YouTube video or Shorts URL.")
            self.probe_status_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT_MUTED};")

    def _trigger_probe_from_input(self) -> None:
        cleaned = normalize_youtube_url(self.url_input.text())
        if not is_likely_youtube_url(cleaned):
            return
        if cleaned == self.last_probed_url and self.current_meta is not None:
            return

        self.last_probed_url = cleaned
        self.probe_worker = ProbeWorker(self.engine, cleaned)
        self.probe_worker.finished_probe.connect(self._on_probe_finished)
        self.probe_worker.failed_probe.connect(self._on_probe_failed)
        self.probe_worker.start()

    def _on_probe_finished(self, meta: VideoMetadata, thumb_bytes: bytes) -> None:
        # Ignore stale probe results if the input changed
        current_cleaned = normalize_youtube_url(self.url_input.text())
        if current_cleaned and normalize_youtube_url(meta.url) != current_cleaned:
            return

        self.current_meta = meta
        self.preview_title_lbl.setText(meta.title)
        views_str = f"{meta.view_count:,} views" if meta.view_count else ""
        channel_line = f"{meta.uploader} • {views_str}" if views_str else meta.uploader
        self.preview_channel_lbl.setText(channel_line)

        max_badge = format_resolution_badge(meta.max_height, meta.max_fps)
        audio_codec_str = f"{meta.best_audio_codec.upper()} " if meta.best_audio_codec else ""
        audio_asr_str = f" {meta.best_audio_asr / 1000:g}kHz" if meta.best_audio_asr else ""
        self.preview_specs_lbl.setText(
            f"Max: {max_badge} • Audio: {audio_codec_str}~{meta.best_audio_kbps}kbps{audio_asr_str} • Editor-Ready"
        )

        pix = QPixmap()
        if thumb_bytes:
            pix.loadFromData(thumb_bytes)
        self.thumb_widget.set_thumbnail(pix, meta.duration)
        self.preview_frame.setVisible(True)

        self.probe_status_lbl.setText(
            f"Stream verified: Up to {max_badge} available. Ready to download."
        )
        self.probe_status_lbl.setStyleSheet(f"font-size: 12px; font-weight: 600; color: {SUCCESS_COLOR};")

        # Update resolution pills to highlight available heights on this video
        self.res_buttons["max"].setText(f"MAX ({max_badge})")
        avail_set = set(meta.available_heights)
        for h_str, label_prefix in [
            ("2160", "4K (2160p)"),
            ("1440", "1440p (2K)"),
            ("1080", "1080p (FHD)"),
            ("720", "720p (HD)"),
        ]:
            h_val = int(h_str)
            btn = self.res_buttons[h_str]
            is_avail = h_val in avail_set
            btn.set_available_highlight(is_avail)
            btn.setText(f"{label_prefix} ✓" if is_avail else label_prefix)

    def _on_probe_failed(self, url: str, err_msg: str) -> None:
        current_cleaned = normalize_youtube_url(self.url_input.text())
        if current_cleaned == url:
            if "Age-Restricted" in err_msg or "confirm your age" in err_msg.lower():
                self.probe_status_lbl.setText(
                    "Age-Restricted Video (18+): YouTube sign-in required. Select Firefox or load cookies.txt below."
                )
                self.probe_status_lbl.setStyleSheet(f"font-size: 12px; font-weight: 600; color: {WARNING_COLOR};")
            else:
                self.probe_status_lbl.setText(
                    "Link ready (metadata will be resolved during download)."
                )
                self.probe_status_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")

    def _reset_res_pill_labels(self) -> None:
        self.res_buttons["max"].setText("MAX Available (Auto)")
        for h_str, label_prefix in [
            ("2160", "4K (2160p)"),
            ("1440", "1440p (2K)"),
            ("1080", "1080p (FHD)"),
            ("720", "720p (HD)"),
        ]:
            self.res_buttons[h_str].setText(label_prefix)
            self.res_buttons[h_str].set_available_highlight(False)

    # ----------------------------------------------------------------------
    # DOWNLOAD & ENCODE EXECUTION
    # ----------------------------------------------------------------------
    def _start_download(self) -> None:
        if self.download_worker and self.download_worker.isRunning():
            return

        # Apply any edited download path from the folder input box
        entered_folder = self.folder_input.text().strip()
        if entered_folder and os.path.normpath(entered_folder) != self.output_dir:
            if not self._set_output_dir(entered_folder):
                return

        raw_url = self.url_input.text().strip()
        if not raw_url:
            # If URL input is empty, check clipboard automatically so 1-click download works!
            clip = QApplication.clipboard()
            if clip and is_likely_youtube_url(clip.text() or ""):
                raw_url = normalize_youtube_url(clip.text())
                self.url_input.setText(raw_url)

        url = normalize_youtube_url(raw_url)
        if not url or not is_likely_youtube_url(url):
            self.status_main_lbl.setText("Please paste a valid YouTube link first.")
            self.status_main_lbl.setStyleSheet(
                f"font-size: 12.5px; font-weight: 700; color: {ERROR_COLOR};"
            )
            self.url_input.setFocus()
            return

        max_height: Optional[int] = None
        if self.selected_res_key != "max":
            max_height = int(self.selected_res_key)

        self.download_btn.setEnabled(False)
        self.download_btn.setText("Downloading && Encoding...")
        self.cancel_btn.setVisible(True)
        self.progress_bar.set_progress(2.0, "probing")
        self.status_main_lbl.setText("Initializing high-speed YouTube stream...")
        self.status_main_lbl.setStyleSheet(
            f"font-size: 12.5px; font-weight: 600; color: {TEXT_PRIMARY};"
        )
        self.telemetry_right_lbl.setText("")
        self.telemetry_right_lbl.setVisible(False)

        self.download_worker = DownloadWorker(
            engine=self.engine,
            url=url,
            output_dir=self.output_dir,
            mode=self.selected_mode,
            max_height=max_height,
            audio_format=self.selected_audio_fmt,
            prefetched_meta=self.current_meta,
        )
        self.download_worker.progress_changed.connect(self._on_download_progress)
        self.download_worker.download_completed.connect(self._on_download_completed)
        self.download_worker.download_failed.connect(self._on_download_failed)
        self.download_worker.start()

    def _cancel_download(self) -> None:
        if self.download_worker and self.download_worker.isRunning():
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setText("Cancelling...")
            self.download_worker.cancel()

    def _on_download_progress(self, upd: ProgressUpdate) -> None:
        self.progress_bar.set_progress(upd.percent, upd.phase)
        self.status_main_lbl.setText(upd.status_text)

        right_parts = []
        if upd.encoder_badge:
            right_parts.append(upd.encoder_badge)
        if upd.speed_text:
            right_parts.append(upd.speed_text)
        if upd.eta_text:
            right_parts.append(upd.eta_text)
        right_text = " • ".join(right_parts)
        self.telemetry_right_lbl.setText(right_text)
        self.telemetry_right_lbl.setVisible(bool(right_text))

    def _on_download_completed(self, result: DownloadResult) -> None:
        self.download_btn.setEnabled(True)
        self.download_btn.setText("Download && Encode Now")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("Cancel")

        self.progress_bar.set_progress(100.0, "finished")
        size_mb = result.file_size_bytes / (1024 * 1024)
        self.status_main_lbl.setText(
            f"Saved: {os.path.basename(result.output_path)} ({size_mb:.1f} MB in {result.elapsed_seconds:.1f}s)"
        )
        self.status_main_lbl.setStyleSheet(
            f"font-size: 12.5px; font-weight: 700; color: {SUCCESS_COLOR};"
        )
        right_text = f"{result.resolution_label} • {result.codec_summary}"
        self.telemetry_right_lbl.setText(right_text)
        self.telemetry_right_lbl.setVisible(True)

        # Add to completed history list at the top
        if self.empty_history_lbl.isVisible():
            self.empty_history_lbl.setVisible(False)

        row_widget = HistoryItemRow(result)
        self.history_layout.insertWidget(0, row_widget)

    def _on_download_failed(self, error_msg: str, was_cancelled: bool) -> None:
        self.download_btn.setEnabled(True)
        self.download_btn.setText("Download && Encode Now")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("Cancel")

        if was_cancelled:
            self.progress_bar.set_progress(0.0, "idle")
            self.status_main_lbl.setText("Download cancelled.")
            self.status_main_lbl.setStyleSheet(
                f"font-size: 12.5px; font-weight: 600; color: {TEXT_SECONDARY};"
            )
            self.telemetry_right_lbl.setText("")
            self.telemetry_right_lbl.setVisible(False)
        else:
            self.progress_bar.set_progress(100.0, "error")
            if "Age-Restricted" in error_msg or "confirm your age" in error_msg.lower():
                self.status_main_lbl.setText(
                    "Error: Age-Restricted Video (18+) — YouTube sign-in required. Select Firefox or load cookies.txt in settings."
                )
            else:
                self.status_main_lbl.setText(f"Error: {error_msg}")
            self.status_main_lbl.setStyleSheet(
                f"font-size: 12.5px; font-weight: 700; color: {ERROR_COLOR};"
            )
            self.telemetry_right_lbl.setText("")
            self.telemetry_right_lbl.setVisible(False)
