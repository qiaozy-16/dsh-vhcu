/**
 * @file phase1-guards.test.mjs — regression guards for the V1.1 P0 fixes.
 *
 * These are deliberately source-level invariants: the dangerous behaviour
 * (global python.exe kill, undefined watermark, undefined `hwnd`) lives in
 * module-private code paths that need a live daemon to execute, so a cheap
 * static guard is what stops the pattern from silently coming back.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const hostSource = readFileSync(join(ROOT, 'lib', 'index.js'), 'utf8');
const inputSource = readFileSync(join(ROOT, 'vision', 'vhcu_daemon', 'input.py'), 'utf8');

test('P0.2 cleanup never kills every python.exe on the machine', () => {
  assert.doesNotMatch(hostSource, /taskkill['"],\s*\[[^\]]*'\/IM'/);
  assert.doesNotMatch(hostSource, /VHCU_KILL_ALL_PYTHON/);
  // P12 CON-01: no remembered-PID taskkill after the supervisor already stopped.
  assert.doesNotMatch(hostSource, /taskkill['"],\s*\['\/F', '\/T', '\/PID'/);
  assert.match(hostSource, /expectedStartedAt/);
});

test('P0.3 Node soft memory watermark is defined at 1.2 GiB', () => {
  assert.match(hostSource, /const MEMORY_SOFT_BYTES = 1\.2 \* 1024 \* 1024 \* 1024;/);
  assert.match(hostSource, /if \(rss >= MEMORY_SOFT_BYTES\) \{/);
});

test('P0.3 captureFullFrame no longer dereferences an undefined hwnd', () => {
  const start = hostSource.indexOf('async function captureFullFrame');
  const end = hostSource.indexOf('async function captureOccludedResult');
  assert.ok(start > 0 && end > start, 'captureFullFrame body was not found');
  const body = hostSource.slice(start, end);
  assert.doesNotMatch(body, /captureOccludedResult\(error, hwnd, maxw\)/);
  assert.match(body, /captureOccludedResult\(error, null, maxw\)/);
});

test('P0.3 privacy blocks fail closed instead of returning ok:true', () => {
  assert.match(hostSource, /if \(capture\.privacyBlocked && !capture\.unprotected\) \{/);
  assert.match(hostSource, /if \(privacyBlocked && !capture\?\.unprotected\) \{/);
});

test('P0.4 Win keys are sent as extended keys', () => {
  assert.match(inputSource, /VK_LWIN = 0x5B/);
  assert.match(inputSource, /VK_RWIN = 0x5C/);
  assert.match(inputSource, /flags \|= extended_key_flag\(vk\)/);
});
