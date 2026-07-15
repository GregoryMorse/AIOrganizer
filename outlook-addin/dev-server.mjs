import { createReadStream, existsSync } from "node:fs";
import { createServer } from "node:https";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";
import devCerts from "office-addin-dev-certs";

const root = fileURLToPath(new URL(".", import.meta.url));
const options = await devCerts.getHttpsServerOptions();
const mime = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
};

createServer(options, (request, response) => {
  const requestPath = new URL(request.url ?? "/", "https://localhost").pathname;
  const relative = requestPath === "/" ? "taskpane.html" : requestPath.slice(1);
  const target = normalize(join(root, relative));
  if (!target.startsWith(root) || !existsSync(target)) {
    response.writeHead(404).end("Not found");
    return;
  }
  response.writeHead(200, {
    "Content-Type": mime[extname(target)] ?? "application/octet-stream",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'self' https://appsforoffice.microsoft.com; script-src 'self' https://appsforoffice.microsoft.com; style-src 'self'; connect-src 'none'; frame-ancestors https://*.office.com https://*.office365.com https://outlook.office.com;",
  });
  createReadStream(target).pipe(response);
}).listen(3000, "127.0.0.1", () => {
  process.stdout.write("AIOrganizer Outlook companion: https://localhost:3000/taskpane.html\n");
});
