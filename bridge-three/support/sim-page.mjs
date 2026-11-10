// A simulated page for end-to-end tests: the real relay and the real page connector, with a clock
// driven by a timer, a small scene of object doubles, and a frame grabber that yields a fixed tiny
// JPEG, in place of a browser and a renderer. No GPU, no browser.
//
// Usage: node support/sim-page.mjs --project <dir>
// Prints {"ready": true} once the lock is written, and runs until stdin closes or it is killed.

import { existsSync } from "node:fs";
import { pathToFileURL } from "node:url";

import { VirtualClock } from "../src/clock.js";
import { connectPage } from "../src/connect.js";
import { ErrorMonitor } from "../src/errors.js";
import { lockPath, startRelay } from "../relay/relay.mjs";
import { cameraFacing, objectAt, sceneOf } from "./doubles.mjs";

// SOI, a baseline SOF0 for a 32x16 single-component image, EOI: enough for the frame readers.
const TINY_JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xc0, 0x00, 0x0b, 0x08, 0x00, 0x10, 0x00, 0x20, 0x01, 0x01, 0x11, 0x00,
  0xff, 0xd9]).toString("base64");

export function fixedGrabber() {
  return { copy: () => ({ visible: true }), encode: async () => TINY_JPEG };
}

export async function startSimPage({ project, forward = [1, 0, 0], frameMs = 16, capture = true }) {
  const relay = await startRelay({ project });
  const clock = new VirtualClock();
  const scene = sceneOf([objectAt({ name: "mover", position: [1, 0.5, 2], children: [objectAt({ name: "part" })] })]);
  const attached = { sceneId: "sim", scene, camera: cameraFacing(forward) };
  const loop = () => clock.requestAnimationFrame(loop);
  clock.requestAnimationFrame(loop);
  const driver = setInterval(() => clock.tick(performance.now()), frameMs);

  const grabber = capture ? fixedGrabber() : null;
  const page = await connectPage({
    url: relay.pageUrl, clock, view: () => attached, engineVersion: "sim", grabber: () => grabber,
    errors: new ErrorMonitor(),
  });
  return {
    clock,
    relay,
    async close() {
      clearInterval(driver);
      page.close();
      await relay.close();
    },
  };
}

async function waitForFile(path, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (!existsSync(path)) {
    if (Date.now() > deadline) throw new Error(`no lock at ${path}`);
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  const argv = process.argv.slice(2);
  const at = argv.indexOf("--project");
  const project = at >= 0 ? argv[at + 1] : undefined;
  if (!project) throw new Error("--project <dir> is required");
  const sim = await startSimPage({ project });
  await waitForFile(lockPath(project), 5000);
  console.log(JSON.stringify({ ready: true }));
  const shutdown = async () => {
    await sim.close();
    process.exit(0);
  };
  process.stdin.on("end", shutdown);
  process.stdin.resume();
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
}
