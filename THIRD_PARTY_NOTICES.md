# Third-party notices

Release automation must generate the complete dependency inventory and include
license texts in each bundle. Important direct components include:

- Qt for Python / PySide6 — LGPL-3.0-only, GPL, or commercial terms depending on use.
- Qt PDF — LGPL-3.0-only, GPL-2.0-only, or commercial terms.
- Tesseract OCR and tessdata_fast — Apache-2.0.
- Model Context Protocol Python SDK — MIT.
- Microsoft Authentication Library (MSAL) for Python — MIT.
- Requests and urllib3 — Apache-2.0 and MIT respectively.
- keyring — MIT.
- The Office JavaScript API is loaded from Microsoft's hosted CDN by the optional Outlook add-in
  and is not bundled into the Python desktop artifact. Outlook manifest/development tooling is
  development-only and remains recorded in `outlook-addin/package-lock.json`.

AIOrganizer dynamically loads Qt libraries and does not modify Qt. Distributors
remain responsible for satisfying the applicable LGPL relinking and notice
requirements.
