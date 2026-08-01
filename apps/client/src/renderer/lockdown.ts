/**
 * Exam shell lockdown: fullscreen, restricted keys, and a strike allowance.
 *
 * The keystroke classifier is a pure function so the whole restricted set
 * can be tested without a browser, a keyboard, or a person.
 *
 * What "lockdown" does and does not mean here
 * -------------------------------------------
 * This blocks the *accidental* and the *casual*: the reflexive Cmd-C, the
 * ESC that drops fullscreen mid-question, the right-click out of habit. It
 * does not stop anyone determined. A candidate can alt-tab at the OS level,
 * use a second machine, or photograph the screen with a phone, and no
 * amount of `preventDefault` in a renderer changes that. Treating this as
 * a security boundary would be a mistake; it is a guardrail plus an
 * observation channel, and the OS-level signals in the main process
 * (`environment.ts`) are what actually notice the serious cases.
 *
 * Why an allowance rather than immediate escalation
 * -------------------------------------------------
 * ESC is muscle memory and Cmd-C is reflex. Someone who hits one in the
 * first minute of a stressful exam has not cheated, and a system that
 * escalates on the first press generates flags a human then has to
 * dismiss — which trains reviewers to dismiss flags generally. Three
 * warnings with a visible count means an exhausted allowance is a
 * deliberate pattern rather than a slip, which is a far more reviewable
 * claim.
 */

import type { LockdownEvent, LockdownSignal } from "../protocol/events.generated.ts";

export const DEFAULT_ALLOWANCE = 3;

export interface RestrictedKey {
  event: LockdownEvent;
  detail: string;
}

/** The chord description a human would recognise, for the reviewer. */
function describe(e: KeyboardEvent, name: string): string {
  const parts: string[] = [];
  if (e.metaKey) parts.push("Meta");
  if (e.ctrlKey) parts.push("Ctrl");
  if (e.altKey) parts.push("Alt");
  if (e.shiftKey) parts.push("Shift");
  parts.push(e.key.length === 1 ? e.key.toUpperCase() : e.key);
  return `${parts.join("+")} — ${name}`;
}

/**
 * Classify a keystroke, or null if it is allowed.
 *
 * Note what is deliberately *not* restricted: ordinary typing, arrow keys,
 * backspace, tab within the page. An exam shell that fights the candidate's
 * normal input is one they cannot actually work in.
 */
export function classifyKey(e: KeyboardEvent): RestrictedKey | null {
  const mod = e.metaKey || e.ctrlKey;

  if (e.key === "Escape") {
    return { event: "fullscreen_exit", detail: "Escape — exit fullscreen" };
  }

  if (mod) {
    const key = e.key.toLowerCase();
    const clipboard: Record<string, string> = { c: "copy", v: "paste", x: "cut" };
    if (key in clipboard) {
      return { event: "clipboard", detail: describe(e, clipboard[key]) };
    }
    if (key === "a") return { event: "restricted_key", detail: describe(e, "select all") };
    if (key === "p") return { event: "restricted_key", detail: describe(e, "print") };
    if (key === "s") return { event: "restricted_key", detail: describe(e, "save page") };
    if (key === "f") return { event: "restricted_key", detail: describe(e, "find in page") };
    if (key === "u") return { event: "restricted_key", detail: describe(e, "view source") };
    // Cmd/Ctrl+T, +N, +W: new tab / window / close.
    if (["t", "n", "w"].includes(key)) {
      return { event: "tab_switch", detail: describe(e, "new or close window") };
    }
    // DevTools: Cmd+Opt+I (mac), Ctrl+Shift+I / J / C.
    if (e.shiftKey && ["i", "j", "c"].includes(key)) {
      return { event: "restricted_key", detail: describe(e, "developer tools") };
    }
    if (e.altKey && ["i", "j"].includes(key)) {
      return { event: "restricted_key", detail: describe(e, "developer tools") };
    }
  }

  // Bare F12 is DevTools on most platforms.
  if (e.key === "F12") {
    return { event: "restricted_key", detail: "F12 — developer tools" };
  }

  // Alt+Tab / Cmd+Tab are intercepted by the OS before the page sees them
  // on most platforms, so catching them here is best-effort only. The
  // authoritative signal for the candidate leaving the window is
  // `window_focused` from the main process, not this.
  if (e.altKey && e.key === "Tab") {
    return { event: "tab_switch", detail: "Alt+Tab — switch application" };
  }

  return null;
}

export interface StrikeState {
  strike: number;
  allowance: number;
  exhausted: boolean;
}

/**
 * Counts strikes and produces the signal for each one.
 *
 * The count is local so the candidate can be warned instantly and
 * accurately. It is *not* the authority: `LockdownSignal.strike` is an
 * observation like everything else on this wire, and the server re-derives
 * the total from the stream it received. A tampered client that reports
 * `strike: 0` forever does not thereby have zero strikes — it has a
 * sequence of lockdown signals the fusion engine counts for itself.
 */
export class StrikeCounter {
  #strike = 0;
  readonly allowance: number;

  constructor(allowance = DEFAULT_ALLOWANCE) {
    this.allowance = allowance;
  }

  get state(): StrikeState {
    return {
      strike: this.#strike,
      allowance: this.allowance,
      exhausted: this.#strike >= this.allowance,
    };
  }

  record(restricted: RestrictedKey): { signal: LockdownSignal; state: StrikeState } {
    this.#strike += 1;
    return {
      signal: {
        type: "signal.lockdown",
        event: restricted.event,
        strike: this.#strike,
        allowance: this.allowance,
        detail: restricted.detail,
        confidence: 1,
      },
      state: this.state,
    };
  }
}

/** Candidate-facing wording for a strike, with the count spelled out. */
export function strikeMessage(state: StrikeState): { title: string; body: string } {
  const remaining = Math.max(0, state.allowance - state.strike);
  if (state.exhausted) {
    return {
      title: "That action is not allowed during the exam",
      body:
        `You have used all ${state.allowance} warnings. Further attempts are ` +
        `recorded and will be reviewed by a person.`,
    };
  }
  return {
    title: "That action is not allowed during the exam",
    body:
      `Warning ${state.strike} of ${state.allowance}. ` +
      `${remaining} ${remaining === 1 ? "warning remains" : "warnings remain"}.`,
  };
}
