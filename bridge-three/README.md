# Playtest bridge (three.js)

You can use this package under either of these licenses, at your option:

- Apache License, Version 2.0 (`LICENSE-APACHE`)
- MIT license (`LICENSE-MIT`)

This licence covers `bridge-three/` alone. The rest of this repository has its own licence; see the
root README.

The bridge lets the playtest core drive a three.js game. It speaks the same `upa/1` protocol as
the Unity bridge package in `bridge/`, so the core and `BridgeClient` connect to it unchanged. Like
that package, it is game-agnostic: it carries generic verbs and facts, and never a title, control,
field or genre.

## How it connects

A browser page cannot listen on TCP, so a small Node relay does it for the page:

```
core (BridgeClient) --TCP JSONL--> relay --WebSocket--> page (BridgeSession)
```

The relay writes `<project>/Temp/playtest-bridge.lock` while a page is attached, and serves one
core at a time; a second core gets `busy`. It passes lines through unchanged. The page's session does
hello, auth and every reply. When the page closes or reloads, the lock is removed and the core
connection ends, so the core fails closed.

## Use it in a game

```js
import * as THREE from "three";
import { installBridge } from "@playtest/bridge-three";

const bridge = installBridge({ engineVersion: THREE.REVISION });
// ... build the scene and camera ...
bridge?.attach({ sceneId: "level-1", scene, camera });
renderer.setAnimationLoop(render);
```

`installBridge` does nothing (it returns null) unless the page URL carries `?playtest=<relay url>`.
Install it before the game starts its loop.

Start the relay with the game's project directory, then open the game with the printed URL as its
`playtest` parameter:

```sh
node bridge-three/relay/relay.mjs --project path/to/game
# {"playtest":"ws://127.0.0.1:53817/?key=..."}
# open http://localhost:5173/?playtest=<that URL, URL-encoded>
```

## The clock

The bridge routes the page's `requestAnimationFrame`, `cancelAnimationFrame` and `performance.now`
through a virtual clock. Pausing freezes the frames and the time the game reads, and a step advances
game time exactly, in frames of `stepFrameS` (1/60 s by default). `clockMode: "stepped"` runs frames
only when the core steps the clock. `setTimeout`, `setInterval` and `Date.now()` are not virtualised,
so a game that times itself with them is not paused.

## What v0 serves

| Verb | Status |
|---|---|
| `hello` / `status` | `engine: "three"`, `unity_version: null`, no input backends, no capture |
| `observe` | Scene id, frame, game time, clock state, camera yaw and pitch. `public` is empty until preflight readers exist |
| `clock` | pause, resume, step, set_scale |
| `stop` | Releases nothing, because this bridge applies no input yet |
| `act`, `place` | Refused with `invalid` |
| `inspect` | Refused with `internal`: there is no inspection source yet |

Yaw and pitch follow the protocol's `ViewAngles` convention. A heading of 0 faces world −Z, a
positive yaw turns right toward +X, and a positive pitch looks down.

## Tests

```sh
cd bridge-three && node --test     # unit tests and the relay end to end
uv run pytest tests/test_bridge_three.py   # the production Python client against this bridge
```

Both use `support/sim-page.mjs`: the real relay and page connector, with a timer-driven clock and a
camera stand-in. They need Node 22 or later, and no browser or GPU.
