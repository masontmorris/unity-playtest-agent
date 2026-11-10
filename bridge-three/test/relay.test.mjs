import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { connect } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { framesPath, lockPath, startRelay } from "../relay/relay.mjs";
import { startSimPage } from "../support/sim-page.mjs";

async function until(condition, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs;
  while (!condition()) {
    if (Date.now() > deadline) throw new Error("timed out");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

function coreConnection(port) {
  const socket = connect(port, "127.0.0.1");
  socket.setEncoding("utf8");
  const lines = [];
  let pending = "";
  let closed = false;
  socket.on("data", (chunk) => {
    pending += chunk;
    let newline;
    while ((newline = pending.indexOf("\n")) >= 0) {
      lines.push(JSON.parse(pending.slice(0, newline)));
      pending = pending.slice(newline + 1);
    }
  });
  socket.on("close", () => { closed = true; });
  let seq = 0;
  return {
    lines,
    get closed() { return closed; },
    send(type, fields) {
      seq += 1;
      socket.write(JSON.stringify({
        protocol: "upa/1", session: "core", seq, game_s: null, wall_utc: new Date().toISOString(), type, ...fields,
      }) + "\n");
    },
    async next(count) {
      await until(() => lines.length >= count);
      return lines[count - 1];
    },
    destroy: () => socket.destroy(),
  };
}

test("a core reaches the page through the lock, a second core is busy, and the lock goes with the page", async () => {
  const project = mkdtempSync(join(tmpdir(), "bridge-three-"));
  const sim = await startSimPage({ project });
  try {
    await until(() => existsSync(lockPath(project)));
    const lock = JSON.parse(readFileSync(lockPath(project), "utf8"));
    assert.equal(lock.protocol, "upa/1");
    assert.equal(lock.pid, process.pid);

    const core = coreConnection(lock.port);
    core.send("hello", { token: lock.token, core_version: "test" });
    const status = await core.next(1);
    assert.equal(status.type, "status");
    assert.equal(status.engine, "three");
    assert.equal(status.lock.port, lock.port);

    const second = coreConnection(lock.port);
    const busy = await second.next(1);
    assert.equal(busy.code, "busy");
    await until(() => second.closed);

    core.send("observe", { mode: "once", with_frame: false, rate_hz: null });
    const observation = await core.next(2);
    assert.equal(observation.scene, "sim");
    assert.equal(observation.view.yaw_deg, 90);

    core.send("stop", { reason: "finish" });
    assert.equal((await core.next(3)).type, "stopped");
    await until(() => core.closed);

    const again = coreConnection(lock.port);
    again.send("hello", { token: lock.token, core_version: "test" });
    assert.equal((await again.next(1)).type, "status", "the listener takes a new core after a stop");
    again.destroy();
  } finally {
    await sim.close();
    rmSync(project, { recursive: true, force: true });
  }
  assert.equal(existsSync(lockPath(project)), false);
});

test("the relay writes complete frames into the window and refuses anything else", async () => {
  const project = mkdtempSync(join(tmpdir(), "bridge-three-"));
  const relay = await startRelay({ project });
  const page = new WebSocket(relay.pageUrl);
  const replies = [];
  page.addEventListener("message", (event) => replies.push(JSON.parse(event.data)));
  await new Promise((resolve) => page.addEventListener("open", resolve));
  await until(() => replies.some((m) => m.relay === "endpoint"));
  const jpeg = Buffer.from([0xff, 0xd8, 0x00, 0xff, 0xd9]).toString("base64");
  page.send(JSON.stringify({ relay: "frame", name: "f-000001.jpg", jpeg_base64: jpeg }));
  page.send(JSON.stringify({ relay: "frame", name: "f-000002.jpg", jpeg_base64: Buffer.from("png?").toString("base64") }));
  page.send(JSON.stringify({ relay: "frame", name: "../escape.jpg", jpeg_base64: jpeg }));
  await until(() => replies.filter((m) => m.relay === "frame_written").length === 3);
  const [ok, notJpeg, badName] = replies.filter((m) => m.relay === "frame_written");
  assert.equal(ok.path, join(framesPath(project), "f-000001.jpg"));
  assert.equal(readFileSync(ok.path).length, 5);
  assert.match(notJpeg.error, /complete JPEG/);
  assert.match(badName.error, /f-NNNNNN/);
  page.close();
  await relay.close();
  rmSync(project, { recursive: true, force: true });
});

test("a page without the key is refused", async () => {
  const project = mkdtempSync(join(tmpdir(), "bridge-three-"));
  const sim = await startSimPage({ project });
  try {
    const url = new URL(sim.relay.pageUrl);
    url.searchParams.set("key", "wrong");
    const socket = new WebSocket(url);
    const outcome = await new Promise((resolve) => {
      socket.addEventListener("open", () => resolve("open"));
      socket.addEventListener("error", () => resolve("error"));
    });
    assert.equal(outcome, "error");
  } finally {
    await sim.close();
    rmSync(project, { recursive: true, force: true });
  }
});
