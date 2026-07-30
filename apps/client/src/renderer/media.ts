/**
 * The candidate/proctor video call: join a LiveKit room and publish.
 *
 * This is deliberately separate from the MediaPipe pipeline in index.ts,
 * and deliberately not load-bearing for it. Edge inference produces the
 * signed observations the fusion engine actually rules on; this module
 * publishes the live call a proctor can watch. If the media plane is
 * disabled, unreachable, or fails to connect, monitoring continues
 * unaffected — see ARCHITECTURE.md § 5.6.
 */

import { Room, Track } from "livekit-client";

/**
 * Join this candidate's room and publish an already-open camera track.
 *
 * Takes a `MediaStreamTrack` rather than opening its own camera: index.ts
 * already holds the track MediaPipe is reading from, and requesting the
 * device a second time would either prompt twice or contend for it,
 * depending on the OS. Publishing the same track LiveKit and MediaPipe
 * both read from costs nothing extra — a `MediaStreamTrack` has no
 * concept of a single consumer.
 *
 * Returns null without throwing if the media plane is disabled server-
 * side or the room could not be joined; the caller treats both the same
 * way, as "no call this session," never as a reason to stop monitoring.
 */
export async function joinMediaRoom(cameraTrack: MediaStreamTrack): Promise<Room | null> {
  const join = await window.proctor.getMediaJoin();
  if (!join) return null;

  const room = new Room();
  try {
    await room.connect(join.url, join.token);
  } catch (error) {
    console.error("could not join the proctor video call:", error);
    return null;
  }

  await room.localParticipant.publishTrack(cameraTrack, { source: Track.Source.Camera });

  // Microphone access is requested here — only once a room join actually
  // succeeded — and nowhere earlier. MediaPipe's own pipeline never reads
  // audio, so asking before knowing the call is even happening would be a
  // permission prompt with no purpose behind it yet.
  try {
    const micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const audioTrack = micStream.getAudioTracks()[0];
    if (audioTrack) {
      await room.localParticipant.publishTrack(audioTrack, { source: Track.Source.Microphone });
    }
  } catch (error) {
    // Camera-only is a degraded but still useful call; do not tear down
    // the room over a declined or unavailable microphone.
    console.error("microphone unavailable for the proctor call:", error);
  }

  return room;
}
