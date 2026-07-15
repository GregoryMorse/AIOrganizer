# Experimental Outlook companion

This is a thin Office.js task pane for read-mode Outlook items. It requests `ReadItem`, reads no
message body, downloads no attachment, and has no mailbox-write or AIOrganizer commit path. It
exports a versioned metadata JSON file that the desktop app validates as untrusted data.

For local development only:

```powershell
cd outlook-addin
npm install
npm run validate
npm start
```

Then sideload `manifest.xml` using Outlook's custom-add-in **Add from File** workflow. The development
server uses the Office development certificate and binds only to `127.0.0.1:3000`. Production use
requires reviewed HTTPS hosting, production icon assets, manifest validation, privacy documentation,
and Microsoft 365 administrator/store deployment review.
