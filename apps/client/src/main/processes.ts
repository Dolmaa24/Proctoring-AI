/**
 * Process-name matching. Pure, so it can be tested without Electron.
 *
 * This is a name check and nothing more. Renaming a binary defeats it
 * entirely. It exists to catch the candidate who did not think to hide
 * anything, and its output feeds a flag for human review — never an
 * automated consequence.
 *
 * Matching is by *exact basename*, not substring, and OS-vendor paths are
 * excluded. Both rules are the result of a real false positive rather than
 * caution in the abstract: the first version matched the substring
 * "parsec" to catch the game-streaming app, and on macOS that matches
 * Apple's `parsecd` and `parsec-fbf` — CoreParsec, the Spotlight
 * suggestions daemon, which runs on every Mac ever made. Every macOS
 * candidate would have been flagged for running remote-desktop software.
 *
 * The lesson generalises: a substring blacklist over process names is a
 * false-positive generator, because it is a bet that no OS vendor ever
 * ships a daemon whose name contains your keyword. That bet loses.
 */

/**
 * Executable basenames indicating screen sharing, remote control, or capture.
 *
 * Lowercase, without any `.exe` suffix — `normalise()` strips it. Entries
 * must be complete executable names, not fragments.
 */
const BLACKLIST = new Set([
  // Screen capture / broadcasting
  "obs",
  "obs64",
  "obs32",
  // Remote control
  "anydesk",
  "teamviewer",
  "teamviewerd",
  "teamviewer_service",
  "parsecd",
  "screenconnect",
  "screenconnect.clientservice",
  "logmein",
  "logmeinsystray",
  "remoting_host",
  "chrome_remote_desktop_host",
  // VNC family
  "x11vnc",
  "winvnc",
  "tvnserver",
  "vncserver",
  "vncviewer",
]);

/** Subset of the blacklist that specifically implies screen *capture*. */
const CAPTURE = new Set(["obs", "obs64", "obs32", "parsecd"]);

/**
 * Path prefixes owned by the OS vendor.
 *
 * Anything here is a system daemon, not something the candidate installed,
 * and must never be reported. This is what stops Apple's `parsecd` from
 * being mistaken for Parsec.
 */
const SYSTEM_PREFIXES = [
  "/system/",
  "/usr/libexec/",
  "/usr/sbin/",
  "/usr/lib/",
  "c:\\windows\\",
  "/library/apple/",
];

function isSystemPath(raw: string): boolean {
  const lower = raw.toLowerCase();
  return SYSTEM_PREFIXES.some((prefix) => lower.startsWith(prefix));
}

/** Basename, lowercased, with a trailing `.exe` removed. */
function normalise(raw: string): string {
  const basename = raw.trim().split(/[/\\]/).pop() ?? "";
  return basename.toLowerCase().replace(/\.exe$/, "");
}

/**
 * Return the running processes matching the blacklist.
 *
 * Takes raw entries as the OS reported them (full paths on macOS/Linux,
 * bare names on Windows) and returns them unmodified, so a reviewer sees
 * exactly what was found.
 */
export function findBlacklisted(processNames: readonly string[]): string[] {
  const hits = new Set<string>();
  for (const raw of processNames) {
    const name = raw.trim();
    if (!name || isSystemPath(name)) continue;
    if (BLACKLIST.has(normalise(name))) hits.add(name);
  }
  return [...hits].sort();
}

/** Parse `tasklist /fo csv /nh` output into process names. */
export function parseWindowsProcessList(stdout: string): string[] {
  return stdout
    .split("\n")
    .map((line) => line.split('","')[0]?.replace(/^"/, "").trim())
    .filter((name): name is string => Boolean(name));
}

/** Parse `ps -Ao comm=` output into process names. */
export function parsePosixProcessList(stdout: string): string[] {
  return stdout
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

/**
 * Whether the blacklist hits suggest an active screen share.
 *
 * Weak by construction: having OBS installed and running is not the same
 * as broadcasting, and Electron offers no portable way to ask the OS who
 * is capturing the screen. Reported as a low-weight signal for review.
 */
export function suggestsScreenShare(blacklisted: readonly string[]): boolean {
  return blacklisted.some((name) => CAPTURE.has(normalise(name)));
}

/** Exposed for tests and for auditing what the client looks for. */
export const BLACKLISTED_NAMES: readonly string[] = [...BLACKLIST].sort();
