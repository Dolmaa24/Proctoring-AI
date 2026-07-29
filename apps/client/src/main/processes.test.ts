/**
 * Run: node --experimental-strip-types --test apps/client/src/main/processes.test.ts
 *
 * The false-positive tests matter more than the detection tests. A missed
 * screen-sharing app is a gap; a false hit is a real person accused of
 * cheating because of what they happen to have installed.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  findBlacklisted,
  parsePosixProcessList,
  parseWindowsProcessList,
  suggestsScreenShare,
} from "./processes.ts";

test("detects screen sharing and remote control tools", () => {
  const running = ["Finder", "obs64", "Google Chrome", "AnyDesk", "node"];
  assert.deepEqual(findBlacklisted(running), ["AnyDesk", "obs64"]);
});

test("matches case-insensitively and ignores the directory path", () => {
  const running = ["/Applications/AnyDesk.app/Contents/MacOS/AnyDesk"];
  assert.equal(findBlacklisted(running).length, 1);
});

test("does not flag ordinary software with colliding names", () => {
  // Each of these has bitten a real blacklist. "obs" alone matches
  // "observer" and Adobe's "obsidian"; "vnc" alone is fine but "team"
  // matches Microsoft Teams, which is not remote-control software.
  const innocent = [
    "Obsidian",
    "observer-daemon",
    "Microsoft Teams",
    "teams-helper",
    "com.apple.WebKit.WebContent",
    "Slack",
    "zoom.us",
  ];
  assert.deepEqual(findBlacklisted(innocent), []);
});

test("ignores blank and whitespace-only entries", () => {
  assert.deepEqual(findBlacklisted(["", "   ", "\t"]), []);
});

test("deduplicates repeated matches of the same process", () => {
  const running = ["obs64", "obs64", "obs64"];
  assert.deepEqual(findBlacklisted(running), ["obs64"]);
});

test("parses windows tasklist csv output", () => {
  const stdout = [
    '"System Idle Process","0","Services","0","8 K"',
    '"AnyDesk.exe","4242","Console","1","24,132 K"',
    "",
  ].join("\n");
  const names = parseWindowsProcessList(stdout);
  assert.deepEqual(names, ["System Idle Process", "AnyDesk.exe"]);
  assert.deepEqual(findBlacklisted(names), ["AnyDesk.exe"]);
});

test("parses posix ps output", () => {
  const stdout = "/usr/sbin/cfprefsd\n/Applications/obs64\n\n  \n";
  const names = parsePosixProcessList(stdout);
  assert.deepEqual(names, ["/usr/sbin/cfprefsd", "/Applications/obs64"]);
});

test("screen share inference stays narrow", () => {
  assert.equal(suggestsScreenShare(["obs64"]), true);
  assert.equal(suggestsScreenShare(["AnyDesk"]), false, "remote control is not capture");
  assert.equal(suggestsScreenShare([]), false);
});
