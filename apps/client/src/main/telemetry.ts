/**
 * Signed telemetry transport. Runs in the Electron main process only.
 *
 * The session key lives here and never reaches the renderer. That is a
 * deliberate boundary: the renderer hosts the models and the camera feed,
 * and anyone who opens DevTools has a JavaScript console inside it. If the
 * key were there, forging telemetry would be a one-line paste. Keeping it
 * in main means an attacker has to modify the packaged binary instead.
 *
 * This does not make the client trustworthy — see ARCHITECTURE.md § 3. It
 * removes the cheapest attack, which is the job of every layer here.
 */

import { setTimeout as delay } from "node:timers/promises";
import WebSocket from "ws";

import type { Envelope, Payload } from "../protocol/events.generated.ts";
import { signEnvelope } from "../protocol/signing.ts";

export interface Enrolment {
  session_id: string;
  session_key_b64: string;
  telemetry_url: string;
  protocol_version: number;
}

const MAX_QUEUE = 2_000;
const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 15_000;

export class TelemetryClient {
  #socket: WebSocket | null = null;
  #key: CryptoKey | null = null;
  #seq = 0;
  #startedAt = 0;
  #queue: string[] = [];
  #closed = false;
  #dropped = 0;

  constructor(
    private readonly baseUrl: string,
    private readonly enrolment: Enrolment,
    private readonly importKey: (b64: string) => Promise<CryptoKey>,
  ) {}

  get droppedFrames(): number {
    return this.#dropped;
  }

  get connected(): boolean {
    return this.#socket?.readyState === WebSocket.OPEN;
  }

  async start(): Promise<void> {
    this.#key = await this.importKey(this.enrolment.session_key_b64);
    // Monotonic from here. `performance.now()` is immune to wall-clock
    // changes, which matters because the gateway rejects a monotonic
    // counter that moves backwards — using Date.now() would make an
    // ordinary NTP correction look like tampering.
    this.#startedAt = performance.now();
    void this.#connectLoop();
  }

  /**
   * Queue one observation for transmission.
   *
   * Sequence numbers are assigned here, at enqueue time, not at send time.
   * The gateway treats a gap as evidence of suppression, so a frame that
   * is queued during a network drop must keep its place in the sequence
   * and be delivered late rather than skipped.
   */
  async emit(payload: Payload): Promise<void> {
    if (this.#closed || !this.#key) return;

    const monotonic = Math.round(performance.now() - this.#startedAt);
    const envelope: Envelope = {
      v: this.enrolment.protocol_version,
      session_id: this.enrolment.session_id,
      seq: this.#seq++,
      ts_client_ms: Date.now(),
      ts_monotonic_ms: monotonic,
      payload,
    };

    const frame = await signEnvelope(envelope, this.#key);

    if (this.connected) {
      this.#socket!.send(frame);
      return;
    }

    this.#queue.push(frame);
    if (this.#queue.length > MAX_QUEUE) {
      // Bounded, but note what dropping costs: the gateway will see the
      // gap and flag it. That is the correct outcome — a client that
      // cannot deliver its telemetry should look like one, not silently
      // renumber to hide the loss.
      this.#queue.shift();
      this.#dropped += 1;
    }
  }

  async close(): Promise<void> {
    this.#closed = true;
    await this.#flush();
    this.#socket?.close(1000, "session ended");
    this.#socket = null;
  }

  async #connectLoop(): Promise<void> {
    let attempt = 0;
    while (!this.#closed) {
      try {
        await this.#connectOnce();
        attempt = 0;
      } catch {
        // Swallowed on purpose: a transport error is expected on a flaky
        // network and must not surface to the candidate as a crash.
      }
      if (this.#closed) return;
      const backoff = Math.min(RECONNECT_BASE_MS * 2 ** attempt++, RECONNECT_MAX_MS);
      await delay(backoff + Math.random() * 250);
    }
  }

  #connectOnce(): Promise<void> {
    const url =
      this.baseUrl.replace(/^http/, "ws") + this.enrolment.telemetry_url;

    return new Promise((resolve, reject) => {
      const socket = new WebSocket(url);
      this.#socket = socket;

      socket.on("open", () => {
        void this.#flush();
      });
      socket.on("close", () => {
        this.#socket = null;
        resolve();
      });
      socket.on("error", (error) => {
        this.#socket = null;
        reject(error);
      });
    });
  }

  async #flush(): Promise<void> {
    while (this.#queue.length && this.connected) {
      this.#socket!.send(this.#queue.shift()!);
    }
  }
}
