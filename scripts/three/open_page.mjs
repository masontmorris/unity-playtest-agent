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
const browser = await chromium.launch({ headless: true, args });
const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
page.on("console", (m) => { if (m.type() === "error") console.log(JSON.stringify({ console_error: m.text().slice(0, 300) })); });
page.on("pageerror", (e) => console.log(JSON.stringify({ page_error: String(e).slice(0, 300) })));
await page.goto(url, { waitUntil: "load", timeout: 120000 });
console.log(JSON.stringify({ opened: url.split("?")[0] }));
process.stdin.resume();
process.stdin.on("end", async () => { await browser.close(); process.exit(0); });
