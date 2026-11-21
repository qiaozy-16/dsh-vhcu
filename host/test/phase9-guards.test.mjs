/**
 * @file phase9-guards.test.mjs — V1.1 workspace hygiene and mouse-efficiency guards.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const hostSource = readFileSync(join(ROOT, 'lib', 'index.js'), 'utf8');

test('P9 frames stay in memory with a five-minute link and no-store header', () => {
  assert.match(hostSource, /const FRAME_TTL_MS = 5 \* 60 \* 1000;/);
  assert.match(hostSource, /'cache-control': 'no-store'/);
  assert.match(hostSource, /'x-vhcu-frame-storage': 'daemon-memory'/);
  assert.match(hostSource, /markVhcuPng\(image\.buffer\)/);
});

test('P9 tool prompts forbid downloading frames into the workspace', () => {
  assert.match(hostSource, /严禁用 shell\/脚本把截图下载或另存到用户工作区/);
  assert.match(hostSource, /截图仅在守护进程内存（5 分钟签名 URL）/);
});

test('P9 workspace cleanup runs on mount and dispose', () => {
  const mounts = hostSource.match(/cleanupWorkspaceArtifacts\(/g) ?? [];
  assert.ok(mounts.length >= 2, 'startup and shutdown must both call cleanup');
  assert.match(hostSource, /minAgeMs: 60_000/);
  assert.match(hostSource, /minAgeMs: 0/);
});

test('P9 nine mouse steps add the macro-efficiency hint', () => {
  assert.match(hostSource, /let consecutiveMouseSteps = 0;/);
  assert.match(hostSource, /检测到大量连续鼠标操作，效率极低，请考虑将当前步骤保存为参数化宏。/);
  assert.match(hostSource, /consecutiveMouseSteps <= 8/);
  assert.match(hostSource, /\.\.\.mouseEfficiencyNotice\(\)/);
  assert.match(hostSource, /resetMouseEfficiency\(\);/);
});
