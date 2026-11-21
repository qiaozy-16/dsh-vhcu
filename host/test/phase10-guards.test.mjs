/**
 * @file phase10-guards.test.mjs — P10 shell-script denial and cursor outline.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const host = readFileSync(join(ROOT, 'lib', 'index.js'), 'utf8');
const cleanup = readFileSync(join(ROOT, 'host', 'lib', 'workspace-cleanup.js'), 'utf8');
const cursor = readFileSync(join(ROOT, 'vision', 'vhcu_daemon', 'cursor_overlay.py'), 'utf8');
const contract = readFileSync(join(ROOT, 'contract', 'lib', 'contract.js'), 'utf8');

test('P10 shell middleware denies workspace script creation before dispatch', () => {
  assert.match(host, /detectScriptWrite/);
  assert.match(host, /ctx\.on\('tools\/execute'/);
  assert.match(host, /deniedToolResult\(detail\)/);
  assert.match(contract, /E_SCRIPT_WRITE_DENIED:/);
});

test('P10 cleanup also removes vhcu-* and .py helper scripts', () => {
  assert.match(cleanup, /\\\.\(ps1\|cmd\|bat\|sh\|py\)/);
  assert.match(cleanup, /\^vhcu\[-_\]/);
  assert.match(cleanup, /explicitlyNamed/);
});

test('P10 cursor keeps the white rim on the solid cursor and ripple only', () => {
  assert.match(cursor, /OUTLINE_DIP = 1\.5/);
  assert.match(cursor, /OUTLINE_ARGB = 0xFFFFFFFF/);
  assert.match(cursor, /GdipDrawPath\.argtypes/);
  assert.match(cursor, /self\._stroke_path\(gp, path, OUTLINE_ARGB, OUTLINE_DIP \* scale\)/);
  // The enlarged glow must not be stroked; it produced a stray white line.
  assert.doesNotMatch(cursor, /outline=index == 0/);
  assert.match(cursor, /self\._fill_rounded\(gp, points, _rgb_argb\(rgb, alpha\), 4\.0 \* self\._scale\)/);
  assert.match(cursor, /OUTLINE_ARGB, OUTLINE_DIP \* self\._scale/);
});
