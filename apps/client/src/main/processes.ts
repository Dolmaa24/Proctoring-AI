/**
 * Process-name matching. Pure, so it can be tested without Electron.
 *
 * This is a name check and nothing more. Renaming a binary defeats it
 * entirely. It exists to catch the candidate who did not think to hide
 * anything, and its output feeds a flag for human review — never an
 * automated consequence.
 */

/**
 * Substrings indicating screen sharing, remote control, or capture.
 *
 * Deliberately short. Every entry is a potential false positive, and a
 * false positive here accuses someone of cheating for having ordinary
 * software installed. Entries must be specific enough not to collide with
 * unrelated binaries — see the tests for the collisions this list has
 * already had to avoid.
 */
export const BLACKLIST = [
  "obs64",
  "obs-studio",
  "anydesk",
  "teamviewer",
  "realvnc",
  "tightvnc",
  "chrome remote desktop",
  "parsec",
  "screenconnect",
  "logmein",
] as const;

/**
 * Return the running processes matching the blacklist.
 *
 * Matching is case-insensitive on the basename, so a full path does not
 * change the result and `/Applications/AnyDesk.app/.../AnyDesk` matches.
 */
export function findBlacklisted(processNames: readonly string[]): string[] {
  const hits = new Set<string>();
  for (const raw of processNames) {
    const name = raw.trim();
    if (!name) continue;
    const basename = name.split("/").pop()!.toLowerCase();
    for (const needle of BLACKLIST) {
      if (basename.includes(needle)) {
        hits.add(name);
        break;
      }
    }
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
  return blacklisted.some((name) => {
    const lower = name.toLowerCase();
    return lower.includes("obs") || lower.includes("parsec");
  });
}
