/**
 * @file phase7-guards.test.mjs — regression guards for V1.1 precision vision
 * and opt-in macro self-growth.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DEFAULT_SETTINGS, mergeSettings } from '../lib/settings.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const hostSource = readFileSync(join(ROOT, 'lib', 'index.js'), 'utf8');
const clientSource = readFileSync(join(ROOT, 'lib', 'client.js'), 'utf8');
const contractSource = readFileSync(join(ROOT, 'contract', 'lib', 'contract.js'), 'utf8');
const errorSchema = readFileSync(join(ROOT, 'contract', 'schema', 'v1', 'error.json'), 'utf8');

test('P7.1 precision defaults: allowed by default, macro generation off', () => {
  assert.equal(DEFAULT_SETTINGS.allowHighPrecisionVision, true);
  assert.equal(DEFAULT_SETTINGS.enableMacroAutoGeneration, false);
  assert.equal(mergeSettings({}, { allowHighPrecisionVision: false }).allowHighPrecisionVision, false);
  assert.equal(mergeSettings({}, { enableMacroAutoGeneration: true }).enableMacroAutoGeneration, true);
});

test('P7.2 both capture tools expose precision_mode and bypass scaling', () => {
  const full = hostSource.indexOf("name: 'vhcu_get_full_frame'");
  const target = hostSource.indexOf("name: 'vhcu_capture_target'");
  const next = hostSource.indexOf("name: 'vhcu_find_elements'");
  const fullBlock = hostSource.slice(full, target);
  const targetBlock = hostSource.slice(target, next > target ? next : undefined);
  assert.match(fullBlock, /precision_mode: \{/);
  assert.match(targetBlock, /precision_mode: \{/);
  assert.match(hostSource, /const precision = precisionRequested && precisionAllowed\(\);/);
  assert.match(hostSource, /if \(precision\) maxw = Math\.max\(320, Math\.trunc\(Number\(capture\?\.width\) \|\| maxw\)\);/);
  assert.match(hostSource, /if \(precision\) maxw = Math\.max\(320, width\);/);
  assert.match(hostSource, /precision_denied: precisionDenied/);
  assert.match(hostSource, /markPrecisionCapture\(/);
});

test('P7.3 UI exposes both switches and the precision indicator', () => {
  assert.ok(clientSource.includes('允许模型使用高精度视觉'));
  assert.ok(clientSource.includes('允许模型自动生成宏（宏自生长）'));
  assert.ok(clientSource.includes('高精度采集中'));
  assert.match(clientSource, /vhcu-precision-banner/);
  assert.match(clientSource, /allowHighPrecisionVision/);
  assert.match(clientSource, /enableMacroAutoGeneration/);
});

test('P7.4 macro save is blocked with the new contract error when disabled', () => {
  const save = hostSource.indexOf("name: 'vhcu_save_macro'");
  const guard = hostSource.indexOf('if (!settings.enableMacroAutoGeneration)', save);
  const store = hostSource.indexOf('if (!macroStore)', save);
  assert.ok(guard > save && store > guard, 'setting guard must run before persistence');
  assert.match(hostSource.slice(guard, store), /E_MACRO_GENERATION_DISABLED/);
  assert.match(contractSource, /E_MACRO_GENERATION_DISABLED:/);
  assert.match(errorSchema, /E_MACRO_GENERATION_DISABLED/);
});

test('P7.5 precision frame URLs may exceed the normal 1920 cap', () => {
  assert.match(hostSource, /Math\.min\(16384, Math\.trunc\(Number\(maxWidth\) \|\| 960\)\)/);
});
