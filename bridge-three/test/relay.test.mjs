import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { connect } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { lockPath } from "../relay/relay.mjs";
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
