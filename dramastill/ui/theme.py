from PySide6.QtGui import QFontDatabase

from dramastill.paths import font_path


STYLESHEET = """
* { font-size: 13px; }
QMainWindow, QWidget { background: #12141c; color: #ece7dc; }
QFrame#sidebar { background: #0e1016; }
QLabel#logo { font-size: 20px; font-weight: 700; color: #f0d48a; }
QLabel#subtitle, QLabel#hint { color: #a39c90; }
QLabel#section { font-size: 18px; font-weight: 600; }
QPushButton#nav {
    text-align: left;
    padding: 10px 14px;
    border: none;
    border-radius: 8px;
    background: transparent;
    color: #d9d3c7;
}
QPushButton#nav:checked { background: #2a2418; color: #f0d48a; }
QPushButton#nav:hover { background: #1c1f2a; }
QPushButton {
    background: #2a3142;
    color: #f4f1ea;
    border: 1px solid #3a4256;
    border-radius: 8px;
    padding: 8px 14px;
}
QPushButton:hover { background: #343c52; }
QPushButton#primary { background: #b8892d; color: #1a1408; border: none; font-weight: 700; padding: 12px 18px; }
QPushButton#primary:hover { background: #d4a84a; }
QPushButton#danger { background: #6e2d32; border: none; }
QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: #1b1f2b;
    color: #f4f1ea;
    border: 1px solid #343b4d;
    border-radius: 8px;
    padding: 6px 8px;
    selection-background-color: #8d6a22;
}
QComboBox QAbstractItemView { background: #1b1f2b; color: #f4f1ea; selection-background-color: #8d6a22; }
QPlainTextEdit { font-family: "Cascadia Mono", "DejaVu Sans Mono", monospace; }
QGroupBox {
    border: 1px solid #2c3344;
    border-radius: 12px;
    margin-top: 14px;
    padding: 12px;
    background: #171b26;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #f0d48a; }
QProgressBar { background: #1b1f2b; border: none; border-radius: 6px; height: 14px; text-align: center; color: #1a1408; }
QProgressBar::chunk { background: #d4a84a; border-radius: 6px; }
QTableWidget { background: #171b26; gridline-color: #2c3344; border: none; }
QHeaderView::section { background: #12141c; color: #a39c90; border: none; padding: 6px; }
QTableWidget::item:selected { background: #3a3120; }
QScrollBar:vertical { background: #12141c; width: 12px; }
QScrollBar::handle:vertical { background: #3a4256; border-radius: 6px; }
QCheckBox { spacing: 8px; }
QStatusBar { background: #0e1016; color: #a39c90; }
QLabel#balance { color: #f0d48a; padding: 0 12px; font-weight: 600; }
"""


def load_font() -> str:
    path = font_path()
    if path is None:
        return "Sans Serif"
    family = QFontDatabase.addApplicationFont(str(path))
    families = QFontDatabase.applicationFontFamilies(family)
    return families[0] if families else "Sans Serif"
