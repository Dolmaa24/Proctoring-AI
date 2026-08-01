import assert from "node:assert/strict";
import { test } from "node:test";

import { StrikeCounter, classifyKey, strikeMessage } from "./lockdown.ts";

function key(init: Partial<KeyboardEvent> & { key: string }): KeyboardEvent {
  return {
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    shiftKey: false,
    ...init,
  } as KeyboardEvent;
}

test("Escape is classified as a fullscreen exit", () => {
  assert.equal(classifyKey(key({ key: "Escape" }))?.event, "fullscreen_exit");
});

test("clipboard shortcuts are caught on both Meta and Ctrl", () => {
  for (const mod of [{ metaKey: true }, { ctrlKey: true }]) {
    for (const k of ["c", "v", "x"]) {
      const result = classifyKey(key({ key: k, ...mod }));
      assert.equal(result?.event, "clipboard", `${JSON.stringify(mod)}+${k} not caught`);
    }
  }
});

test("developer tools shortcuts are caught", () => {
  assert.equal(classifyKey(key({ key: "F12" }))?.event, "restricted_key");
  assert.equal(
    classifyKey(key({ key: "i", ctrlKey: true, shiftKey: true }))?.event,
    "restricted_key",
  );
  assert.equal(classifyKey(key({ key: "i", metaKey: true, altKey: true }))?.event, "restricted_key");
});

test("ordinary typing is never restricted", () => {
  // An exam shell that fights normal input is one nobody can work in.
  for (const k of ["a", "z", "5", " ", "Backspace", "ArrowLeft", "Enter", "Shift", "Tab"]) {
    assert.equal(classifyKey(key({ key: k })), null, `${k} should be allowed`);
  }
});

test("a plain letter is only restricted when a modifier is held", () => {
  assert.equal(classifyKey(key({ key: "c" })), null);
  assert.notEqual(classifyKey(key({ key: "c", metaKey: true })), null);
});

test("the detail names the chord in a form a reviewer can read", () => {
  const result = classifyKey(key({ key: "c", metaKey: true }));
  assert.match(result!.detail, /Meta\+C/);
  assert.match(result!.detail, /copy/);
});

test("the counter reports remaining warnings and then exhaustion", () => {
  const counter = new StrikeCounter(3);
  const restricted = classifyKey(key({ key: "Escape" }))!;

  const first = counter.record(restricted);
  assert.equal(first.signal.strike, 1);
  assert.equal(first.signal.allowance, 3);
  assert.equal(first.state.exhausted, false);

  counter.record(restricted);
  const third = counter.record(restricted);
  assert.equal(third.signal.strike, 3);
  assert.equal(third.state.exhausted, true);
});

test("strikes keep counting past the allowance rather than clamping", () => {
  // The server re-derives the total from the stream; a client that stopped
  // counting at the allowance would under-report a candidate who kept
  // going, which is exactly the pattern worth reviewing.
  const counter = new StrikeCounter(3);
  const restricted = classifyKey(key({ key: "Escape" }))!;
  for (let i = 0; i < 5; i++) counter.record(restricted);
  assert.equal(counter.state.strike, 5);
  assert.equal(counter.state.exhausted, true);
});

test("the signal carries the shape the protocol requires", () => {
  const counter = new StrikeCounter();
  const { signal } = counter.record(classifyKey(key({ key: "v", ctrlKey: true }))!);
  assert.equal(signal.type, "signal.lockdown");
  assert.equal(signal.event, "clipboard");
  assert.ok(signal.strike >= 0);
  assert.ok(signal.allowance >= 0);
});

test("warning copy counts down and reads as a warning, not a verdict", () => {
  const counter = new StrikeCounter(3);
  const restricted = classifyKey(key({ key: "Escape" }))!;

  const first = strikeMessage(counter.record(restricted).state);
  assert.match(first.body, /Warning 1 of 3/);
  assert.match(first.body, /2 warnings remain/);

  counter.record(restricted);
  const third = strikeMessage(counter.record(restricted).state);
  assert.match(third.body, /all 3 warnings/);
  assert.match(third.body, /reviewed by a person/);
});

test("the singular case reads correctly", () => {
  const counter = new StrikeCounter(3);
  const restricted = classifyKey(key({ key: "Escape" }))!;
  counter.record(restricted);
  const second = strikeMessage(counter.record(restricted).state);
  assert.match(second.body, /1 warning remains/);
});
