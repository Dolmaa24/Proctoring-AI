/**
 * OS-level observation. The reason this client is a desktop app.
 *
 * Everything here is unavailable to a web page: how many displays are
 * attached, whether the exam window actually has focus, what else is
 * running. A browser tab can be told it lost focus but cannot see a second
 * monitor or a running copy of AnyDesk.
 *
 * Note what this layer is *not*, because it is routinely oversold: it
 * observes, it does not prevent. Real OS lockdown — killing processes,
 * blocking input — needs privileges we should not ask exam candidates to
 * grant, and invites an arms race on someone else's computer. Detection
 * here is also name-based, so renaming a binary defeats it; see
 * processes.ts.
 */

import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { BrowserWindow, powerMonitor, screen } from "electron";

import type { EnvironmentSignal } from "../protocol/events.generated.ts";
import {
  findBlacklisted,
  parsePosixProcessList,
  parseWindowsProcessList,
  suggestsScreenShare,
} from "./processes.ts";

const run = promisify(execFile);
const PROCESS_SCAN_TIMEOUT_MS = 4_000;

/** List running process names. Returns null when the scan could not run. */
async function listProcesses(): Promise<string[] | null> {
  try {
    if (process.platform === "win32") {
      const { stdout } = await run("tasklist", ["/fo", "csv", "/nh"], {
        timeout: PROCESS_SCAN_TIMEOUT_MS,
        windowsHide: true,
      });
      return parseWindowsProcessList(stdout);
    }
    const { stdout } = await run("ps", ["-Ao", "comm="], {
      timeout: PROCESS_SCAN_TIMEOUT_MS,
    });
    return parsePosixProcessList(stdout);
  } catch {
    // Sandboxing, permissions, or a missing binary. Distinguishing "nothing
    // suspicious is running" from "we could not look" matters, and the
    // caller reports the difference as confidence rather than swallowing it.
    return null;
  }
}

export interface EnvironmentReading {
  signal: EnvironmentSignal;
  scanFailed: boolean;
}

export async function readEnvironment(
  window: BrowserWindow | null,
): Promise<EnvironmentReading> {
  const processNames = await listProcesses();
  const scanFailed = processNames === null;
  const blacklisted = processNames ? findBlacklisted(processNames) : [];

  return {
    scanFailed,
    signal: {
      type: "signal.environment",
      // False when hidden, minimised, or behind another app — all equally
      // interesting for proctoring purposes.
      window_focused: Boolean(window && !window.isDestroyed() && window.isFocused()),
      monitor_count: screen.getAllDisplays().length,
      blacklisted_processes: blacklisted,
      screen_share_active: suggestsScreenShare(blacklisted),
      // A failed scan must not read as a clean machine. Low confidence
      // makes the fusion engine's `min_confidence` gate drop the sample
      // rather than treat it as evidence of a tidy environment.
      confidence: scanFailed ? 0.2 : 1.0,
    },
  };
}

/** Seconds since the last input event, per the OS. */
export function idleSeconds(): number {
  return powerMonitor.getSystemIdleTime();
}
