import { test } from 'node:test';
import assert from 'node:assert/strict';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { Supervisor } from '../lib/supervisor.js';
import { resolvePython } from '../lib/python.js';
import { fetchFrame, isPng } from '../lib/frames.js';
import { validateFrame } from '../../contract/lib/contract.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '..', '..');
const VISION = join(ROOT, 'vision');

const python = await resolvePython().catch(() => null);
const skip = python ? false : 'no usable Python 3 on this machine';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(predicate, timeoutMs = 8000, label = 'condition') {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (predicate()) return true;
    await sleep(40);
  }
  throw new Error(`timeout waiting for ${label}`);
}

function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

test('real Python daemon: ready → diff events → PNG frame → stop → no zombie', { skip, timeout: 30000 }, async () => {
  const supervisor = new Supervisor({
    python,
    visionPath: VISION,
    daemonArgs: ['-u', '-m', 'vhcu_daemon'],
    cwd: ROOT,
    readyTimeoutMs: 15000,
    heartbeatMs: 500,
    heartbeatMissLimit: 3,
    shutdownGraceMs: 2000,
  });

  const diffs = [];
  supervisor.on('event', (frame) => {
    if (frame.kind === 'vision.diff') diffs.push(frame);
  });

  let ready;
  try {
    ready = await supervisor.start();
    assert.equal(ready.proto, 1);
    assert.ok(ready.capabilities.sources.includes('fake'));
    assert.ok(ready.port > 0);
    assert.ok(ready.token.length >= 16);

    const pong = await supervisor.send('ping', {}, { timeoutMs: 3000 });
    assert.equal(pong.pong, true);

    await supervisor.send('vision.start', { source: 'fake', fps: 10 }, { timeoutMs: 5000 });
    await waitFor(() => diffs.length >= 2, 8000, 'two diff events');

    for (const frame of diffs) {
      const check = validateFrame(frame);
      assert.equal(check.ok, true, check.errors?.join('; '));
      assert.ok(frame.stats.diffMs >= 0);
      assert.ok(frame.rois.length >= 1 && frame.rois.length <= 8);
    }

    const last = diffs[diffs.length - 1];
    const roi = last.rois[0];
    const image = await fetchFrame({
      port: ready.port,
      token: ready.token,
      x: roi.x,
      y: roi.y,
      w: roi.w,
      h: roi.h,
      maxWidth: 160,
    });
    assert.equal(isPng(image.buffer), true, 'image plane must return PNG');
    assert.ok(image.bytes > 50);

    const status = await supervisor.send('vision.status', {}, { timeoutMs: 3000 });
    assert.ok(status.frames >= 2);
    assert.equal(status.state, 'running');

    await supervisor.send('vision.stop', { reason: 'test' }, { timeoutMs: 3000 });
    const count = diffs.length;
    await sleep(500);
    assert.equal(diffs.length, count, 'no diff events after vision.stop');

    const pid = ready.pid;
    await supervisor.stop('test-done');
    assert.equal(supervisor.state, 'stopped');
    await waitFor(() => !isAlive(pid), 3000, 'python process exit');
  } finally {
    await supervisor.stop('cleanup').catch(() => {});
  }
});

test('image plane rejects a wrong token', { skip, timeout: 20000 }, async () => {
  const supervisor = new Supervisor({
    python,
    visionPath: VISION,
    daemonArgs: ['-u', '-m', 'vhcu_daemon'],
    cwd: ROOT,
    readyTimeoutMs: 15000,
  });
  try {
    const ready = await supervisor.start();
    await supervisor.send('vision.start', { source: 'fake', fps: 4 }, { timeoutMs: 5000 });
    await assert.rejects(
      fetchFrame({ port: ready.port, token: 'x'.repeat(24), maxWidth: 64 }),
      (error) => error.status === 401 || error.code === 'E_CAPTURE',
    );
  } finally {
    await supervisor.stop('cleanup').catch(() => {});
  }
});
