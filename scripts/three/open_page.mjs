// Open a three.js game in headless Chromium and keep it open until stdin closes:
//   node open_page.mjs <game dir> <url>
// Playwright is resolved from the game's own node_modules. Page console errors are echoed as JSON lines.
import { createRequire } from "node:module";
import path from "node:path";

const [gameDir, url] = process.argv.slice(2);
const require = createRequire(path.join(path.resolve(gameDir), "package.json"));
const { chromium } = require("playwright");

// Software rendering (SwiftShader) keeps the page off the shared GPU, so a smoke test needs no GPU lease.
// Set PLAYTEST_PAGE_GPU=1 to render on the GPU instead (then hold the lease).
const gpu = process.env.PLAYTEST_PAGE_GPU === "1";
const args = gpu ? ["--ignore-gpu-blocklist"] : ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--disable-gpu"];
// PLAYTEST_PAGE_HEADED=1 opens a visible window, which is certain to render on the GPU.
const browser = await chromium.launch({ headless: process.env.PLAYTEST_PAGE_HEADED !== "1", args });
const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
// Count real animation frames, on the browser's own requestAnimationFrame (the bridge later virtualises the global).
await page.addInitScript(() => {
  const raf = globalThis.requestAnimationFrame.bind(globalThis);
  let frames = 0;
  const tick = () => { frames += 1; raf(tick); };
  raf(tick);
  Object.defineProperty(globalThis, "__playtestRealFrames", { value: () => frames });
});
page.on("console", (m) => { if (m.type() === "error") console.log(JSON.stringify({ console_error: m.text().slice(0, 300) })); });
page.on("pageerror", (e) => console.log(JSON.stringify({ page_error: String(e).slice(0, 300) })));
await page.goto(url, { waitUntil: "load", timeout: 120000 });
console.log(JSON.stringify({ opened: url.split("?")[0] }));

// Every 5 s: real frames per second, and the bridge's capture timings since the last report (see capture.js).
// A page whose main thread is stuck cannot answer, which is reported as unresponsive rather than waited on.
const HEALTH_MS = 5000;
let lastFrames = null;
let lastAt = Date.now();
const health = setInterval(async () => {
  const sample = page.evaluate(() => {
    const steps = {};
    for (const entry of performance.getEntriesByType("measure")) {
      if (!entry.name.startsWith("playtest-capture-")) continue;
      const step = (steps[entry.name.slice("playtest-capture-".length)] ??= { n: 0, total_ms: 0, max_ms: 0 });
      step.n += 1;
      step.total_ms += entry.duration;
      step.max_ms = Math.max(step.max_ms, entry.duration);
    }
    for (const name of ["copy", "encode", "write"]) performance.clearMeasures(`playtest-capture-${name}`);
    return { frames: globalThis.__playtestRealFrames?.() ?? null, steps };
  });
  const answer = await Promise.race([sample, new Promise((resolve) => setTimeout(() => resolve(null), HEALTH_MS - 500))])
    .catch(() => null);
  const at = Date.now();
  if (answer === null) {
    console.log(JSON.stringify({ page_health: { unresponsive: true } }));
    return;
  }
  const fps = lastFrames === null || answer.frames === null ? null : (answer.frames - lastFrames) / ((at - lastAt) / 1000);
  lastFrames = answer.frames;
  lastAt = at;
  const capture = Object.fromEntries(Object.entries(answer.steps).map(([name, s]) =>
    [name, { n: s.n, mean_ms: +(s.total_ms / s.n).toFixed(1), max_ms: +s.max_ms.toFixed(1) }]));
  console.log(JSON.stringify({ page_health: { real_fps: fps === null ? null : +fps.toFixed(1), capture } }));
}, HEALTH_MS);
process.stdin.resume();
process.stdin.on("end", async () => { clearInterval(health); await browser.close(); process.exit(0); });
