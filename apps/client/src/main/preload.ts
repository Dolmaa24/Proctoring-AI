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

contextBridge.exposeInMainWorld("proctor", {
  /**
   * Report one observation. Resolves false if main rejected it.
   *
   * Rejection is not an error the renderer should retry around — it means
   * the payload was a kind the renderer is not permitted to originate.
   */
  observe: (payload: Payload): Promise<boolean> =>
    ipcRenderer.invoke("proctor:observe", payload),
});

declare global {
  interface Window {
    proctor: { observe(payload: Payload): Promise<boolean> };
  }
}
