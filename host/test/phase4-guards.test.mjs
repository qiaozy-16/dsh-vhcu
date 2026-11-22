/**
 * @file phase4-guards.test.mjs — regression guards for the V1.1 phase-4 fixes.
 *
 * Behavioural coverage: ReleaseAllTest / UiaWorker timeout test / UiaPrivacyGateTest
 * / Chromium class rejection in vision/tests, and frames/guards tests in host.
 * These source guards pin the remaining wiring (startup/shutdown calls, counter
 * migration, UIA entry gates, unhealthy state).
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const hostSource = readFileSync(join(ROOT, 'lib', 'index.js'), 'utf8');
const cliSource = readFileSync(join(ROOT, 'vision', 'vhcu_daemon', 'cli.py'), 'utf8');
const inputSource = readFileSync(join(ROOT, 'vision', 'vhcu_daemon', 'input.py'), 'utf8');
const uiaSource = readFileSync(join(ROOT, 'vision', 'vhcu_daemon', 'uia_worker.py'), 'utf8');
const cursorSource = readFileSync(join(ROOT, 'vision', 'vhcu_daemon', 'virtual_cursor.py'), 'utf8');

test('P4.1 release_all exists and runs at startup (before READY) and shutdown', () => {
  assert.match(inputSource, /def release_all\(self\) -> dict:/);
  assert.match(inputSource, /RELEASE_VKS = \(/);
  assert.equal((cliSource.match(/self\.input\.release_all\(\)/g) ?? []).length, 2);
  const startup = cliSource.indexOf('self.input.release_all()');
  const ready = cliSource.indexOf('self.proto.ready(');
  assert.ok(startup > 0 && ready > startup, 'release_all must precede READY');
  const shutdown = cliSource.indexOf('def shutdown(self, reason: str)');
  assert.ok(cliSource.indexOf('self.input.release_all()', shutdown) > shutdown);
});

test('P4.2 injection state is a counter, never a saved previous boolean', () => {
  assert.match(hostSource, /let activeInjections = 0;/);
  assert.doesNotMatch(hostSource, /\bisAiInjecting\b/);
  assert.doesNotMatch(hostSource, /previousAiInjecting/);
  assert.equal((hostSource.match(/activeInjections \+= 1;/g) ?? []).length, 2);
  assert.equal((hostSource.match(/activeInjections = Math\.max\(0, activeInjections - 1\);/g) ?? []).length, 2);
  assert.match(hostSource, /if \(activeInjections === 0\) aiInjectionGraceUntil = Date\.now\(\) \+ AI_INJECTION_GRACE_MS;/);
  assert.equal((hostSource.match(/shouldCaptureInput\(activeInjections > 0,/g) ?? []).length, 3);
});

test('P4.3 UIA find/click are gated by privacy on both daemon and Node', () => {
  assert.match(
    cliSource,
    /def _assert_uia_privacy\(self, hwnd: int, allow_unprotected: bool = False\)/,
  );
  assert.equal((cliSource.match(/self\._assert_uia_privacy\(/g) ?? []).length, 4);
  assert.match(cliSource, /E_PRIVACY_BLOCKED/);
  // Node findElements must return before ensureDaemonReady when privacy is on.
  const findStart = hostSource.indexOf('async function findElements');
  const ensure = hostSource.indexOf('await ensureDaemonReady()', findStart);
  const gate = hostSource.indexOf('inputAllowedUnderPrivacy(state.privacy?.blocked', findStart);
  assert.ok(gate > findStart && ensure > gate, 'findElements privacy gate must run before daemon start');
  // D5: the gate reports the same privacy taxonomy as the daemon-side UIA gate.
  const gateBlock = hostSource.slice(gate, gate + 320);
  assert.match(gateBlock, /code: 'E_PRIVACY_BLOCKED'/);
  const inputGate = hostSource.indexOf('隐私保护已触发：AI 已暂停，禁止任何键鼠注入');
  assert.ok(inputGate > 0, 'input privacy gate message must exist');
  assert.match(hostSource.slice(inputGate, inputGate + 220), /code: 'E_PRIVACY_BLOCKED'/);
});

test('P4.4 timed-out UIA worker is marked unhealthy and refuses new work', () => {
  assert.match(uiaSource, /self\._unhealthy = False/);
  assert.match(uiaSource, /self\._unhealthy = True/);
  assert.equal((uiaSource.match(/UIA worker became unhealthy after a request timeout/g) ?? []).length, 3);
  assert.match(uiaSource, /"unhealthy": bool\(self\._unhealthy\)/);
});

test('P4.5 PostMessage blocklist keeps unverified shells, allows Chromium', () => {
  assert.match(cursorSource, /UNSAFE_POSTMESSAGE_CLASSES = frozenset\(\{/);
  // V1.1 relaxation: Chromium main windows are no longer blocked.
  assert.doesNotMatch(cursorSource, /"Chrome_WidgetWin_[01]"/);
  assert.match(cursorSource, /"CefBrowserWindow"/);
  assert.match(cursorSource, /"ApplicationFrameWindow"/);
  assert.match(cursorSource, /def is_postmessage_unsupported_class/);
  assert.match(cursorSource, /if is_postmessage_unsupported_class\(name\):/);
  assert.match(cursorSource, /E_VIRTUAL_CURSOR_UNSUPPORTED/);
});
