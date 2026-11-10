import { KEY_CODES } from "./catalog.js";

const BUTTON_INDEX = Object.freeze({ buttonSouth: 0, buttonEast: 1, buttonWest: 2, buttonNorth: 3, leftShoulder: 4, rightShoulder: 5, leftTrigger: 6, rightTrigger: 7, select: 8, start: 9, leftStickPress: 10, rightStickPress: 11 });
const DPAD_INDEX = Object.freeze({ up: 12, down: 13, left: 14, right: 15 });
const BUTTON_CODE = Object.freeze({ leftButton: 0, rightButton: 2, middleButton: 1 });

function event(target, kind, fields) {
  const EventType = kind.startsWith("key") ? (target?.KeyboardEvent ?? globalThis.KeyboardEvent)
    : kind.startsWith("pointer") ? (target?.PointerEvent ?? globalThis.PointerEvent ?? target?.MouseEvent ?? globalThis.MouseEvent)
      : (target?.MouseEvent ?? globalThis.MouseEvent);
  let result;
  if (EventType) result = new EventType(kind, { bubbles: true, cancelable: true, ...fields });
  else {
    result = new Event(kind, { bubbles: true, cancelable: true });
    for (const [key, value] of Object.entries(fields)) Object.defineProperty(result, key, { value });
  }
  if (fields.code && result.code !== fields.code) Object.defineProperty(result, "code", { value: fields.code });
  if (fields.key && result.key !== fields.key) Object.defineProperty(result, "key", { value: fields.key });
  for (const key of ["movementX", "movementY", "button", "buttons", "pointerId", "pointerType"]) if (fields[key] !== undefined && result[key] !== fields[key]) Object.defineProperty(result, key, { value: fields[key] });
  if (typeof target?.dispatchEvent !== "function") throw new Error(`no event target is available for ${kind}`);
  target.dispatchEvent(result);
}

function button(value = 0) { return { pressed: value > 0, touched: value > 0, value }; }
function keyValue(control, code) {
  if (/^[a-z]$/.test(control)) return control;
  if (/^digit\d$/.test(control) || /^numpad\d$/.test(control)) return control.slice(-1);
  return ({ space: " ", enter: "Enter", escape: "Escape", tab: "Tab", backspace: "Backspace", leftshift: "Shift", rightshift: "Shift", leftctrl: "Control", rightctrl: "Control", leftalt: "Alt", rightalt: "Alt", leftmeta: "Meta", rightmeta: "Meta", minus: "-", equals: "=", leftbracket: "[", rightbracket: "]", backslash: "\\", semicolon: ";", quote: "'", backquote: "`", comma: ",", period: ".", slash: "/", numpadenter: "Enter", numpadplus: "+", numpadminus: "-", numpadmultiply: "*", numpaddivide: "/", numpadperiod: ".", numpadcomma: ",", numpadequals: "=" }[control] ?? code);
}

export class DomInputSurface {
  #clock;
  #window;
  #keyboard;
  #document;
  #canvas;
  #keys = new Set();
  #mouse = new Set();
  #buttons = Array.from({ length: 16 }, () => button());
  #axes = [0, 0, 0, 0];
  #gamepadHeld = new Set();
  #gamepadSent = false;
  #originalGetGamepads;
  #navigator;
  #captureTarget;
  #lastDelta = null;
  #deltaFrame = null;
  #padTimestamp = Date.now() - (globalThis.performance?.timeOrigin ?? Date.now());

  constructor({ clock, target = globalThis, windowTarget = target.window ?? target, documentTarget = target.document ?? windowTarget, keyboardTarget = documentTarget, pointerTarget = documentTarget, canvas = null } = {}) {
    this.#clock = clock;
    this.#window = windowTarget;
    this.#keyboard = keyboardTarget;
    this.#document = pointerTarget;
    this.#canvas = canvas ?? documentTarget?.querySelector?.("canvas") ?? null;
    this.#captureTarget = this.#canvas ?? pointerTarget;
    const controlsByCode = new Map([...KEY_CODES].map(([control, code]) => [code, control]));
    const down = e => { const control = controlsByCode.get(e.code); if (control) this.#keys.add(control); };
    const up = e => { const control = controlsByCode.get(e.code); if (control) this.#keys.delete(control); };
    keyboardTarget?.addEventListener?.("keydown", down, true);
    keyboardTarget?.addEventListener?.("keyup", up, true);
    if (windowTarget !== keyboardTarget) {
      windowTarget?.addEventListener?.("keydown", down, true);
      windowTarget?.addEventListener?.("keyup", up, true);
    }
    const moved = e => { this.#lastDelta = { x: e.movementX, y: e.movementY }; this.#deltaFrame = clock.frame; };
    pointerTarget?.addEventListener?.("pointermove", moved, true);
    pointerTarget?.addEventListener?.("mousemove", moved, true);
    this.#captureTarget?.addEventListener?.("pointerdown", e => { const control = Object.keys(BUTTON_CODE).find(name => BUTTON_CODE[name] === e.button); if (control) this.#mouse.add(control); }, true);
    this.#captureTarget?.addEventListener?.("pointerup", e => { const control = Object.keys(BUTTON_CODE).find(name => BUTTON_CODE[name] === e.button); if (control) this.#mouse.delete(control); }, true);
    this.#navigator = target.navigator ?? globalThis.navigator;
    if (this.#navigator) {
      this.#originalGetGamepads = this.#navigator.getGamepads;
      const gamepad = () => this.#gamepad();
      try { this.#navigator.getGamepads = () => [gamepad()]; } catch {
        try { Object.defineProperty(this.#navigator, "getGamepads", { configurable: true, value: () => [gamepad()] }); } catch { this.#navigator = null; }
      }
    }
  }

  get available() {
    const keyboard = this.#keyboard?.dispatchEvent && this.#keyboard?.addEventListener;
    const pointer = this.#document?.dispatchEvent && this.#document?.addEventListener;
    const gamepad = this.#navigator && this.#window?.dispatchEvent;
    return Boolean((keyboard && pointer) || gamepad);
  }
  supports(input) {
    if (input.device === "Keyboard") return Boolean(this.#keyboard?.dispatchEvent && this.#keyboard?.addEventListener);
    if (input.device === "Mouse") return Boolean(this.#document?.dispatchEvent && this.#document?.addEventListener && (this.#canvas ?? this.#document)?.dispatchEvent && (this.#canvas ?? this.#document)?.addEventListener);
    return Boolean(this.#navigator && this.#window?.dispatchEvent);
  }

  apply(input) {
    if (input.device === "Keyboard") {
      const code = KEY_CODES.get(input.control);
      event(this.#keyboard, "keydown", { code, key: keyValue(input.control, code), repeat: false });
    } else if (input.device === "Mouse" && input.control === "delta") {
      const dx = input.x * 64, dy = input.y * 64;
      this.#lastDelta = null;
      event(this.#document, "pointermove", { movementX: dx, movementY: dy, pointerId: 1, pointerType: "mouse" });
      event(this.#document, "mousemove", { movementX: dx, movementY: dy });
      return { applied_delta_x: this.#lastDelta?.x ?? null, applied_delta_y: this.#lastDelta?.y ?? null };
    } else if (input.device === "Mouse") {
      const code = BUTTON_CODE[input.control];
      const target = this.#canvas ?? this.#document;
      event(target, "pointerdown", { button: code, buttons: 1 << code, pointerId: 1, pointerType: "mouse" });
      event(target, "mousedown", { button: code, buttons: 1 << code });
    } else {
      this.#applyGamepad(input);
    }
    return { applied_delta_x: null, applied_delta_y: null };
  }

  release(input) {
    if (input.device === "Keyboard") {
      const code = KEY_CODES.get(input.control);
      event(this.#keyboard, "keyup", { code, key: keyValue(input.control, code), repeat: false });
    } else if (input.device === "Mouse" && input.control !== "delta") {
      const code = BUTTON_CODE[input.control];
      const target = this.#canvas ?? this.#document;
      event(target, "pointerup", { button: code, buttons: 0, pointerId: 1, pointerType: "mouse" });
      event(target, "mouseup", { button: code, buttons: 0 });
    } else if (input.device === "Gamepad") {
      this.#neutralGamepad(input.control);
    }
    return this.isReleased(input);
  }

  observeFrame() { return this.#afterDispatch(); }

  isReleased(input) {
    if (input.device === "Keyboard") return !this.#keys.has(input.control);
    if (input.device === "Mouse") return input.control === "delta" || !this.#mouse.has(input.control);
    if (input.control === "leftStick") return this.#axes[0] === 0 && this.#axes[1] === 0;
    if (input.control === "rightStick") return this.#axes[2] === 0 && this.#axes[3] === 0;
    if (input.control === "dpad") return [12, 13, 14, 15].every(i => !this.#buttons[i].pressed);
    return !this.#gamepadValues(input.control).some(i => this.#buttons[i]?.value > 0);
  }

  /**
   * The delivered device state, in the Unity bridge's input-state shape: each device's pressed controls
   * and its vector and axis values. A mouse delta reads as moving only until the game frame after it
   * was delivered has run; after that it has been consumed and reads as zero.
   */
  state() {
    const delta = this.#lastDelta !== null && this.#deltaFrame !== null && this.#clock.frame <= this.#deltaFrame + 1
      ? [this.#lastDelta.x, this.#lastDelta.y] : [0, 0];
    const pressed = Object.keys(BUTTON_INDEX).filter(name => this.#buttons[BUTTON_INDEX[name]].pressed
      && name !== "leftTrigger" && name !== "rightTrigger");
    const dpad = [(this.#buttons[15].pressed ? 1 : 0) - (this.#buttons[14].pressed ? 1 : 0),
      (this.#buttons[12].pressed ? 1 : 0) - (this.#buttons[13].pressed ? 1 : 0)];
    return {
      Keyboard: { pressed: [...this.#keys].sort(), values: {} },
      Mouse: { pressed: [...this.#mouse].sort(), values: { delta } },
      Gamepad: { pressed, values: {
        leftStick: [this.#axes[0], -this.#axes[1] + 0], rightStick: [this.#axes[2], -this.#axes[3] + 0],
        leftTrigger: this.#buttons[6].value, rightTrigger: this.#buttons[7].value, dpad,
      } },
    };
  }

  releasedNames() { return [...this.#keys].map(x => `key:${x.toLowerCase()}`).concat([...this.#mouse].map(x => `mouse:${x.toLowerCase()}`), [...this.#gamepadHeld].map(x => `gamepad:${x.toLowerCase()}`)); }

  releaseAll() {
    const held = [
      ...[...this.#keys].map(control => ({ device: "Keyboard", control })),
      ...[...this.#mouse].map(control => ({ device: "Mouse", control })),
      ...[...this.#gamepadHeld].map(control => ({ device: "Gamepad", control })),
    ];
    for (const input of held) this.release(input);
    return this.releasedNames().length === 0;
  }

  dispatchConnected() {
    if (this.#gamepadSent || !this.#navigator) return;
    this.#gamepadSent = true;
    event(this.#window, "gamepadconnected", { gamepad: this.#gamepad() });
  }

  #gamepad() { return { id: "DOM virtual gamepad", index: 0, connected: true, mapping: "standard", axes: [...this.#axes], buttons: this.#buttons.map(b => ({ ...b })), timestamp: this.#padTimestamp, hapticActuators: [], vibrationActuator: null }; }
  #gamepadValues(control) {
    if (control === "dpad") return [12, 13, 14, 15];
    if (control in BUTTON_INDEX) return [BUTTON_INDEX[control]];
    return [];
  }
  #applyGamepad(input) {
    this.#padTimestamp = Date.now() - (globalThis.performance?.timeOrigin ?? Date.now());
    if (input.control === "leftStick" || input.control === "rightStick") {
      const at = input.control === "leftStick" ? 0 : 2;
      this.#axes[at] = input.x;
      this.#axes[at + 1] = -input.y;
    } else if (input.control === "dpad") {
      this.#buttons[12] = button(input.y < 0 ? 1 : 0);
      this.#buttons[13] = button(input.y > 0 ? 1 : 0);
      this.#buttons[14] = button(input.x < 0 ? 1 : 0);
      this.#buttons[15] = button(input.x > 0 ? 1 : 0);
    } else if (input.control === "leftTrigger" || input.control === "rightTrigger") {
      this.#buttons[BUTTON_INDEX[input.control]] = button(input.x);
    } else {
      this.#buttons[BUTTON_INDEX[input.control]] = button(1);
    }
    this.#gamepadHeld.add(input.control);
    this.dispatchConnected();
  }
  #neutralGamepad(control) {
    this.#padTimestamp = Date.now() - (globalThis.performance?.timeOrigin ?? Date.now());
    if (control === "leftStick" || control === "rightStick") {
      const at = control === "leftStick" ? 0 : 2;
      this.#axes[at] = 0; this.#axes[at + 1] = 0;
    } else for (const at of this.#gamepadValues(control)) this.#buttons[at] = button();
    this.#gamepadHeld.delete(control);
  }
  #afterDispatch() {
    return new Promise(resolve => {
      let settled = false;
      let frameId;
      const finish = observed => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (!observed) this.#clock.cancelAnimationFrame(frameId);
        resolve(observed);
      };
      const timer = setTimeout(() => finish(false), 100);
      frameId = this.#clock.requestAnimationFrame(() => finish(true));
    });
  }
}
