/**
 * The only bridge between the renderer and the privileged main process.
 *
 * Intentionally one function wide. The renderer can hand an observation
 * inward; it cannot read the session key, choose a sequence number, reach
 * the socket, or ask main to report anything about the OS. Everything the
 * renderer is allowed to influence passes through here and is re-checked
 * on the other side.
 */

import { contextBridge, ipcRenderer } from "electron";

import type { Payload } from "../protocol/events.generated.ts";

export interface MediaJoin {
  url: string;
  token: string;
}

contextBridge.exposeInMainWorld("proctor", {
  /**
   * Report one observation. Resolves false if main rejected it.
   *
   * Rejection is not an error the renderer should retry around — it means
   * the payload was a kind the renderer is not permitted to originate.
   */
  observe: (payload: Payload): Promise<boolean> =>
    ipcRenderer.invoke("proctor:observe", payload),

  /**
   * The LiveKit URL and a publish-only token for this candidate's room, or
   * null if the media plane is disabled or unreachable.
   *
   * Handing the renderer a token is not the same trust decision as
   * withholding the telemetry session key in telemetry.ts. That key signs
   * arbitrary observations on Node's HMAC primitives and has no reason to
   * ever leave main. A LiveKit token has to reach the renderer no matter
   * what: `livekit-client` needs `RTCPeerConnection`/`getUserMedia`, which
   * only exist in a DOM context. What keeps this safe to hand over is the
   * token's own shape, not where it lives — it is structurally publish-
   * only (see `proctor_media.tokens.VideoGrant.publisher` and
   * ARCHITECTURE.md § 5.6), so a compromised renderer leaking it is no
   * more useful to an attacker than the camera access it already has.
   */
  getMediaJoin: (): Promise<MediaJoin | null> => ipcRenderer.invoke("proctor:media-join"),

  /**
   * Report that the candidate accepted the disclaimer.
   *
   * Deliberately not `observe({type: "lifecycle", phase: "exam_start"})`.
   * Main rejects any payload from the renderer that is not a `signal.*`,
   * and that rule is worth keeping: a renderer able to originate arbitrary
   * lifecycle phases could claim `identity_verified` or `session_end` it
   * has no standing to assert. So the renderer reports the one fact it
   * actually witnessed — the button was pressed — and main decides what
   * that means on the wire and tells the gateway.
   */
  grantConsent: (): Promise<boolean> => ipcRenderer.invoke("proctor:consent"),
});

declare global {
  interface Window {
    proctor: {
      observe(payload: Payload): Promise<boolean>;
      getMediaJoin(): Promise<MediaJoin | null>;
      grantConsent(): Promise<boolean>;
    };
  }
}
