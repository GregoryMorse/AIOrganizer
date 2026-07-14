from __future__ import annotations

import sys


def main() -> int:
    if "--mcp" in sys.argv:
        sys.argv.remove("--mcp")
        from ai_organizer.mcp_server.server import main as mcp_main

        return mcp_main()
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    from ai_organizer.desktop.main_window import MainWindow

    QCoreApplication.setApplicationName("AIOrganizer")
    QCoreApplication.setOrganizationName("AIOrganizer")
    application = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
