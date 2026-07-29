/**
 * Client half of the telemetry authentication scheme.
 *
 * Mirrors python/proctor_protocol/signing.py. A signed frame is:
 *
 *     {"b": "<base64url of the envelope JSON bytes>", "s": "<hex HMAC-SHA256>"}
 *
 * The signature covers the base64 payload exactly as transmitted, and the
 * server verifies against the bytes it received rather than re-serialising
 * the parsed object.
 *
 * That indirection is why this file does not need a canonical-JSON
 * implementation. Signing a re-serialisation across Python and JavaScript
 * is a trap: `JSON.stringify` and Python's `json.dumps` do not agree on
 * every float, and every signal in this protocol is float-valued. Signing
 * opaque bytes sidesteps the whole class of bug, and the conformance test
 * in tools/conformance.ts proves the two halves actually agree.
 */

import type { Envelope } from "./events.generated.ts";

const encoder = new TextEncoder();

/** Base64url without padding, matching Python's `_b64encode`. */
export function base64UrlEncode(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function base64UrlDecode(text: string): Uint8Array {
  const padded = text.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(padded + "=".repeat((4 - (padded.length % 4)) % 4));
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function toHex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * Import a session key delivered by the gateway at enrolment.
 *
 * The key arrives base64-standard (not url-safe) in the enrolment response,
 * matching what the server emits.
 */
export async function importSessionKey(sessionKeyB64: string): Promise<CryptoKey> {
  const binary = atob(sessionKeyB64);
  const raw = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  return crypto.subtle.importKey(
    "raw",
    raw,
    { name: "HMAC", hash: "SHA-256" },
    false, // not extractable: marginal, but no reason to hand it back out
    ["sign"],
  );
}

/** Serialise and sign an envelope, returning the frame to transmit. */
export async function signEnvelope(envelope: Envelope, key: CryptoKey): Promise<string> {
  const body = encoder.encode(JSON.stringify(envelope));
  const b64 = base64UrlEncode(body);
  const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(b64));
  return JSON.stringify({ b: b64, s: toHex(signature) });
}
