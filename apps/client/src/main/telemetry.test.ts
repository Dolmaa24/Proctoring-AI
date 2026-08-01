/**
 * Transport ordering.
 *
 * The bug these cover was not hypothetical and was not caught by any
 * unit test: `emit` handed out sequence numbers synchronously and then
 * awaited signing before sending, so two overlapping calls could reach
 * the socket in the opposite order. The gateway reads a frame arriving
 * after a higher sequence number as a replay attempt, so an honest
 * client under load raised `stream_replay` and `stream_sequence_gap`
 * against itself. It only showed up once the end-to-end run reached
 * real throughput.
 *
 * Signing is injected so these tests can make it resolve out of order on
 * purpose. That is the whole point: with a well-behaved signer the race
 * is invisible, which is exactly why it survived to production.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { TelemetryClient, type Enrolment } from "./telemetry.ts";
import type { Envelope } from "../protocol/events.generated.ts";

const ENROLMENT: Enrolment = {
  session_id: "sess-test",
  session_key_b64: "AAAA",
  telemetry_url: "/v1/sessions/sess-test/telemetry",
  protocol_version: 1,
};

const KEY = {} as CryptoKey;
const importKey = async () => KEY;

/**
 * Build a started client and guarantee it is shut down afterwards.
 *
 * `start` kicks off the reconnect loop, which retries with backoff
 * forever against a gateway that is not there. Left running it keeps the
 * event loop alive and the test process never exits, so cleanup here is
 * not tidiness — it is the difference between a suite that terminates
 * and one that hangs.
 */
async function withClient(
  sign: (envelope: Envelope, key: CryptoKey) => Promise<string>,
  body: (client: TelemetryClient) => Promise<void>,
): Promise<void> {
  const client = new TelemetryClient("http://127.0.0.1:1", ENROLMENT, importKey, sign);
  await client.start();
  try {
    await body(client);
  } finally {
    await client.close();
  }
}

/** Sequence numbers in the order they actually reached dispatch. */
function sentSequence(client: TelemetryClient): number[] {
  // Nothing is connected in these tests, so every frame lands in the
  // offline queue. That is the same `#dispatch` call the socket path
  // takes — the branch inside it picks the destination, but the ordering
  // under test is decided before that branch is reached.
  return client.pending.map((frame) => JSON.parse(frame).seq as number);
}

/**
 * A signer whose completion order is the reverse of its call order: the
 * first envelope in takes the longest to sign. Models the real failure,
 * where signing durations vary and a later frame can finish first.
 */
function reversedSigner(total: number) {
  let calls = 0;
  return async (envelope: Envelope): Promise<string> => {
    const position = calls++;
    // Earlier calls wait longer, so completion order inverts call order.
    await new Promise((resolve) => setTimeout(resolve, (total - position) * 5));
    return JSON.stringify({ seq: envelope.seq });
  };
}

test("frames reach the socket in sequence order even when signing finishes out of order", async () => {
  await withClient(reversedSigner(6), async (client) => {
    // Not awaited individually — this is the concurrent pattern the
    // renderer actually uses, firing several observations per tick.
    await Promise.all(
      Array.from({ length: 6 }, () => client.emit({ type: "lifecycle", phase: "session_start" })),
    );

    assert.deepEqual(
      sentSequence(client),
      [0, 1, 2, 3, 4, 5],
      "dispatch order must follow sequence order, not signing completion order",
    );
  });
});

test("a frame that fails to sign does not wedge the frames behind it", async () => {
  let calls = 0;
  const flaky = async (envelope: Envelope): Promise<string> => {
    if (calls++ === 1) throw new Error("signing failed");
    return JSON.stringify({ seq: envelope.seq });
  };

  await withClient(flaky, async (client) => {
    const results = await Promise.allSettled(
      Array.from({ length: 4 }, () => client.emit({ type: "lifecycle", phase: "session_start" })),
    );

    assert.equal(results[1]?.status, "rejected", "the failing frame should surface its error");
    // The gap at seq 1 is deliberate and correct: that frame genuinely
    // was never sent, and the gateway is supposed to notice. What must
    // not happen is the failure blocking everything queued behind it.
    assert.deepEqual(sentSequence(client), [0, 2, 3]);
  });
});

test("close waits for frames still being signed", async () => {
  await withClient(reversedSigner(3), async (client) => {
    // Deliberately not awaited: these are still mid-signature when close
    // is called, which is what happens when the app quits mid-tick.
    void client.emit({ type: "lifecycle", phase: "session_start" });
    void client.emit({ type: "lifecycle", phase: "session_start" });
    void client.emit({ type: "lifecycle", phase: "session_start" });

    await client.close();

    assert.deepEqual(
      sentSequence(client),
      [0, 1, 2],
      "in-flight frames must be dispatched before close, or shutdown fabricates a gap",
    );
  });
});
