import { appendFileSync, readFileSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join } from "node:path";

const root = "/work/app/web/dist";
const unmocked = "/out/unmocked-api.txt";
const types = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".map": "application/json",
  ".webmanifest": "application/manifest+json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".txt": "text/plain; charset=utf-8",
};

function isFile(path) {
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
}

createServer((request, response) => {
  let path;
  try {
    path = decodeURIComponent(new URL(request.url, "http://127.0.0.1").pathname);
  } catch {
    response.writeHead(400).end();
    return;
  }
  if (path === "/api" || path.startsWith("/api/") || path === "/healthz") {
    const line = `${request.method} ${path}`;
    appendFileSync(unmocked, `${line}\n`);
    console.error(`Запрос к API без подмены в тесте QA: ${line}`);
    response.writeHead(501, { "content-type": "application/json" });
    response.end(
      JSON.stringify({
        code: "office_unmocked_api",
        message: `Тест QA не подменил запрос ${line}`,
        retryable: false,
        operation_id: "office_e2e",
      }),
    );
    return;
  }
  let file = join(root, path);
  if (!file.startsWith(`${root}/`) || !isFile(file)) {
    if (extname(path)) {
      response.writeHead(404).end();
      return;
    }
    file = join(root, "index.html");
  }
  response.writeHead(200, { "content-type": types[extname(file)] ?? "application/octet-stream" });
  response.end(readFileSync(file));
}).listen(4173, "127.0.0.1");
