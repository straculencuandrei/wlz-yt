"""Custom modern PyQt6 widgets for YT All-In-One Studio."""

from __future__ import annotations

import os
import subprocess
from typing import Callable, Optional

from PyQt6.QtCore import QPoint, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.engine import DownloadResult
from ui.icons import create_action_icon, create_mode_icon
from ui.theme import (
    ACCENT_PRIMARY,
    ACCENT_SOFT_BG,
    BG_CARD_ELEVATED,
    BG_HOVER,
    BORDER_SUBTLE,
    GPU_COLOR,
    SUCCESS_COLOR,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)


class FlowLayout(QLayout):
    """Responsive wrapping layout for pills, badges, and action chips."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        margin: int = 0,
        h_spacing: int = 8,
        v_spacing: int = 8,
    ) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._h_space = h_spacing
        self._v_space = v_spacing
        self.setContentsMargins(margin, margin, margin, margin)

    def __del__(self) -> None:
        while self._items:
            item = self._items.pop()
            del item

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> Optional[QLayoutItem]:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> Optional[QLayoutItem]:
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        total_w = 0
        max_h = 0
        visible_count = 0
        for item in self._items:
            w = item.widget()
            if w is not None and not w.isVisibleTo(w.parentWidget()):
                continue
            hint = item.sizeHint()
            total_w += hint.width()
            max_h = max(max_h, hint.height())
            visible_count += 1
        if visible_count > 1:
            total_w += self._h_space * (visible_count - 1)
        margins = self.contentsMargins()
        return QSize(
            total_w + margins.left() + margins.right(),
            max_h + margins.top() + margins.bottom(),
        )

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            w = item.widget()
            if w is not None and not w.isVisibleTo(w.parentWidget()):
                continue
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        left, top, right, bottom = self.getContentsMargins()
        effective = rect.adjusted(left, top, -right, -bottom)
        x = effective.x()
        y = effective.y()
        line_height = 0

        for item in self._items:
            w = item.widget()
            if w is not None and not w.isVisibleTo(w.parentWidget()):
                continue
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h_space
            if next_x - self._h_space > effective.right() + 1 and line_height > 0:
                x = effective.x()
                y = y + line_height + self._v_space
                next_x = x + hint.width() + self._h_space
                line_height = 0

            if not test_only:
                item_w = min(hint.width(), max(1, effective.width()))
                item.setGeometry(QRect(QPoint(x, y), QSize(item_w, hint.height())))

            x = next_x
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + bottom


class StyledComboBox(QComboBox):
    """QComboBox with a crisp painted HiDPI chevron arrow on the right."""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(TEXT_SECONDARY), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        cx = self.width() - 16.0
        cy = self.height() / 2.0
        painter.drawLine(QRectF(cx - 4.0, cy - 2.0, 4.0, 4.0).topLeft(), QRectF(cx, cy + 2.5, 0, 0).topLeft())
        painter.drawLine(QRectF(cx, cy + 2.5, 0, 0).topLeft(), QRectF(cx + 4.0, cy - 2.0, 0, 0).topLeft())
        painter.end()


class ModeSelectorCard(QFrame):
    """Tactile selectable card for 'Video + Audio', 'Audio Only', and 'Video Only' modes."""

    clicked = pyqtSignal(str)

    def __init__(
        self,
        mode_key: str,
        title: str,
        subtitle: str,
        badge_text: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.mode_key = mode_key
        self._selected = False
        self._hovered = False

        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setMinimumHeight(68)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(5)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(7)
        top_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.icon_label = QLabel()
        self.icon_label.setFixedSize(24, 24)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_row.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignVCenter)

        self.title_label = QLabel(title)
        self.title_label.setMinimumWidth(0)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        top_row.addWidget(self.title_label, 1, Qt.AlignmentFlag.AlignVCenter)

        self.badge_label = QLabel(badge_text)
        self.badge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_row.addWidget(self.badge_label, 0, Qt.AlignmentFlag.AlignVCenter)

        layout.addLayout(top_row)

        self.sub_label = QLabel(subtitle)
        self.sub_label.setWordWrap(True)
        self.sub_label.setMinimumWidth(0)
        self.sub_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.sub_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.sub_label.setStyleSheet(
            f"font-size: 11px; color: {TEXT_SECONDARY}; background: transparent;"
        )
        layout.addWidget(self.sub_label)

        self._refresh_visuals()

    def set_selected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self._refresh_visuals()
            self.update()

    def _refresh_visuals(self) -> None:
        icon_color = ACCENT_PRIMARY if self._selected else TEXT_SECONDARY
        self.icon_label.setPixmap(create_mode_icon(self.mode_key, icon_color, 24))

        if self._selected:
            self.badge_label.setStyleSheet(
                f"background-color: {ACCENT_PRIMARY}; color: #FFFFFF; "
                "font-size: 9.5px; font-weight: 700; padding: 2px 7px; border-radius: 5px;"
            )
            self.title_label.setStyleSheet(
                "font-size: 13.5px; font-weight: 700; color: #FFFFFF; background: transparent;"
            )
        else:
            self.badge_label.setStyleSheet(
                f"background-color: #20273B; color: {TEXT_MUTED}; "
                "font-size: 9.5px; font-weight: 600; padding: 2px 7px; border-radius: 5px;"
            )
            self.title_label.setStyleSheet(
                f"font-size: 13.5px; font-weight: 600; color: {TEXT_PRIMARY}; background: transparent;"
            )

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.mode_key)
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(1.0, 1.0, self.width() - 2.0, self.height() - 2.0)

        if self._selected:
            bg_color = QColor(ACCENT_SOFT_BG)
            border_color = QColor(ACCENT_PRIMARY)
            border_width = 1.8
        elif self._hovered:
            bg_color = QColor(BG_HOVER)
            border_color = QColor("#323D59")
            border_width = 1.2
        else:
            bg_color = QColor(BG_CARD_ELEVATED)
            border_color = QColor(BORDER_SUBTLE)
            border_width = 1.0

        painter.setBrush(bg_color)
        painter.setPen(QPen(border_color, border_width))
        painter.drawRoundedRect(rect, 11.0, 11.0)
        painter.end()


class PillOptionButton(QPushButton):
    """Selectable pill button for Resolution or Audio Format choices."""

    def __init__(self, key_value: str, label: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(label, parent)
        self.key_value = key_value
        self._active = False
        self._available_highlight = False
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setMinimumHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self._apply_style()

    def set_active(self, active: bool) -> None:
        self._active = active
        self._apply_style()

    def set_available_highlight(self, available: bool) -> None:
        self._available_highlight = available
        self._apply_style()

    def _apply_style(self) -> None:
        if self._active:
            self.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: {ACCENT_PRIMARY};
                    color: #FFFFFF;
                    border: 1.5px solid {ACCENT_PRIMARY};
                    border-radius: 9px;
                    padding: 6px 13px;
                    font-size: 12px;
                    font-weight: 700;
                }}
                """
            )
        elif self._available_highlight:
            self.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: #161D2E;
                    color: {TEXT_PRIMARY};
                    border: 1px solid #2D4063;
                    border-radius: 9px;
                    padding: 6px 13px;
                    font-size: 12px;
                    font-weight: 600;
                }}
                QPushButton:hover {{
                    background-color: #1F2840;
                    border-color: #3E5887;
                }}
                """
            )
        else:
            self.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: #141926;
                    color: {TEXT_SECONDARY};
                    border: 1px solid {BORDER_SUBTLE};
                    border-radius: 9px;
                    padding: 6px 13px;
                    font-size: 12px;
                    font-weight: 600;
                }}
                QPushButton:hover {{
                    background-color: #1C2336;
                    color: {TEXT_PRIMARY};
                }}
                """
            )


class SmoothProgressBar(QWidget):
    """Custom hardware-smooth progress bar with phase coloring (Download / GPU Encode / Done)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._percent = 0.0
        self._phase = "idle"
        self.setFixedHeight(10)

    def set_progress(self, percent: float, phase: str = "downloading") -> None:
        self._percent = max(0.0, min(100.0, percent))
        self._phase = phase
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w, h = float(self.width()), float(self.height())
        track_rect = QRectF(0, 0, w, h)

        # Background track
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#1A2030"))
        painter.drawRoundedRect(track_rect, h / 2, h / 2)

        if self._percent > 0.2:
            fill_w = max(h, (self._percent / 100.0) * w)
            fill_rect = QRectF(0, 0, fill_w, h)

            grad = QLinearGradient(0, 0, w, 0)
            if self._phase == "encoding":
                grad.setColorAt(0.0, QColor("#0EA5E9"))
                grad.setColorAt(1.0, QColor("#6366F1"))
            elif self._phase in ("finished", "verifying"):
                grad.setColorAt(0.0, QColor("#10B981"))
                grad.setColorAt(1.0, QColor("#34D399"))
            elif self._phase == "error":
                grad.setColorAt(0.0, QColor("#EF4444"))
                grad.setColorAt(1.0, QColor("#F87171"))
            else:
                grad.setColorAt(0.0, QColor("#FF244C"))
                grad.setColorAt(1.0, QColor("#FF5838"))

            painter.setBrush(grad)
            painter.drawRoundedRect(fill_rect, h / 2, h / 2)

        painter.end()


class RoundedThumbnailWidget(QWidget):
    """16:9 Rounded video thumbnail preview with duration overlay badge."""

    def __init__(self, width: int = 168, height: int = 94, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(width, height)
        self._pixmap: Optional[QPixmap] = None
        self._duration_text: str = ""

    def set_thumbnail(self, pixmap: Optional[QPixmap], duration_sec: float = 0.0) -> None:
        self._pixmap = pixmap
        if duration_sec > 0:
            total_sec = int(round(duration_sec))
            hrs, rem = divmod(total_sec, 3600)
            mins, secs = divmod(rem, 60)
            if hrs > 0:
                self._duration_text = f"{hrs}:{mins:02d}:{secs:02d}"
            else:
                self._duration_text = f"{mins}:{secs:02d}"
        else:
            self._duration_text = ""
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        rect = QRectF(0, 0, float(self.width()), float(self.height()))
        clip_path = QPainterPath()
        clip_path.addRoundedRect(rect, 9.0, 9.0)
        painter.setClipPath(clip_path)

        # Fill dark fallback background
        painter.fillRect(rect, QColor("#0D111A"))

        if self._pixmap and not self._pixmap.isNull():
            scaled = self._pixmap.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            sx = (scaled.width() - self.width()) // 2
            sy = (scaled.height() - self.height()) // 2
            painter.drawPixmap(0, 0, scaled, sx, sy, self.width(), self.height())
        else:
            # Subtle placeholder icon
            painter.setPen(QPen(QColor("#2D3753"), 1.5))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 9.0, 9.0)

        # Draw duration badge in bottom-right corner
        if self._duration_text:
            font = QFont("Segoe UI", 9, QFont.Weight.Bold)
            painter.setFont(font)
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(self._duration_text) + 12
            th = 20
            badge_rect = QRectF(self.width() - tw - 6, self.height() - th - 6, tw, th)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(8, 10, 15, 215))
            painter.drawRoundedRect(badge_rect, 5.0, 5.0)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, self._duration_text)

        painter.end()


class HistoryItemRow(QFrame):
    """Completed download card row with Play & Show in Explorer actions."""

    def __init__(self, result: DownloadResult, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.result = result
        self.setObjectName("HistoryRow")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # Left status check icon
        check_lbl = QLabel()
        check_lbl.setPixmap(create_action_icon("check", SUCCESS_COLOR, 18).pixmap(18, 18))
        check_lbl.setFixedSize(20, 20)
        check_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(check_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        # Info column
        info_col = QVBoxLayout()
        info_col.setContentsMargins(0, 0, 0, 0)
        info_col.setSpacing(2)

        title_lbl = QLabel(os.path.basename(result.output_path))
        title_lbl.setWordWrap(True)
        title_lbl.setMinimumWidth(0)
        title_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title_lbl.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {TEXT_PRIMARY};")
        info_col.addWidget(title_lbl)

        size_mb = result.file_size_bytes / (1024 * 1024)
        meta_str = (
            f"{result.resolution_label} • {result.codec_summary} • "
            f"{size_mb:.1f} MB • Verified"
        )
        meta_lbl = QLabel(meta_str)
        meta_lbl.setWordWrap(True)
        meta_lbl.setMinimumWidth(0)
        meta_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        meta_lbl.setStyleSheet(f"font-size: 11.5px; color: {TEXT_SECONDARY};")
        info_col.addWidget(meta_lbl)

        layout.addLayout(info_col, 1)

        # Play button
        play_btn = QPushButton("Play")
        play_btn.setObjectName("SecondaryBtn")
        play_btn.setIcon(create_action_icon("play", TEXT_PRIMARY, 14))
        play_btn.setFixedHeight(32)
        play_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        play_btn.clicked.connect(self._open_file)
        layout.addWidget(play_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        # Show in Folder button
        folder_btn = QPushButton("Show in Folder")
        folder_btn.setObjectName("SecondaryBtn")
        folder_btn.setIcon(create_action_icon("folder", TEXT_SECONDARY, 14))
        folder_btn.setFixedHeight(32)
        folder_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        folder_btn.clicked.connect(self._reveal_in_explorer)
        layout.addWidget(folder_btn, 0, Qt.AlignmentFlag.AlignVCenter)

    def _open_file(self) -> None:
        if os.path.isfile(self.result.output_path):
            try:
                os.startfile(self.result.output_path)  # type: ignore[attr-defined]
            except Exception:
                pass

    def _reveal_in_explorer(self) -> None:
        path = os.path.normpath(self.result.output_path)
        if os.path.exists(path):
            subprocess.Popen(["explorer.exe", f"/select,{path}"])
        else:
            parent_dir = os.path.dirname(path)
            if os.path.isdir(parent_dir):
                os.startfile(parent_dir)  # type: ignore[attr-defined]
