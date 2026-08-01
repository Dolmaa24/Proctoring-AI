/**
 * Transient top-right notices shown to the candidate.
 *
 * Two deliberate choices about what these say and who they are for.
 *
 * **They are addressed to the candidate, not the proctor.** A candidate
 * who does not know they have been flagged cannot correct the thing that
 * caused it — moving a phone off the desk, turning on a light, sitting
 * back in frame. Silent observation followed by a post-exam accusation is
 * the failure mode this project exists to avoid, so the wording is
 * corrective ("A phone is visible — please move it out of view") rather
 * than accusatory ("Phone detected: violation logged").
 *
 * **They are driven by local detections, not by server flags.** That makes
 * them immediate, but it also means a toast is *not* proof a flag was
 * raised: the server applies onset windows and confidence thresholds this
 * code does not, so a brief phone-shaped blur can toast without ever
 * becoming a violation. This is the right trade — warning early and
 * sometimes unnecessarily is kinder than warning late and never — but it
 * does mean a toast must never be phrased as if a decision has been made.
 */

const DEFAULT_DURATION_MS = 6_000;

export type ToastTone = "info" | "warn" | "strike";

export interface Toast {
  key: string;
  title: string;
  body?: string;
  tone: ToastTone;
  durationMs?: number;
}

/**
 * Re-notifying about the same ongoing condition is noise. A phone sitting
 * on the desk for two minutes is one thing to tell someone about, not
 * twenty, so each key has a floor on how often it can reappear.
 */
const REPEAT_FLOOR_MS: Record<ToastTone, number> = {
  info: 30_000,
  warn: 20_000,
  // Strikes are the exception: every one of them must be shown, because
  // the count is the whole point and a suppressed strike would leave the
  // candidate with a wrong idea of how many they have left.
  strike: 0,
};

export class ToastHost {
  #root: HTMLElement;
  #lastShown = new Map<string, number>();

  constructor(root: HTMLElement) {
    this.#root = root;
  }

  show(toast: Toast, now = Date.now()): boolean {
    const floor = REPEAT_FLOOR_MS[toast.tone];
    const previous = this.#lastShown.get(toast.key);
    if (floor > 0 && previous !== undefined && now - previous < floor) return false;
    this.#lastShown.set(toast.key, now);

    const element = document.createElement("div");
    element.className = `toast toast-${toast.tone}`;
    // Assertive only for strikes: they carry a countdown the candidate has
    // to act on. Interrupting a screen reader mid-sentence for an advisory
    // notice would be its own accessibility problem.
    element.setAttribute("role", toast.tone === "strike" ? "alert" : "status");

    const title = document.createElement("strong");
    title.textContent = toast.title;
    element.append(title);

    if (toast.body) {
      const body = document.createElement("span");
      body.textContent = toast.body;
      element.append(body);
    }

    this.#root.append(element);

    const duration = toast.durationMs ?? DEFAULT_DURATION_MS;
    window.setTimeout(() => {
      element.classList.add("toast-leaving");
      window.setTimeout(() => element.remove(), 250);
    }, duration);

    return true;
  }
}

/** Wording for the detections the renderer can raise locally. */
export const TOASTS = {
  phone: {
    key: "phone",
    tone: "warn",
    title: "A phone is visible",
    body: "Please move it out of the camera view.",
  },
  secondPerson: {
    key: "second-person",
    tone: "warn",
    title: "Another person is visible",
    body: "You should be alone for this session.",
  },
  multipleFaces: {
    key: "multiple-faces",
    tone: "warn",
    title: "More than one face is visible",
    body: "You should be alone for this session.",
  },
  book: {
    key: "book",
    tone: "info",
    title: "A book or paper is visible",
    body: "Remove it unless your exam permits it.",
  },
  blurry: {
    key: "blurry",
    tone: "info",
    title: "Your camera looks blurred",
    body: "Clean the lens or refocus so your face is clear.",
  },
  poorLight: {
    key: "poor-light",
    tone: "info",
    title: "The lighting is making you hard to see",
    body: "Face a light source, or move away from a bright window.",
  },
  absent: {
    key: "absent",
    tone: "warn",
    title: "You are not visible",
    body: "Please return to the camera view.",
  },
} as const satisfies Record<string, Omit<Toast, "durationMs">>;
