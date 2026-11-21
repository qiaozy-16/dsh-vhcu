/**
 * @file phase5-guards.test.mjs — regression guards for V1.1 phase-5 changes.
 *
 * Covers: uia injection mode + per-call preferred_method, virtual-mode soft-pause
 * bypass, the four Chinese setting labels, Chromium PostMessage relaxation, and
 * the restored plugin-list icon. Behavioural coverage for the UIA point path
 * lives in vision/tests/test_uia_worker.py.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { mergeSettings } from '../lib/settings.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const hostSource = readFileSync(join(ROOT, 'lib', 'index.js'), 'utf8');
const clientSource = readFileSync(join(ROOT, 'lib', 'client.js'), 'utf8');
const cliSource = readFileSync(join(ROOT, 'vision', 'vhcu_daemon', 'cli.py'), 'utf8');
const uiaSource = readFileSync(join(ROOT, 'vision', 'vhcu_daemon', 'uia_worker.py'), 'utf8');
const contractSource = readFileSync(join(ROOT, 'contract', 'lib', 'contract.js'), 'utf8');
const cmdSchema = readFileSync(join(ROOT, 'contract', 'schema', 'v1', 'cmd.json'), 'utf8');

test('P5.0 settings: uia is a valid mode and invalid values still fall back', () => {
  assert.equal(mergeSettings({}, { cursorInjectionMode: 'uia' }).cursorInjectionMode, 'uia');
  assert.equal(mergeSettings({}, { cursorInjectionMode: 'physical' }).cursorInjectionMode, 'physical');
  assert.equal(mergeSettings({}, { cursorInjectionMode: 'teleport' }).cursorInjectionMode, 'hybrid');
});

test('P5.1 virtual mode bypasses the soft-pause takeover', () => {
  const fn = hostSource.indexOf('function triggerUserTakeover()');
  const guard = hostSource.indexOf("if (currentCursorInjectionMode() === 'virtual') return;", fn);
  const pause = hostSource.indexOf('shouldPauseForUserTakeover(', fn);
  assert.ok(guard > fn && pause > guard, 'virtual guard must precede soft-pause evaluation');
});

test('P5.2 uia mode + hybrid preferred_method are wired', () => {
  assert.match(hostSource, /\['physical', 'virtual', 'uia', 'hybrid'\]/);
  assert.match(hostSource, /function currentDaemonInjectionMode\(\)/);
  assert.match(hostSource, /return mode === 'uia' \? 'physical' : mode;/);
  assert.match(hostSource, /function resolveInjectionMethod\(preferredMethod\)/);
  assert.match(hostSource, /preferred_method: \{/);
  assert.match(hostSource, /enum: \['physical', 'virtual', 'uia'\]/);
  assert.match(hostSource, /'uia\.click_point'/);
  assert.match(hostSource, /if \(method === 'uia'\)/);
});

test('P5.3 setting labels match the four requested plain-language strings', () => {
  for (const label of [
    '智能混合（物理/虚拟/UIA语义）',
    '强制物理光标',
    '强制虚拟光标',
    '强制UIA 语义（底层精准控制）',
  ]) {
    assert.ok(clientSource.includes(label), `missing label: ${label}`);
  }
  assert.match(clientSource, /h\('option', \{ value: 'uia' \}/);
});

test('P5.5 uia.click_point is contracted and served by daemon + UIA worker', () => {
  assert.match(contractSource, /'uia\.click_point'/);
  assert.match(cmdSchema, /"uia\.click_point"/);
  assert.match(cliSource, /if op == "uia\.click_point":/);
  assert.match(uiaSource, /def click_point\(self, x: int, y: int, click_type: str = "click"/);
  assert.match(uiaSource, /def invoke_point\(self, x: int, y: int/);
});

test('P5.6 plugin-list icon is configured and readable through the workspace', () => {
  const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8'));
  const plugin = JSON.parse(readFileSync(join(ROOT, 'dsh.plugin.json'), 'utf8'));
  assert.equal(pkg.icon, 'assets/icon.png');
  assert.equal(plugin.icon, 'assets/icon.png');
  const icon = join(ROOT, 'assets', 'icon.png');
  assert.equal(existsSync(icon), true);
  assert.ok(statSync(icon).size > 0 && statSync(icon).size <= 256 * 1024);
  const bytes = readFileSync(icon).subarray(0, 8);
  assert.deepEqual([...bytes], [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
});
