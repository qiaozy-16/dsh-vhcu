import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
  cleanupWorkspaceArtifacts,
  isVhcuFramePng,
  markVhcuPng,
} from '../lib/workspace-cleanup.js';

// 1x1 transparent PNG.
const PNG_1X1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
  'base64',
);

function tempWorkspace() {
  const dir = mkdtempSync(join(tmpdir(), 'vhcu-workspace-'));
  writeFileSync(join(dir, 'keep.png'), PNG_1X1);
  writeFileSync(join(dir, 'frame.png'), markVhcuPng(PNG_1X1));
  writeFileSync(join(dir, 'crop.ps1'), 'Invoke-WebRequest "http://127.0.0.1/x/vhcu/frame?sig=1" -OutFile native.png');
  writeFileSync(join(dir, 'vhcu-captcha-1.ps1'), 'Add-Type -AssemblyName System.Drawing');
  writeFileSync(join(dir, 'vhcu-cluster.py'), 'print("pixels")');
  writeFileSync(join(dir, 'notes.ps1'), 'Write-Host hello');
  writeFileSync(join(dir, 'keep.txt'), 'not a screenshot');
  return dir;
}

test('P9 markVhcuPng adds a marker without breaking PNG bytes', () => {
  const marked = markVhcuPng(PNG_1X1);
  assert.ok(marked.length > PNG_1X1.length);
  assert.equal(marked.subarray(0, 8).toString('hex'), PNG_1X1.subarray(0, 8).toString('hex'));
  assert.equal(marked.subarray(-8).toString('hex'), PNG_1X1.subarray(-8).toString('hex'));
  assert.equal(isVhcuFramePng(marked), true);
  assert.equal(isVhcuFramePng(PNG_1X1), false);
});

test('P9 cleanup removes marked frames and VHCU crop scripts only', () => {
  const dir = tempWorkspace();
  try {
    const result = cleanupWorkspaceArtifacts({ root: dir, minAgeMs: 0 });
    assert.deepEqual(result.removed.sort(), [
      'crop.ps1',
      'frame.png',
      'vhcu-captcha-1.ps1',
      'vhcu-cluster.py',
    ]);
    assert.equal(readFileSync(join(dir, 'keep.png')).length, PNG_1X1.length);
    assert.equal(readFileSync(join(dir, 'notes.ps1'), 'utf8'), 'Write-Host hello');
    assert.equal(readFileSync(join(dir, 'keep.txt'), 'utf8'), 'not a screenshot');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('P9 cleanup respects minAgeMs for a just-written frame', () => {
  const dir = tempWorkspace();
  try {
    const result = cleanupWorkspaceArtifacts({ root: dir, minAgeMs: 60_000 });
    assert.deepEqual(result.removed, []);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
