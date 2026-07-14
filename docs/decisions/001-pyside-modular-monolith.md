# ADR-001: PySide6 and modular monolith

Status: accepted

Use PySide6/Qt Widgets for the desktop application. Keep domain and application
packages free of Qt and adapter imports. The desktop, MCP server, providers,
filesystem, extraction, and persistence packages depend inward on typed ports.
