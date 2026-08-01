/**
 * The consent gate. Nothing is captured before this resolves.
 *
 * The ordering here is the point, and it is not cosmetic: the camera is
 * not opened, no model is loaded, and no telemetry signal is emitted until
 * the candidate has read what will happen and pressed the button. A
 * disclaimer shown *while* the webcam light is already on is not consent,
 * it is notification, and the two are not interchangeable — see
 * ARCHITECTURE.md § 5.6.5 on why consent gates in this project fail closed.
 *
 * The copy below is deliberately specific. "Your session is monitored" is
 * the kind of sentence that gets written to avoid alarming people, and it
 * leaves a candidate genuinely unaware that their microphone is on and a
 * video file is being kept. Everything that is captured is named.
 */

export interface ConsentTerms {
  recording: boolean;
  microphone: boolean;
  retentionDays: number;
  allowance: number;
}

const ITEM_STYLE = "consent-item";

function item(text: string): HTMLLIElement {
  const li = document.createElement("li");
  li.className = ITEM_STYLE;
  li.textContent = text;
  return li;
}

/**
 * Show the disclaimer and resolve once the candidate accepts.
 *
 * There is no "decline" button, and that absence is itself a decision
 * worth stating: this shell cannot offer a meaningful alternative — the
 * exam simply does not proceed — and a decline button that closes the app
 * would dress an institutional requirement up as a free choice. The
 * candidate declines by closing the window, which is honest about what
 * declining actually costs them. An institution deploying this owes them a
 * real route to an unproctored alternative; that route is not something
 * this dialog can provide.
 */
export function requestConsent(terms: ConsentTerms): Promise<void> {
  const overlay = document.createElement("div");
  overlay.className = "consent-overlay";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-labelledby", "consent-title");

  const panel = document.createElement("div");
  panel.className = "consent-panel";

  const title = document.createElement("h1");
  title.id = "consent-title";
  title.textContent = "Before your proctored session begins";

  const lead = document.createElement("p");
  lead.textContent =
    "This session is monitored automatically. Here is exactly what that means:";

  const list = document.createElement("ul");
  list.append(
    item("Your camera is on for the whole session, and your face, gaze and head position are analysed on this device."),
  );
  if (terms.microphone) {
    list.append(item("Your microphone is on. Speech may be transcribed and reviewed."));
  }
  if (terms.recording) {
    list.append(
      item(
        `Video and audio of this session are recorded and kept for ${terms.retentionDays} days.`,
      ),
    );
  }
  list.append(
    item("Which applications and displays are active on this computer is checked periodically."),
  );
  list.append(
    item(
      `The window is locked to fullscreen. Leaving fullscreen, copying, pasting, or opening ` +
        `developer tools is blocked, and you get ${terms.allowance} warnings before further ` +
        `attempts are recorded.`,
    ),
  );

  const fairness = document.createElement("p");
  fairness.className = "consent-fairness";
  fairness.textContent =
    "Anything the system notices is a flag for a human to review, not a decision. " +
    "Automated monitoring makes mistakes, and no result here ends your exam on its own. " +
    "If something looks wrong to you during the session, tell your invigilator.";

  const button = document.createElement("button");
  button.className = "consent-accept";
  button.type = "button";
  button.textContent = "I understand — start my session";

  panel.append(title, lead, list, fairness, button);
  overlay.append(panel);
  document.body.append(overlay);
  button.focus();

  return new Promise<void>((resolve) => {
    button.addEventListener(
      "click",
      () => {
        overlay.remove();
        resolve();
      },
      { once: true },
    );
  });
}

/**
 * Enter fullscreen, reporting whether it worked.
 *
 * Never throws. A refused fullscreen request must not be the thing that
 * stops a candidate sitting an exam — the session continues and the
 * server sees the window state through `signal.environment` regardless.
 */
export async function enterFullscreen(): Promise<boolean> {
  try {
    if (document.fullscreenElement) return true;
    await document.documentElement.requestFullscreen({ navigationUI: "hide" });
    return true;
  } catch {
    return false;
  }
}
