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
bridge?.attach({ sceneId: "level-1", scene, camera, canvas: renderer.domElement });
renderer.setAnimationLoop(render);
```

`installBridge` does nothing (it returns null) unless the page URL carries `?playtest=<relay url>`.
Install it before the game starts its loop.
By default, keyboard events target `document`, pointer movement targets `document`, and pointer
buttons target the first canvas. Pass `inputTargets` to `installBridge` to set `keyboardTarget`,
`pointerTarget`, or `canvas` explicitly.

Start the relay with the game's project directory, then open the game with the printed URL as its
`playtest` parameter:

```sh
node bridge-three/relay/relay.mjs --project path/to/game
# {"playtest":"ws://127.0.0.1:53817/?key=..."}
# open http://localhost:5173/?playtest=<that URL, URL-encoded>
```

Pass `--catalog path/to/actions.json` to pin the page to an action catalog. The relay reads the JSON,
computes its SHA-256, and sends both with the endpoint message. The page validates the catalog before
it accepts actions. A missing catalog has no action names, so an act gets `unknown_action`.

The catalog uses the same `ActionCatalog` shape as the Unity bridge:

```json
{
  "actions": {
    "move_left": {
      "backend": "input_system",
      "inputs": [{"device": "Keyboard", "control": "a"}],
      "default_lease_s": 0.2
    }
  }
}
```

`backend: "input_system"` selects the shared control vocabulary. This bridge delivers those controls
as DOM events and a virtual standard gamepad. It does not interpret action names or change declared
values. Keyboard and mouse release checks read the delivered state recorded by capture-phase event
listeners. Gamepad release checks read the virtual gamepad state. `release_verified` means that this
bridge observed its own delivered state return to neutral. It does not prove that the game handled an
event or produced an effect.

## The clock

The bridge routes the page's `requestAnimationFrame`, `cancelAnimationFrame` and `performance.now`
through a virtual clock. Pausing freezes the frames and the time the game reads, and a step advances
game time exactly, in frames of `stepFrameS` (1/60 s by default). `clockMode: "stepped"` runs frames
only when the core steps the clock. `setTimeout`, `setInterval` and `Date.now()` are not virtualised,
so a game that times itself with them is not paused.

## What it serves

| Verb | Status |
|---|---|
| `hello` / `status` | `engine: "three"`, `unity_version: null`, the `dom` input backend; `capture` once a canvas is attached, with its self-test result |
| `observe` | Scene id, frame, game time, clock state, camera yaw and pitch, and with `with_frame` the newest written frame. `public` is empty until preflight readers exist |
| `act` | Runs the exact catalog action or skill. Holds, waits, leases and skill deadlines use wall time, so pausing the virtual clock does not keep an input held |
| `clock` | pause, resume, step, set_scale |
| `stop` | Releases every held DOM input and reports whether the delivered state is neutral |
| `inspect` | `transforms`: every object in the attached scene with its world position and basis, and the runtime error monitor. `fields`: no game fields yet (they come with a preflight), and the delivered input state per device, which the input probe reads to confirm a neutral release. Other stages are refused with `internal` |
| `place` | Refused with `invalid` |

## Coordinates

Every world position and direction the bridge reports is in the protocol's frame: x right, y up and
z negated from three.js. That frame is left-handed, like the Unity bridge's, so the core's yaw formula
`atan2(forward.x, forward.z)` turns positive to the right. An object's forward is its local +Z
(three.js's `lookAt` convention) and its right is local −X. A camera looks down its local −Z, with
local +X to its right. The camera's `ViewAngles` follow the same convention: a heading of 0 faces
three.js −Z, a positive yaw turns right toward +X, and a positive pitch looks down. Positions are in
`three_world_unit`, which is whatever one unit means in the game.

## Frames

With a canvas attached, the page grabs a frame right after a game frame's callbacks, in the same
task, so the canvas still holds what the game drew. It grabs at most 10 a second, scales them to
640 px wide, and sends each as a JPEG to the relay. The relay writes it atomically to
`<project>/Temp/playtest-bridge-frames/f-NNNNNN.jpg`, keeps the newest 120 as the Unity bridge does,
and reports the path back. `frame_ref` names only a frame the relay confirmed written. The capture
self-test passes once the first frame is written and its pixels read back as something other than
fully transparent, which is what an unreadable canvas gives.

## Runtime errors

The page's uncaught errors, unhandled rejections, `console.error` calls and errors thrown by frame
callbacks are counted the way the Unity bridge counts error logs, and reported with each
`transforms` inspection as `runtime_errors`.

## Tests

```sh
cd bridge-three && node --test     # unit tests and the relay end to end
uv run pytest tests/test_bridge_three.py   # the production Python client against this bridge
```

Both use `support/sim-page.mjs`: the real relay and page connector, with a timer-driven clock, a
small scene of object stand-ins and a grabber that yields a fixed tiny JPEG. They need Node 22 or
later, and no browser or GPU.
