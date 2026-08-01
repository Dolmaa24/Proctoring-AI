// GENERATED FILE — DO NOT EDIT BY HAND.
//
// Source of truth: python/proctor_protocol/events.py
// Regenerate:     python tools/generate_ts_protocol.py
//
// The Python models define the wire format. This file exists so the
// edge client cannot drift from them without CI noticing.

export const PROTOCOL_VERSION = 1;

export type ObjectLabel = "phone" | "person" | "smartwatch" | "headphones" | "book";
export type LockdownEvent = "fullscreen_exit" | "restricted_key" | "clipboard" | "context_menu" | "tab_switch";
export type LifecyclePhase = "session_start" | "identity_verified" | "exam_start" | "exam_end" | "session_end";

export interface BoundingBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface GazeSignal {
  type: "signal.gaze";
  yaw_deg: number;
  pitch_deg: number;
  on_screen: boolean;
  confidence: number;
}

export interface HeadPoseSignal {
  type: "signal.head_pose";
  yaw_deg: number;
  pitch_deg: number;
  roll_deg: number;
  confidence: number;
}

export interface FaceSignal {
  type: "signal.face";
  face_count: number;
  primary_bbox?: BoundingBox | null;
  confidence: number;
}

export interface ObjectSignal {
  type: "signal.object";
  label: ObjectLabel;
  confidence: number;
  bbox?: BoundingBox | null;
}

export interface LivenessSignal {
  type: "signal.liveness";
  score: number;
  confidence: number;
}

export interface AudioSignal {
  type: "signal.audio";
  speech_active: boolean;
  energy_db: number;
  confidence: number;
}

export interface EnvironmentSignal {
  type: "signal.environment";
  window_focused: boolean;
  monitor_count: number;
  blacklisted_processes?: string[];
  screen_share_active?: boolean;
  confidence?: number;
}

export interface FrameQualitySignal {
  type: "signal.frame_quality";
  sharpness: number;
  brightness: number;
  face_covered?: boolean;
  confidence?: number;
}

export interface LockdownSignal {
  type: "signal.lockdown";
  event: LockdownEvent;
  strike: number;
  allowance: number;
  detail?: string | null;
  confidence?: number;
}

export interface Heartbeat {
  type: "heartbeat";
  frames_processed: number;
  edge_fps: number;
  dropped_frames?: number;
}

export interface Lifecycle {
  type: "lifecycle";
  phase: LifecyclePhase;
  detail?: string | null;
}

export interface Attestation {
  type: "attestation";
  client_build: string;
  model_hashes: Record<string, string>;
  platform: string;
}

export type Payload =
  | GazeSignal
  | HeadPoseSignal
  | FaceSignal
  | ObjectSignal
  | LivenessSignal
  | AudioSignal
  | EnvironmentSignal
  | FrameQualitySignal
  | LockdownSignal
  | Heartbeat
  | Lifecycle
  | Attestation;

export interface Envelope {
  v?: number;
  session_id: string;
  seq: number;
  ts_client_ms: number;
  ts_monotonic_ms: number;
  payload: GazeSignal | HeadPoseSignal | FaceSignal | ObjectSignal | LivenessSignal | AudioSignal | EnvironmentSignal | FrameQualitySignal | LockdownSignal | Heartbeat | Lifecycle | Attestation;
}
