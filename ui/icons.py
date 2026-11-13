"""Custom High-DPI Vector Icons painted with QPainter for wlz-yt."""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)


def create_app_logo_pixmap(size: int = 44) -> QPixmap:
    """Render the wlz-yt vector app badge."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0.0, QColor("#FF244C"))
    grad.setColorAt(1.0, QColor("#FF5838"))

    rect = QRectF(2, 2, size - 4, size - 4)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(grad)
    painter.drawRoundedRect(rect, size * 0.27, size * 0.27)

    # Subtle inner highlight border
    painter.setPen(QPen(QColor(255, 255, 255, 45), 1.2))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect.adjusted(0.6, 0.6, -0.6, -0.6), size * 0.26, size * 0.26)

    # Play + Download arrow hybrid symbol in white
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#FFFFFF"))

    cx, cy = size * 0.5, size * 0.5
    play_poly = QPolygonF(
        [
            QPointF(cx - size * 0.16, cy - size * 0.20),
            QPointF(cx + size * 0.21, cy),
            QPointF(cx - size * 0.16, cy + size * 0.20),
        ]
    )
    painter.drawPolygon(play_poly)

    painter.end()
    return pix


def create_app_icon() -> QIcon:
    """Return a multi-resolution QIcon for the Windows taskbar & window header."""
    icon = QIcon()
    for sz in (16, 24, 32, 48, 64, 128):
        icon.addPixmap(create_app_logo_pixmap(sz))
    return icon


def create_mode_icon(mode: str, color_hex: str, size: int = 24) -> QPixmap:
    """Draw a vector icon for 'both' (Video+Audio), 'audio' (Audio Only), or 'video' (Video Only)."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    color = QColor(color_hex)
    pen = QPen(color, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if mode == "both":
        # Clapper / Video screen + sound wave arc
        screen = QRectF(2.5, 4.5, 14.0, 11.5)
        painter.drawRoundedRect(screen, 2.5, 2.5)
        # Play triangle inside
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        tri = QPolygonF([QPointF(8.0, 7.8), QPointF(12.2, 10.25), QPointF(8.0, 12.7)])
        painter.drawPolygon(tri)
        # Audio wave arcs on the right
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(pen)
        painter.drawLine(QPointF(19.5, 7.5), QPointF(19.5, 13.0))
        painter.drawLine(QPointF(22.0, 5.5), QPointF(22.0, 15.0))
        # Bottom timeline bar
        painter.drawLine(QPointF(4.0, 19.5), QPointF(15.0, 19.5))

    elif mode == "audio":
        # Studio waveform bars
        bars = [
            (4.0, 9.5, 14.5),
            (8.0, 6.0, 18.0),
            (12.0, 3.5, 20.5),
            (16.0, 7.0, 17.0),
            (20.0, 9.0, 15.0),
        ]
        pen_wave = QPen(color, 2.3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen_wave)
        for x, y1, y2 in bars:
            painter.drawLine(QPointF(x, y1), QPointF(x, y2))

    else:  # "video"
        # Clean cinema frame with film perforations
        frame = QRectF(3.0, 4.5, 18.0, 15.0)
        painter.drawRoundedRect(frame, 2.8, 2.8)
        painter.drawLine(QPointF(7.0, 4.5), QPointF(7.0, 19.5))
        painter.drawLine(QPointF(17.0, 4.5), QPointF(17.0, 19.5))
        painter.drawLine(QPointF(3.0, 9.5), QPointF(7.0, 9.5))
        painter.drawLine(QPointF(3.0, 14.5), QPointF(7.0, 14.5))
        painter.drawLine(QPointF(17.0, 9.5), QPointF(21.0, 9.5))
        painter.drawLine(QPointF(17.0, 14.5), QPointF(21.0, 14.5))

    painter.end()
    return pix


def create_action_icon(kind: str, color_hex: str = "#FFFFFF", size: int = 18) -> QIcon:
    """Create crisp vector icons for buttons: 'paste', 'download', 'folder', 'play', 'check'."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    scale = size / 18.0
    painter.scale(scale, scale)

    color = QColor(color_hex)
    pen = QPen(color, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)

    if kind == "paste":
        painter.drawRoundedRect(QRectF(4.0, 5.0, 10.0, 11.0), 2.0, 2.0)
        painter.drawRoundedRect(QRectF(6.5, 2.5, 5.0, 2.5), 1.0, 1.0)
    elif kind == "download":
        painter.drawLine(QPointF(9.0, 3.0), QPointF(9.0, 11.8))
        painter.drawLine(QPointF(5.5, 8.8), QPointF(9.0, 12.3))
        painter.drawLine(QPointF(12.5, 8.8), QPointF(9.0, 12.3))
        painter.drawLine(QPointF(4.0, 15.0), QPointF(14.0, 15.0))
    elif kind == "folder":
        path = QPainterPath()
        path.moveTo(2.5, 5.0)
        path.lineTo(7.0, 5.0)
        path.lineTo(8.8, 6.8)
        path.lineTo(15.5, 6.8)
        path.lineTo(15.5, 14.2)
        path.lineTo(2.5, 14.2)
        path.closeSubpath()
        painter.drawPath(path)
    elif kind == "play":
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        poly = QPolygonF([QPointF(5.2, 3.6), QPointF(14.4, 9.0), QPointF(5.2, 14.4)])
        painter.drawPolygon(poly)
    elif kind == "check":
        painter.drawLine(QPointF(3.8, 9.5), QPointF(7.5, 13.0))
        painter.drawLine(QPointF(7.5, 13.0), QPointF(14.5, 5.0))

    painter.end()
    return QIcon(pix)
