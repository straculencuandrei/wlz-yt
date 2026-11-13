"""Theme palette, Windows 11 dark titlebar helper, and QSS styles for YT All-In-One Studio."""

from __future__ import annotations

import ctypes
import sys

# Color Palette (Obsidian Studio Dark Theme)
BG_MAIN = "#0B0D13"
BG_CARD = "#121621"
BG_CARD_ELEVATED = "#181D2C"
BG_INPUT = "#0E121B"
BG_HOVER = "#1E2538"

BORDER_SUBTLE = "#22293E"
BORDER_FOCUS = "#FF2E54"

ACCENT_PRIMARY = "#FF2E54"
ACCENT_HOVER = "#FF4769"
ACCENT_PRESSED = "#E01E42"
ACCENT_SOFT_BG = "#2A121D"

TEXT_PRIMARY = "#F3F5F9"
TEXT_SECONDARY = "#9BA3B8"
TEXT_MUTED = "#646E87"

SUCCESS_COLOR = "#10B981"
SUCCESS_BG = "#0D2821"
SUCCESS_BORDER = "#18493B"

GPU_COLOR = "#38BDF8"
GPU_BG = "#0F2437"
GPU_BORDER = "#1B3F5E"

WARNING_COLOR = "#F59E0B"
ERROR_COLOR = "#EF4444"


def apply_windows_dark_titlebar(hwnd: int) -> None:
    """Apply Windows 10/11 native dark mode titlebar and custom #0B0D13 caption color."""
    if sys.platform != "win32":
        return
    try:
        dwmapi = ctypes.windll.dwmapi
        # DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        dark_mode = ctypes.c_int(1)
        dwmapi.DwmSetWindowAttribute(
            int(hwnd),
            20,
            ctypes.byref(dark_mode),
            ctypes.sizeof(dark_mode),
        )
        # DWMWA_CAPTION_COLOR = 35 (COLORREF is 0x00BBGGRR -> #0B0D13 is R=0x0B, G=0x0D, B=0x13 -> 0x00130D0B)
        caption_color = ctypes.c_int(0x00130D0B)
        dwmapi.DwmSetWindowAttribute(
            int(hwnd),
            35,
            ctypes.byref(caption_color),
            ctypes.sizeof(caption_color),
        )
        # DWMWA_TEXT_COLOR = 36 (0x00F9F5F3)
        text_color = ctypes.c_int(0x00F9F5F3)
        dwmapi.DwmSetWindowAttribute(
            int(hwnd),
            36,
            ctypes.byref(text_color),
            ctypes.sizeof(text_color),
        )
    except Exception:
        pass


GLOBAL_STYLESHEET = f"""
QMainWindow, QWidget#RootWidget {{
    background-color: {BG_MAIN};
    color: {TEXT_PRIMARY};
    font-family: "Segoe UI", "Segoe UI Variable Display", sans-serif;
}}

QScrollArea {{
    background-color: transparent;
    border: none;
}}

QScrollBar:vertical {{
    background: {BG_MAIN};
    width: 8px;
    margin: 0px;
    border-radius: 4px;
}}

QScrollBar::handle:vertical {{
    background: #283048;
    min-height: 24px;
    border-radius: 4px;
}}

QScrollBar::handle:vertical:hover {{
    background: #3A4668;
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QFrame#CardFrame {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 13px;
}}

QFrame#PreviewCard {{
    background-color: {BG_CARD_ELEVATED};
    border: 1px solid #2A334D;
    border-radius: 11px;
}}

QFrame#HistoryRow {{
    background-color: {BG_CARD_ELEVATED};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 10px;
}}

QLineEdit#UrlInput {{
    background-color: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1.5px solid #262F47;
    border-radius: 10px;
    padding: 0px 14px;
    min-height: 40px;
    font-size: 13.5px;
    font-weight: 500;
    selection-background-color: {ACCENT_PRIMARY};
}}

QLineEdit#UrlInput:focus {{
    border: 1.5px solid {ACCENT_PRIMARY};
    background-color: #101521;
}}

QPushButton#PasteBtn {{
    background-color: #1E2538;
    color: {TEXT_PRIMARY};
    border: 1px solid #2E3854;
    border-radius: 10px;
    padding: 0px 16px;
    min-height: 41px;
    font-size: 13px;
    font-weight: 600;
}}

QPushButton#PasteBtn:hover {{
    background-color: #28314A;
    border-color: #3E4C70;
}}

QPushButton#PasteBtn:pressed {{
    background-color: #191F30;
}}

QPushButton#SecondaryBtn {{
    background-color: #181E2E;
    color: {TEXT_SECONDARY};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 8px;
    padding: 0px 12px;
    min-height: 36px;
    font-size: 12px;
    font-weight: 600;
}}

QPushButton#SecondaryBtn:hover {{
    background-color: #222A40;
    color: {TEXT_PRIMARY};
    border-color: #333E5C;
}}

QLineEdit#FolderInput {{
    background-color: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1.5px solid #262F47;
    border-radius: 9px;
    padding: 0px 12px;
    min-height: 35px;
    font-size: 12.5px;
    font-weight: 600;
    selection-background-color: {ACCENT_PRIMARY};
}}

QLineEdit#FolderInput:focus {{
    border: 1.5px solid {ACCENT_PRIMARY};
    background-color: #101521;
}}

QPushButton#BrowseFolderBtn {{
    background-color: #1E263B;
    color: {TEXT_PRIMARY};
    border: 1.5px solid #354469;
    border-radius: 9px;
    padding: 0px 14px;
    min-height: 35px;
    font-size: 12.5px;
    font-weight: 700;
}}

QPushButton#BrowseFolderBtn:hover {{
    background-color: #27324D;
    border-color: {ACCENT_PRIMARY};
    color: #FFFFFF;
}}

QPushButton#QuickPathBtn {{
    background-color: #141926;
    color: {TEXT_SECONDARY};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 7px;
    padding: 4px 10px;
    font-size: 11px;
    font-weight: 600;
}}

QPushButton#QuickPathBtn:hover {{
    background-color: #1E2538;
    color: {TEXT_PRIMARY};
    border-color: #333E5C;
}}

QPushButton#PrimaryDownloadBtn {{
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #FF244C, stop:1 #FF5136);
    color: #FFFFFF;
    border: none;
    border-radius: 11px;
    padding: 12px 22px;
    font-size: 14.5px;
    font-weight: 700;
}}

QPushButton#PrimaryDownloadBtn:hover {{
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #FF3B5F, stop:1 #FF644C);
}}

QPushButton#PrimaryDownloadBtn:pressed {{
    background-color: #D91B3F;
}}

QPushButton#PrimaryDownloadBtn:disabled {{
    background-color: #232A3D;
    color: #636D87;
}}

QPushButton#CancelBtn {{
    background-color: #2B1620;
    color: #FF6B84;
    border: 1px solid #592234;
    border-radius: 11px;
    padding: 12px 18px;
    font-size: 13.5px;
    font-weight: 600;
}}

QPushButton#CancelBtn:hover {{
    background-color: #3D1C2B;
    color: #FF8DA0;
}}

QLabel#BadgeGpu {{
    background-color: {GPU_BG};
    color: {GPU_COLOR};
    border: 1px solid {GPU_BORDER};
    border-radius: 6px;
    padding: 4px 9px;
    font-size: 11px;
    font-weight: 700;
}}

QLabel#BadgeEditor {{
    background-color: {SUCCESS_BG};
    color: {SUCCESS_COLOR};
    border: 1px solid {SUCCESS_BORDER};
    border-radius: 6px;
    padding: 4px 9px;
    font-size: 11px;
    font-weight: 700;
}}

QLabel#BadgeCookie {{
    background-color: #1A1F30;
    color: {TEXT_PRIMARY};
    border: 1px solid #2F3956;
    border-radius: 6px;
    padding: 4px 9px;
    font-size: 11px;
    font-weight: 700;
}}

QComboBox#CookieCombo {{
    background-color: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1.5px solid #262F47;
    border-radius: 9px;
    padding: 0px 30px 0px 12px;
    min-height: 35px;
    font-size: 12.5px;
    font-weight: 600;
}}

QComboBox#CookieCombo:hover {{
    border-color: {GPU_COLOR};
}}

QComboBox#CookieCombo:focus {{
    border-color: {ACCENT_PRIMARY};
}}

QComboBox#CookieCombo::drop-down {{
    border: none;
    width: 28px;
}}

QComboBox#CookieCombo::down-arrow {{
    image: none;
    width: 0px;
    height: 0px;
}}

QComboBox#CookieCombo QAbstractItemView {{
    background-color: {BG_CARD};
    color: {TEXT_PRIMARY};
    border: 1px solid #2A334D;
    selection-background-color: {BG_HOVER};
    selection-color: {GPU_COLOR};
    border-radius: 6px;
    padding: 4px;
}}
"""
