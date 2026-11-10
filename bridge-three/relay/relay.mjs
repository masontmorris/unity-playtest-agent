// Loopback relay between the playtest core and a three.js page.
//
// A browser page cannot listen on TCP, so this process does it for the page. It serves one page
// over a WebSocket and, while that page is attached, one core connection over the same loopback
// JSONL transport the Unity bridge uses, advertised in `<project>/Temp/playtest-bridge.lock`. Lines
// pass through unchanged; the page's bridge session does hello, auth and every reply. The relay only
// adds what a page cannot: the listener, the lock, a fresh token and the busy refusal.
//
// Relay control messages carry a `relay` member, which no upa/1 message has:
//   relay -> page  {"relay": "endpoint", token, pid, port, project}  once the core listener is up
//   relay -> page  {"relay": "open"} / {"relay": "close"}            a core connection began or ended
//   page -> relay  {"relay": "end"}                                  close the core connection
//   page -> relay  {"relay": "frame", name, jpeg_base64}             write one captured frame
//   relay -> page  {"relay": "frame_written", name, path | error}    the frame's file, once complete
//
// Usage: node relay/relay.mjs --project <game project dir> [--ws-port N]
// It prints one JSON line with the page URL parameter, then runs until interrupted.

import { createHash, randomBytes } from "node:crypto";
import { mkdirSync, readdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { createServer as createHttpServer } from "node:http";
import { createServer as createTcpServer } from "node:net";
import { basename, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { acceptWebSocket } from "./websocket.mjs";

export const PROTOCOL = "upa/1";
const HOST = "127.0.0.1";
const MAX_LINE_BYTES = 8 * 1024 * 1024;
const FRAME_NAME = /^f-\d{6,}\.jpg$/;
// The Unity bridge's rolling window: 120 frames, 12 s at 10 Hz.
const FRAME_WINDOW = 120;

export function lockPath(project) {
  return join(project, "Temp", "playtest-bridge.lock");
}

export function framesPath(project) {
  return join(project, "Temp", "playtest-bridge-frames");
}

function clearFrames(directory) {
  rmSync(directory, { recursive: true, force: true });
  mkdirSync(directory, { recursive: true });
}

/** Write one frame atomically and trim the window. Returns its path, or throws why it was not written. */
function writeFrame(directory, name, jpegBase64) {
  if (typeof name !== "string" || !FRAME_NAME.test(name)) throw new Error("frame name must look like f-NNNNNN.jpg");
  if (typeof jpegBase64 !== "string") throw new Error("frame data is missing");
  const bytes = Buffer.from(jpegBase64, "base64");
  const complete = bytes.length > 4 && bytes[0] === 0xff && bytes[1] === 0xd8
    && bytes[bytes.length - 2] === 0xff && bytes[bytes.length - 1] === 0xd9;
  if (!complete) throw new Error("frame data is not a complete JPEG");
  const path = join(directory, name);
  const temporary = `${path}.tmp`;
  writeFileSync(temporary, bytes);
  renameSync(temporary, path);
  const frames = readdirSync(directory).filter((file) => FRAME_NAME.test(file)).sort();
  for (const old of frames.slice(0, Math.max(0, frames.length - FRAME_WINDOW))) rmSync(join(directory, old), { force: true });
  return path;
}

function writeLock(project, body) {
  const path = lockPath(project);
  mkdirSync(join(project, "Temp"), { recursive: true });
  const temporary = `${path}.${process.pid}.tmp`;
  writeFileSync(temporary, JSON.stringify(body) + "\n", { encoding: "utf8", mode: 0o600 });
  renameSync(temporary, path);
}

function busyLine() {
  return JSON.stringify({
    protocol: PROTOCOL, session: "unknown", seq: 0, game_s: null, wall_utc: new Date().toISOString(),
    type: "error", code: "busy", message: "a core session is already connected", in_reply_to: null,
  }) + "\n";
}

/**
 * Start the relay. Resolves to `{ pageUrl, wsPort, close() }`. `pageUrl` is the WebSocket URL the
 * page connects to, including the per-run page key.
 */
export async function startRelay({ project, wsPort = 0, catalogPath = null, catalog = null, log = () => {} }) {
  const projectDir = resolve(project);
  let pinnedCatalog = catalog;
  let catalogSha256 = null;
  if (catalogPath) {
    const bytes = readFileSync(resolve(catalogPath));
    pinnedCatalog = JSON.parse(bytes.toString("utf8"));
    catalogSha256 = createHash("sha256").update(bytes).digest("hex");
  } else if (catalog !== null) {
    catalogSha256 = createHash("sha256").update(JSON.stringify(catalog)).digest("hex");
  }
  const pageKey = randomBytes(18).toString("base64url");
  let page = null;
  let core = null;
  let tcp = null;

  const toPage = (message) => page?.send(typeof message === "string" ? message : JSON.stringify(message));

  const endCore = () => {
    core?.end();
    core = null;
  };

  const detachPage = () => {
    page = null;
    core?.destroy();
    core = null;
    tcp?.close();
    tcp = null;
    rmSync(lockPath(projectDir), { force: true });
    log("page detached; lock removed");
  };

  const serveCore = (socket) => {
    if (core !== null) {
      socket.end(busyLine());
      return;
    }
    core = socket;
    socket.setNoDelay(true);
    toPage({ relay: "open" });
    let pending = "";
    socket.setEncoding("utf8");
    socket.on("data", (chunk) => {
      pending += chunk;
      let newline;
      while ((newline = pending.indexOf("\n")) >= 0) {
        const line = pending.slice(0, newline);
        pending = pending.slice(newline + 1);
        if (line.length > 0) toPage(line);
      }
      if (Buffer.byteLength(pending) > MAX_LINE_BYTES) socket.destroy();
    });
    const closed = () => {
      if (core !== socket) return;
      core = null;
      toPage({ relay: "close" });
    };
    socket.on("close", closed);
    socket.on("error", closed);
  };

  const attachPage = async (connection) => {
    page = connection;
    clearFrames(framesPath(projectDir));
    tcp = createTcpServer(serveCore);
    await new Promise((done) => tcp.listen(0, HOST, done));
    const port = tcp.address().port;
    const token = randomBytes(24).toString("base64url");
    writeLock(projectDir, { protocol: PROTOCOL, pid: process.pid, port, token, started_utc: new Date().toISOString() });
    toPage({ relay: "endpoint", token, pid: process.pid, port, project: basename(projectDir),
      ...(pinnedCatalog === null ? {} : { catalog: pinnedCatalog, catalog_sha256: catalogSha256 }) });
    log(`page attached; core listener on ${HOST}:${port}`);
  };

  const http = createHttpServer((request, response) => {
    response.writeHead(426, { "content-type": "text/plain" }).end("WebSocket only\n");
  });
  http.on("upgrade", (request, socket) => {
    const url = new URL(request.url, `http://${HOST}`);
    if (url.searchParams.get("key") !== pageKey || request.headers.upgrade?.toLowerCase() !== "websocket") {
      socket.end("HTTP/1.1 403 Forbidden\r\n\r\n");
      return;
    }
    if (page !== null) {
      socket.end("HTTP/1.1 409 Conflict\r\n\r\n");
      return;
    }
    let connection = null;
    connection = acceptWebSocket(request, socket, {
      onMessage(text) {
        let body;
        try {
          body = JSON.parse(text);
        } catch {
          connection.close();
          return;
        }
        if (body !== null && typeof body === "object" && "relay" in body) {
          if (body.relay === "end") endCore();
          else if (body.relay === "frame") {
            try {
              toPage({ relay: "frame_written", name: body.name, path: writeFrame(framesPath(projectDir), body.name, body.jpeg_base64) });
            } catch (error) {
              toPage({ relay: "frame_written", name: body.name, error: error.message });
            }
          }
          return;
        }
        core?.write(text + "\n");
      },
      onClose() {
        if (page === connection) detachPage();
      },
    });
    attachPage(connection).catch((error) => {
      log(`could not attach the page: ${error.message}`);
      connection.close();
    });
  });
  await new Promise((done) => http.listen(wsPort, HOST, done));
  const boundPort = http.address().port;

  return {
    pageUrl: `ws://${HOST}:${boundPort}/?key=${pageKey}`,
    wsPort: boundPort,
    async close() {
      page?.close();
      if (page !== null) detachPage();
      await new Promise((done) => http.close(done));
    },
  };
}

function parseArguments(argv) {
  const options = {};
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--project") options.project = argv[++i];
    else if (argv[i] === "--catalog") options.catalogPath = argv[++i];
    else if (argv[i] === "--ws-port") options.wsPort = Number(argv[++i]);
    else throw new Error(`unknown argument ${argv[i]}`);
  }
  if (!options.project) throw new Error("--project <dir> is required");
  return options;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  const options = parseArguments(process.argv.slice(2));
  const relay = await startRelay({ ...options, log: (line) => console.error(`relay: ${line}`) });
  console.log(JSON.stringify({ playtest: relay.pageUrl }));
  const shutdown = async () => {
    await relay.close();
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}
