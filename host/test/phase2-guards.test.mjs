/**
 * @file phase2-guards.test.mjs — regression guards for the V1.1 P1 fixes.
 *
 * Behavioural coverage lives in guards.test.mjs (grace window), frames.test.mjs
 * (seq forwarding), vision/tests/test_capture.py (overlay guard + seq history)
 * and integration.test.mjs (live frame_by_seq). These source guards pin the
 * wiring that has no cheap unit seam: the vision.stopped state transition and
 * the capture_guard call sites.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const hostSource = readFileSync(join(ROOT, 'lib', 'index.js'), 'utf8');
const runnerSource = readFileSync(join(ROOT, 'vision', 'vhcu_daemon', 'runner.py'), 'utf8');
const cliSource = readFileSync(join(ROOT, 'vision', 'vhcu_daemon', 'cli.py'), 'utf8');
const overlaySource = readFileSync(join(ROOT, 'vision', 'vhcu_daemon', 'cursor_overlay.py'), 'utf8');

test('P1 8.7 full-frame URL and metadata are bound to the captured seq', () => {
  const start = hostSource.indexOf('async function captureFullFrame');
  const end = hostSource.indexOf('async function captureOccludedResult');
  assert.ok(start > 0 && end > start, 'captureFullFrame body was not found');
  const body = hostSource.slice(start, end);
  assert.match(body, /const frameSeq = Number\.isInteger\(capturedSeq\)/);
  assert.match(body, /seq: frameSeq/);
  assert.match(body, /seq: frameSeq,\s*\n\s*timeoutMs: 3000/);
});

test('P1 8.7 daemon retains on-demand frames by seq and serves them', () => {
  assert.match(runnerSource, /MAX_RECENT_FRAMES = 4/);
  assert.match(runnerSource, /self\._recent_frames: dict\[int, Frame\]/);
  assert.match(runnerSource, /frame = self\._recent_frames\.get\(seq\)/);
  assert.match(runnerSource, /if frame is None:\s*\n\s*frame = self\._recent_frames\.get\(seq\)/);
});

test('P1 8.8 model-facing captures hide and restore the overlay', () => {
  assert.match(runnerSource, /def capture_guard\(self\)/);
  assert.match(runnerSource, /overlay\.hide_sync\(\)/);
  assert.match(runnerSource, /overlay\.restore_after_capture\(\)/);
  // capture_now + capture_window are guarded; the fingerprint path too.
  assert.equal((runnerSource.match(/with self\.capture_guard\(\):/g) ?? []).length, 2);
  assert.equal((cliSource.match(/with self\.runner\.capture_guard\(\):/g) ?? []).length, 1);
  // The 8-15fps diff tick stays unguarded on purpose (would strobe the cursor).
  assert.match(runnerSource, /frame = self\._source\.grab\(\)/);
  assert.match(overlaySource, /def hide_sync\(self, timeout: float = 0\.3\)/);
  assert.match(overlaySource, /self\._hide_ack\.set\(\)/);
});

test('P1 8.10 injection grace window arms after every AI input action', () => {
  assert.match(hostSource, /aiInjectionGraceUntil = Date\.now\(\) \+ AI_INJECTION_GRACE_MS/);
  assert.match(hostSource, /inInjectionGraceWindow\(Date\.now\(\), aiInjectionGraceUntil\)/);
  assert.match(hostSource, /hook\.on\('keyup', onActivity\)/);
});

test('P1 8.12 unexpected vision.stopped turns Node state into error', () => {
  const start = hostSource.indexOf("frame.kind === 'vision.stopped'");
  const end = hostSource.indexOf("frame.kind === 'hear.audio'");
  assert.ok(start > 0 && end > start, 'vision.stopped handler was not found');
  const body = hostSource.slice(start, end);
  assert.match(body, /if \(state\.phase === 'running'\)/);
  assert.match(body, /state\.phase = 'error'/);
  assert.match(body, /state\.running = false/);
  assert.match(body, /pushStatus\(\)/);
  assert.match(body, /stopPresenceWatcher\(\)/);
});
