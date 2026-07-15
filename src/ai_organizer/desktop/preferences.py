from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QLocale, QSettings, QTranslator
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

_TRANSLATORS: list[QTranslator] = []


def apply_runtime_preferences(application: QApplication, settings: QSettings) -> None:
    locale_name = str(settings.value("accessibility/locale", "system"))
    locale = QLocale.system() if locale_name == "system" else QLocale(locale_name)
    QLocale.setDefault(locale)
    for translator in _TRANSLATORS:
        application.removeTranslator(translator)
    _TRANSLATORS.clear()
    if not locale.name().startswith("en"):
        translator = QTranslator(application)
        catalog = Path(__file__).parents[1] / "resources" / "i18n"
        if translator.load(f"aiorganizer_{locale.name()}", str(catalog)):
            application.installTranslator(translator)
            _TRANSLATORS.append(translator)

    font = application.font()
    base_size = application.property("aiorganizerBaseFontSize")
    if not isinstance(base_size, (int, float)) or base_size <= 0:
        base_size = font.pointSizeF() if font.pointSizeF() > 0 else 9.0
        application.setProperty("aiorganizerBaseFontSize", base_size)
    try:
        requested_scale = int(settings.value("accessibility/textScale", 100))
    except (TypeError, ValueError):
        requested_scale = 100
    scale = max(90, min(180, requested_scale))
    font.setPointSizeF(float(base_size) * scale / 100)
    application.setFont(font)

    if settings.value("accessibility/highContrast", False, bool):
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#000000"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#000000"))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#202020"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.Button, QColor("#000000"))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#ffff00"))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#000000"))
        application.setPalette(palette)
    else:
        application.setPalette(application.style().standardPalette())
