const NAME = /^[a-z][a-z0-9_]{0,39}$/;
const BUTTONS = new Set(["buttonSouth", "buttonEast", "buttonWest", "buttonNorth", "leftShoulder", "rightShoulder", "leftStickPress", "rightStickPress", "start", "select"]);
const KEY_CODES = new Map();
for (let i = 0; i < 26; i += 1) KEY_CODES.set(String.fromCharCode(97 + i), `Key${String.fromCharCode(65 + i)}`);
for (let i = 0; i <= 9; i += 1) KEY_CODES.set(`digit${i}`, `Digit${i}`);
for (let i = 1; i <= 12; i += 1) KEY_CODES.set(`f${i}`, `F${i}`);
for (let i = 0; i <= 9; i += 1) KEY_CODES.set(`numpad${i}`, `Numpad${i}`);
for (const [key, code] of Object.entries({ space: "Space", enter: "Enter", escape: "Escape", tab: "Tab", backspace: "Backspace", leftshift: "ShiftLeft", rightshift: "ShiftRight", leftctrl: "ControlLeft", rightctrl: "ControlRight", leftalt: "AltLeft", rightalt: "AltRight", leftarrow: "ArrowLeft", rightarrow: "ArrowRight", uparrow: "ArrowUp", downarrow: "ArrowDown", leftmeta: "MetaLeft", rightmeta: "MetaRight", capslock: "CapsLock", numlock: "NumLock", printscreen: "PrintScreen", scrolllock: "ScrollLock", pause: "Pause", insert: "Insert", home: "Home", pageup: "PageUp", delete: "Delete", end: "End", pagedown: "PageDown", contextmenu: "ContextMenu", minus: "Minus", equals: "Equal", leftbracket: "BracketLeft", rightbracket: "BracketRight", backslash: "Backslash", semicolon: "Semicolon", quote: "Quote", backquote: "Backquote", comma: "Comma", period: "Period", slash: "Slash", numpadenter: "NumpadEnter", numpadplus: "NumpadAdd", numpadminus: "NumpadSubtract", numpadmultiply: "NumpadMultiply", numpaddivide: "NumpadDivide", numpadperiod: "NumpadDecimal", numpadcomma: "NumpadComma", numpadequals: "NumpadEqual" })) KEY_CODES.set(key, code);

function finite(v) { return typeof v === "number" && Number.isFinite(v); }
function fail(message) { throw new TypeError(message); }
function exactKeys(value, allowed, label) {
  for (const key of Object.keys(value)) if (!allowed.includes(key)) fail(`${label} has unknown member ${key}`);
}

function validateBinding(binding) {
  if (!binding || !["Keyboard", "Mouse", "Gamepad"].includes(binding.device)) fail("unsupported input device");
  exactKeys(binding, ["device", "control", "x", "y"], "input");
  if (typeof binding.control !== "string" || !binding.control || binding.control.length > 40) fail("invalid control");
  const vector = binding.x !== null && binding.x !== undefined && binding.y !== null && binding.y !== undefined;
  if ((binding.x == null) !== (binding.y == null)) fail("x and y must both be present or null");
  if (vector && (!finite(binding.x) || !finite(binding.y) || binding.x < -1 || binding.x > 1 || binding.y < -1 || binding.y > 1)) fail("x and y must be finite values in [-1, 1]");
  if (binding.device === "Keyboard") {
    if (vector || !KEY_CODES.has(binding.control)) fail(`unknown keyboard control ${binding.control}`);
  } else if (binding.device === "Mouse") {
    if (binding.control === "delta") { if (!vector) fail("mouse delta needs x and y"); }
    else if (binding.control === "position") { if (!vector) fail("mouse position needs x and y"); }
    else if (!["leftButton", "rightButton", "middleButton"].includes(binding.control) || vector) fail("unsupported mouse control");
  } else if (binding.control === "leftStick" || binding.control === "rightStick" || binding.control === "dpad") {
    if (!vector) fail("gamepad vector needs x and y");
    if (binding.control === "dpad" && (![binding.x, binding.y].every(v => [-1, 0, 1].includes(v)))) fail("dpad values must be -1, 0 or 1");
  } else if (binding.control === "leftTrigger" || binding.control === "rightTrigger") {
    if (!vector || binding.y !== 0 || binding.x < 0) fail("trigger needs x in [0, 1] and y = 0");
  } else if (!BUTTONS.has(binding.control) || vector) fail("unsupported gamepad control");
}

function validateDefinition(definition) {
  if (!definition || definition.backend !== "input_system") fail("backend must be input_system");
  exactKeys(definition, ["backend", "inputs", "default_lease_s", "steps", "max_s"], "definition");
  const action = Array.isArray(definition.inputs) || definition.default_lease_s != null;
  const skill = Array.isArray(definition.steps) || definition.max_s != null;
  if (action === skill) fail("definition must be an action or skill");
  const validateInputs = (inputs) => {
    if (!Array.isArray(inputs) || inputs.length < 1 || inputs.length > 3) fail("inputs must contain 1..3 bindings");
    const seen = new Set();
    for (const binding of inputs) {
      validateBinding(binding);
      const id = `${binding.device}/${binding.control}`;
      if (seen.has(id)) fail(`duplicate input ${id}`);
      seen.add(id);
    }
  };
  const positive = (v) => finite(v) && v > 0 && v <= 3600;
  if (action) {
    if (!positive(definition.default_lease_s) || definition.steps != null || definition.max_s != null) fail("action needs inputs and default_lease_s only");
    validateInputs(definition.inputs);
  } else {
    if (!Array.isArray(definition.steps) || definition.steps.length < 1 || definition.steps.length > 8 || !positive(definition.max_s)) fail("skill needs 1..8 steps and max_s");
    let total = 0;
    for (const step of definition.steps) {
      exactKeys(step, ["inputs", "hold_s", "wait_s"], "step");
      validateInputs(step.inputs);
      if (!positive(step.hold_s) || !finite(step.wait_s) || step.wait_s < 0 || step.wait_s > 3600) fail("invalid skill hold or wait");
      total += step.hold_s + step.wait_s;
    }
    if (total > definition.max_s) fail("skill holds and waits exceed max_s");
  }
}

export function validateCatalog(catalog) {
  if (!catalog || typeof catalog !== "object" || Array.isArray(catalog) || !catalog.actions || typeof catalog.actions !== "object" || Array.isArray(catalog.actions)) fail("catalog must contain actions object");
  exactKeys(catalog, ["actions"], "catalog");
  for (const [name, definition] of Object.entries(catalog.actions)) {
    if (!NAME.test(name)) fail(`invalid action name ${name}`);
    validateDefinition(definition);
  }
  return catalog;
}

export { KEY_CODES };
