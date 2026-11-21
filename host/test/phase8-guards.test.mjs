/**
 * @file phase8-guards.test.mjs — V1.1 macro semantic governance guards.
 *
 * P8.1 tool prompt forces parameterization and UIA-first guidance;
 * P8.2 save rejects >70% raw coordinate clicks with E_MACRO_NOT_REUSABLE;
 * P8.3 macro list badges distinguish parameterized and process recordings.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  analyzeMacroReusability,
  isMacroNotReusable,
} from '../lib/macros.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const hostSource = readFileSync(join(ROOT, 'lib', 'index.js'), 'utf8');
const clientSource = readFileSync(join(ROOT, 'lib', 'client.js'), 'utf8');
const contractSource = readFileSync(join(ROOT, 'contract', 'lib', 'contract.js'), 'utf8');
const errorSchema = readFileSync(join(ROOT, 'contract', 'schema', 'v1', 'error.json'), 'utf8');

test('P8.1 save_macro prompt requires reusable parameterized functions', () => {
  const save = hostSource.indexOf("name: 'vhcu_save_macro'");
  const block = hostSource.slice(save, save + 2200);
  assert.match(block, /Do NOT record raw coordinate steps\./);
  assert.match(block, /reusable, parameterized functions/);
  assert.match(block, /Use parameter variables instead of hardcoded inputs/);
  assert.match(block, /UIA/);
  assert.match(block, /E_MACRO_NOT_REUSABLE/);
});

test('P8.2 raw coordinate recordings are rejected before persistence', () => {
  const save = hostSource.indexOf("name: 'vhcu_save_macro'");
  const check = hostSource.indexOf('if (isMacroNotReusable(args?.steps))', save);
  const storeAdd = hostSource.indexOf('macroStore.add({', save);
  assert.ok(check > save && storeAdd > check, 'reusability check must run before add');
  const block = hostSource.slice(check, storeAdd);
  assert.match(block, /E_MACRO_NOT_REUSABLE/);
  assert.match(block, /analysis/);
  assert.match(contractSource, /E_MACRO_NOT_REUSABLE:/);
  assert.match(errorSchema, /E_MACRO_NOT_REUSABLE/);
});

test('P8.2b per-step threshold: variables cannot launder raw clicks', () => {
  const click = (x, y) => ({ op: 'click', x, y });
  assert.equal(isMacroNotReusable([click(1, 1)]), true);
  assert.equal(isMacroNotReusable([click(1, 1), click(2, 2), { op: 'wait', ms: 5 }]), false);
  assert.equal(
    isMacroNotReusable([click(1, 1), click(2, 2), click(3, 3), { op: 'wait', ms: 5 }]),
    true,
  );
  assert.equal(isMacroNotReusable([{ op: 'click', x: '{{x}}', y: '{{y}}' }]), false);
  assert.equal(
    isMacroNotReusable([click(1, 1), click(2, 2), click(3, 3), { op: 'type', text: '{{v}}' }]),
    true,
    '3/4 raw clicks must be rejected even with a variable present',
  );
  assert.deepEqual(
    analyzeMacroReusability([click(1, 1), { op: 'drag', x1: 1, y1: 2, x2: 3, y2: 4 }]),
    { steps: 2, rawClicks: 1, ratio: 0.5, variables: [] },
  );
});

test('P8.3 macro list badges distinguish parameterized and process recordings', () => {
  assert.match(clientSource, /vhcu-macro-badge/);
  assert.match(clientSource, /【参数化】/);
  assert.match(clientSource, /【过程录制】/);
  assert.match(clientSource, /function macroIsParameterized/);
  assert.match(clientSource, /function macroHasRawCoordinateClick/);
  assert.match(clientSource, /\.vhcu-macro-badge\.param/);
  assert.match(clientSource, /\.vhcu-macro-badge\.raw/);
});
