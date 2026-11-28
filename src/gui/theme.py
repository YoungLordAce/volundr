"""Dark Qt stylesheet and programmatic vector icons for the Völundr desktop UI."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

_CHEVRON_DOWN_URL = (Path(__file__).resolve().parent / "chevron_down.svg").as_posix()
_CHECK_MARK_URL = (Path(__file__).resolve().parent / "check.svg").as_posix()

DARK_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #14171f;
    color: #e6ebf5;
    font-family: "Segoe UI", "Inter", "Noto Sans", sans-serif;
    font-size: 13px;
}

QLabel, QCheckBox {
    background-color: transparent;
}

QFrame#HeaderBar {
    background-color: #1b202b;
    border-bottom: 1px solid #2b3242;
}

QLabel#AppTitle {
    background-color: transparent;
    font-size: 18px;
    font-weight: 700;
    color: #f4f7ff;
}

QLabel#AppVersion {
    background-color: transparent;
    font-size: 12px;
    color: #8b95ad;
}

QLabel#StatusBadge {
    padding: 4px 10px;
    border-radius: 10px;
    font-weight: 600;
    font-size: 12px;
}

QLabel#InfoStrip {
    background-color: #1a1f2a;
    border: 1px solid #2a3140;
    border-radius: 6px;
    padding: 8px 12px;
    color: #b7c2d9;
}

QPushButton {
    background-color: #262e3f;
    color: #e6ebf5;
    border: 1px solid #384259;
    border-radius: 6px;
    padding: 6px 14px;
    font-weight: 600;
}

QPushButton:hover {
    background-color: #323c52;
    border-color: #4f5d7a;
}

QPushButton:pressed {
    background-color: #1e2433;
}

QPushButton:disabled {
    background-color: #1c212c;
    color: #677188;
    border-color: #272d3b;
}

QPushButton#PrimaryBtn {
    background-color: #3b82f6;
    border-color: #2563eb;
    color: #ffffff;
}

QPushButton#PrimaryBtn:hover {
    background-color: #2563eb;
}

QPushButton#DangerBtn {
    background-color: #b91c1c;
    border-color: #991b1b;
    color: #ffffff;
}

QPushButton#DangerBtn:hover {
    background-color: #991b1b;
}

QPushButton#PrimaryBtn:disabled,
QPushButton#DangerBtn:disabled {
    background-color: #1c212c;
    color: #677188;
    border-color: #272d3b;
}

QPushButton#CompactBtn {
    padding: 4px 12px;
    font-size: 12px;
}

QSpinBox, QDoubleSpinBox {
    background-color: #10141c;
    color: #e6ebf5;
    border: 1px solid #343e54;
    border-radius: 6px;
    padding: 5px 8px;
}

QSpinBox:focus, QDoubleSpinBox:focus {
    border-color: #3b82f6;
}

QTabWidget::pane {
    border: 1px solid #2a3140;
    border-radius: 6px;
    background-color: #171b24;
    top: -1px;
}

QTabBar::tab {
    background-color: #1b202b;
    color: #9aa5be;
    border: 1px solid #2a3140;
    border-bottom: none;
    padding: 8px 18px;
    margin-right: 4px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    font-weight: 600;
}

QTabBar::tab:selected {
    background-color: #171b24;
    color: #f4f7ff;
    border-color: #3b82f6;
}

QTabBar::tab:hover:!selected {
    background-color: #232938;
    color: #e6ebf5;
}

QTableWidget {
    background-color: #12161e;
    alternate-background-color: #171c26;
    gridline-color: #252c3b;
    border: 1px solid #2a3140;
    border-radius: 6px;
    selection-background-color: #284270;
    selection-color: #ffffff;
}

QHeaderView::section {
    background-color: #1d2330;
    color: #b7c2d9;
    padding: 6px 8px;
    border: none;
    border-right: 1px solid #2a3140;
    border-bottom: 1px solid #2a3140;
    font-weight: 600;
}

QPlainTextEdit {
    background-color: #0f131a;
    color: #d6deee;
    border: 1px solid #2a3140;
    border-radius: 6px;
    font-family: "Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace;
    font-size: 12px;
    padding: 6px;
}

QTextBrowser {
    background-color: #0f131a;
    color: #dbe3f2;
    border: 1px solid #2a3140;
    border-radius: 6px;
    font-family: "Segoe UI", "Inter", "Noto Sans", sans-serif;
    font-size: 13px;
    padding: 10px 12px;
}

QScrollBar:vertical {
    background: #10141c;
    width: 10px;
    margin: 2px 1px 2px 1px;
    border-radius: 5px;
}

QScrollBar::handle:vertical {
    background: #3d4861;
    min-height: 28px;
    border-radius: 5px;
}

QScrollBar::handle:vertical:hover {
    background: #57678a;
}

QScrollBar::handle:vertical:pressed {
    background: #3b82f6;
}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {
    background: transparent;
    height: 0px;
}

QScrollBar:horizontal {
    background: #10141c;
    height: 10px;
    margin: 1px 2px 1px 2px;
    border-radius: 5px;
}

QScrollBar::handle:horizontal {
    background: #3d4861;
    min-width: 28px;
    border-radius: 5px;
}

QScrollBar::handle:horizontal:hover {
    background: #57678a;
}

QScrollBar::handle:horizontal:pressed {
    background: #3b82f6;
}

QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {
    background: transparent;
    width: 0px;
}

QAbstractScrollArea::corner {
    background: #10141c;
}

QLineEdit, QComboBox {
    background-color: #10141c;
    color: #e6ebf5;
    border: 1px solid #343e54;
    border-radius: 6px;
    padding: 6px 10px;
}

QComboBox {
    padding-right: 30px;
}

QLineEdit:focus, QComboBox:focus, QComboBox:on {
    border-color: #3b82f6;
}

QComboBox::drop-down {
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 28px;
    border: none;
    background: transparent;
    border-top-right-radius: 6px;
    border-bottom-right-radius: 6px;
}

QComboBox::down-arrow {
    image: url("__CHEVRON_DOWN_URL__");
    width: 12px;
    height: 12px;
}

QComboBox QAbstractItemView {
    background-color: #1b202b;
    color: #e6ebf5;
    selection-background-color: #2563eb;
    border: 1px solid #343e54;
    border-radius: 6px;
    padding: 2px;
    outline: 0;
}

QCheckBox {
    spacing: 8px;
    color: #d6deee;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid #47536e;
    background-color: #10141c;
}

QCheckBox::indicator:hover {
    border-color: #60a5fa;
}

QCheckBox::indicator:checked {
    background-color: #3b82f6;
    border-color: #3b82f6;
    image: url("__CHECK_MARK_URL__");
}

QCheckBox::indicator:checked:hover {
    background-color: #2563eb;
    border-color: #2563eb;
}

QGroupBox {
    border: 1px solid #2a3140;
    border-radius: 6px;
    margin-top: 12px;
    padding: 12px;
    font-weight: 600;
    color: #cbd5eb;
}

QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
}

QMenu {
    background-color: #1b202b;
    color: #e6ebf5;
    border: 1px solid #343e54;
    padding: 4px;
}

QMenu::item {
    padding: 6px 22px;
    border-radius: 4px;
}

QMenu::item:selected {
    background-color: #2563eb;
    color: #ffffff;
}

QMenu::item:disabled {
    color: #5b6478;
    background-color: transparent;
}

QMenu::item:disabled:selected {
    color: #5b6478;
    background-color: transparent;
}

QMenu::separator {
    height: 1px;
    background: #2e3648;
    margin: 4px 8px;
}

QProgressBar {
    background-color: #10141c;
    border: 1px solid #2a3140;
    border-radius: 5px;
    height: 10px;
    text-align: center;
    color: transparent;
}

QProgressBar::chunk {
    background-color: #3b82f6;
    border-radius: 4px;
}

QScrollArea {
    background-color: transparent;
    border: 1px solid #2a3140;
    border-radius: 6px;
}
""".replace("__CHEVRON_DOWN_URL__", _CHEVRON_DOWN_URL).replace("__CHECK_MARK_URL__", _CHECK_MARK_URL)


def _paint_app_icon_pixmap(*, watching: bool | None, size: int) -> QPixmap:
    """Paint a single square pixmap of the Völundr shield icon at `size x size`."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # Rounded dark indigo-slate badge
    inset = max(1.0, size * 0.04)
    rect = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0.0, QColor("#2a3b66"))
    grad.setColorAt(1.0, QColor("#151c2e"))
    painter.setBrush(grad)
    painter.setPen(QPen(QColor("#5b8def"), max(1.2, size * 0.045)))
    radius = size * 0.22
    painter.drawRoundedRect(rect, radius, radius)

    # Bold "V" forge glyph + two umlaut sparks above
    spark_r = max(1.1, size * 0.058)
    painter.setBrush(QColor("#38bdf8"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(QPointF(size * 0.39, size * 0.22), spark_r, spark_r)
    painter.drawEllipse(QPointF(size * 0.61, size * 0.22), spark_r, spark_r)

    path = QPainterPath()
    path.moveTo(size * 0.25, size * 0.35)
    path.lineTo(size * 0.50, size * 0.78)
    path.lineTo(size * 0.75, size * 0.35)
    pen = QPen(QColor("#f8fafc"), max(1.8, size * 0.115))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)

    # Optional live status dot in bottom-right corner (used for system tray icon)
    if watching is not None:
        dot_r = max(2.2, size * 0.14)
        dot_color = QColor("#22c55e") if watching else QColor("#f59e0b")
        painter.setBrush(dot_color)
        painter.setPen(QPen(QColor("#14171f"), max(1.0, size * 0.04)))
        painter.drawEllipse(QPointF(size * 0.80, size * 0.80), dot_r, dot_r)

    painter.end()
    return pixmap


def build_app_icon(
    *,
    watching: bool | None = None,
    sizes: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256),
) -> QIcon:
    """
    Programmatically paint a multi-resolution Völundr shield/forge icon.
    - `watching=None`: clean brand icon for the Windows taskbar, Alt-Tab, and window title bars.
    - `watching=True/False`: includes a live green/amber status dot for the system tray icon.
    """
    icon = QIcon()
    for sz in sizes:
        icon.addPixmap(_paint_app_icon_pixmap(watching=watching, size=sz))
    return icon


class _GlowingDot(QWidget):
    """Soft glowing status LED painted with a radial halo + crisp core."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._core_color = QColor("#f59e0b")
        self._glow_color = QColor("#f59e0b")
        self.setStyleSheet("background: transparent;")
        self.setFixedSize(QSize(18, 18))

    def set_color(self, hex_color: str) -> None:
        self._core_color = QColor(hex_color)
        self._glow_color = QColor(hex_color)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        cx = self.width() / 2.0
        cy = self.height() / 2.0

        # Soft outer radial glow
        glow = QRadialGradient(QPointF(cx, cy), 8.5)
        c_mid = QColor(self._glow_color)
        c_mid.setAlpha(125)
        c_edge = QColor(self._glow_color)
        c_edge.setAlpha(0)
        glow.setColorAt(0.0, c_mid)
        glow.setColorAt(0.55, QColor(self._glow_color.red(), self._glow_color.green(), self._glow_color.blue(), 45))
        glow.setColorAt(1.0, c_edge)
        painter.setBrush(glow)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(cx, cy), 8.5, 8.5)

        # Crisp luminous center dot
        painter.setBrush(self._core_color)
        painter.drawEllipse(QPointF(cx, cy), 3.8, 3.8)
        painter.end()


class StatusIndicator(QWidget):
    """Minimal borderless header status indicator: soft glowing dot + aligned text."""

    _PALETTE = {
        "watching": ("#22c55e", "#86efac", "Auto-Forging"),
        "scanning": ("#38bdf8", "#7dd3fc", "Forging..."),
        "paused": ("#f59e0b", "#fcd34d", "Paused"),
        "error": ("#ef4444", "#fca5a5", "Error"),
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background-color: transparent;")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(6)

        self._dot = _GlowingDot(self)
        self._label = QLabel("Paused", self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.set_state("paused")

    def set_state(self, state: str, text_override: str | None = None) -> None:
        dot_hex, text_hex, default_text = self._PALETTE.get(state, self._PALETTE["paused"])
        self._dot.set_color(dot_hex)
        self._label.setText(text_override or default_text)
        self._label.setStyleSheet(
            f"background-color: transparent; color: {text_hex}; "
            "font-weight: 600; font-size: 13px;"
        )

    def text(self) -> str:
        return self._label.text()
