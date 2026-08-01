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

  /**
   * Tail of the dispatch chain. Every frame waits on the previous one
   * before it reaches the socket — see `emit`.
   */
  #tail: Promise<void> = Promise.resolve();

  // Written out as fields rather than constructor parameter properties.
  // Node's --experimental-strip-types refuses parameter properties, so
  // that shorthand made this whole module unloadable by the test runner —
  // which is a large part of why the ordering bug below reached
  // production with no unit test in its way.
  readonly #baseUrl: string;
  readonly #enrolment: Enrolment;
  readonly #importKey: (b64: string) => Promise<CryptoKey>;
  readonly #sign: (envelope: Envelope, key: CryptoKey) => Promise<string>;

  constructor(
    baseUrl: string,
    enrolment: Enrolment,
    importKey: (b64: string) => Promise<CryptoKey>,
    /**
     * Injected for the same reason `importKey` is: the ordering guarantee
     * in `emit` is only testable if a test can make signing resolve out of
     * order on purpose, which is exactly the case that broke in
     * production.
     */
    sign: (envelope: Envelope, key: CryptoKey) => Promise<string> = signEnvelope,
  ) {
    this.#baseUrl = baseUrl;
    this.#enrolment = enrolment;
    this.#importKey = importKey;
    this.#sign = sign;
  }

  get droppedFrames(): number {
    return this.#dropped;
  }

  /**
   * Frames signed but not yet on the wire, oldest first.
   *
   * Diagnostic, like `droppedFrames`: a queue that only grows is the
   * visible symptom of a socket that reconnects but never drains.
   */
  get pending(): readonly string[] {
    return this.#queue;
  }

  get connected(): boolean {
    return this.#socket?.readyState === WebSocket.OPEN;
  }

  async start(): Promise<void> {
    this.#key = await this.#importKey(this.#enrolment.session_key_b64);
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
      v: this.#enrolment.protocol_version,
      session_id: this.#enrolment.session_id,
      seq: this.#seq++,
      ts_client_ms: Date.now(),
      ts_monotonic_ms: monotonic,
      payload,
    };

    // Sequence numbers are handed out synchronously above, but signing is
    // async — so without this chain two overlapping `emit` calls race, and
    // whichever finishes signing first reaches the socket first. The
    // gateway treats a frame arriving after a higher sequence number as a
    // replay attempt, which meant an honest client under load flagged
    // itself for the one attack this counter exists to detect.
    //
    // Signing still overlaps; only dispatch is ordered. `catch` keeps one
    // failed frame from wedging every frame queued behind it.
    const key = this.#key;
    const previous = this.#tail;
    const mine = (async () => {
      const frame = await this.#sign(envelope, key);
      await previous;
      this.#dispatch(frame);
    })();
    this.#tail = mine.catch(() => undefined);
    return mine;
  }

  #dispatch(frame: string): void {
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
    // Drain before marking closed: frames already mid-signature still have
    // sequence numbers the gateway is expecting, and dropping them here
    // would manufacture the exact gap this class works to avoid — on the
    // shutdown path, where `session_end` makes it look deliberate.
    await this.#tail;
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
      this.#baseUrl.replace(/^http/, "ws") + this.#enrolment.telemetry_url;

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
