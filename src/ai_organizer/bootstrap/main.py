from __future__ import annotations

import sys

from .environment import load_development_env
from .mcp_registration import ensure_codex_mcp_registration


def main() -> int:
    load_development_env()
    if "--mcp" in sys.argv:
        sys.argv.remove("--mcp")
        from ai_organizer.mcp_server.server import main as mcp_main

        return mcp_main()
    smoke_test = "--smoke-test" in sys.argv
    if smoke_test:
        sys.argv.remove("--smoke-test")
    else:
        ensure_codex_mcp_registration()
    from PySide6.QtCore import QCoreApplication, QSettings, QTimer
    from PySide6.QtWidgets import QApplication

    from ai_organizer.desktop.branding import application_icon, application_version
    from ai_organizer.desktop.main_window import MainWindow

    QCoreApplication.setApplicationName("AIOrganizer")
    QCoreApplication.setApplicationVersion(application_version())
    QCoreApplication.setOrganizationName("AIOrganizer")
    application = QApplication.instance()
    if application is None:
        application = QApplication(sys.argv)
    application.setWindowIcon(application_icon())
    application.setProperty("aiorganizerSmokeTest", smoke_test)
    from ai_organizer.desktop.preferences import apply_runtime_preferences

    apply_runtime_preferences(application, QSettings("AIOrganizer", "AIOrganizer"))
    window = MainWindow()
    window.show()
    if smoke_test:
        QTimer.singleShot(250, application.quit)
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
