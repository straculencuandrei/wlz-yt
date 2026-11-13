"""Entry point for YT All-In-One Studio standalone Windows desktop app."""

from __future__ import annotations

import ctypes
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from ui.icons import create_app_icon
from ui.main_window import MainWindow


def main() -> int:
    # Ensure Windows taskbar displays our custom application icon
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "ytallinone.studio.desktop.1"
            )
        except Exception:
            pass

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("YT All-In-One Studio")
    app.setOrganizationName("YTAllInOneStudio")
    app.setWindowIcon(create_app_icon())

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
