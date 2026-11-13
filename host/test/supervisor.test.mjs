import { test } from 'node:test';
import assert from 'node:assert/strict';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { Supervisor } from '../lib/supervisor.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, 'fixtures', 'fake-daemon.mjs');

function makeSupervisor(mode = 'normal', overrides = {}) {
  return new Supervisor({
    python: { cmd: process.execPath, args: [FIXTURE] },
    visionPath: HERE,
    daemonArgs: [],
    env: { FAKE_MODE: mode, FAKE_HEARTBEAT_MS: '100' },
    readyTimeoutMs: 2000,
    heartbeatMs: 100,
    heartbeatMissLimit: 3,
    restartBackoffMinMs: 80,
    restartBackoffMaxMs: 300,
    breakerMaxFailures: 3,
    breakerWindowMs: 10000,
    shutdownGraceMs: 500,
    termGraceMs: 500,
    ...overrides,
  });
}

function once(emitter, name, timeoutMs = 3000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timeout waiting for ${name}`)), timeoutMs);
    emitter.once(name, (...args) => {
      clearTimeout(timer);
      resolve(args);
    });
  });
}

async function waitFor(predicate, timeoutMs = 3000, label = 'condition') {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (await predicate()) return true;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(`timeout waiting for ${label}`);
}

function isAlive(pid) {
  if (!pid) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

test('starts, reports ready, answers ping, then stops cleanly', async () => {
  const supervisor = makeSupervisor();
  supervisor.installExitHook();
  try {
    const ready = await supervisor.start();
    assert.equal(ready.proto, 1);
    assert.ok(ready.pid > 0);
    assert.equal(supervisor.state, 'ready');

    const pong = await supervisor.send('ping', {}, { timeoutMs: 1000 });
    assert.equal(pong.pong, true);
    assert.equal(pong.pid, ready.pid);

    const pid = ready.pid;
    await supervisor.stop('test');
    assert.equal(supervisor.state, 'stopped');
    await waitFor(() => !isAlive(pid), 2000, 'child process to exit');
  } finally {
    await supervisor.stop('cleanup');
    supervisor.dispose();
  }
});

test('rejects an unknown op without touching the daemon', async () => {
  const supervisor = makeSupervisor();
  try {
    await supervisor.start();
    await assert.rejects(supervisor.send('rm -rf /', {}), (error) => error.code === 'E_DENIED');
    assert.equal(supervisor.state, 'ready');
  } finally {
    await supervisor.stop('cleanup');
  }
});

test('command deadline produces E_TIMEOUT and leaves the daemon usable', async () => {
  const supervisor = makeSupervisor('hang-on-cmd');
  try {
    await supervisor.start();
    await assert.rejects(
      supervisor.send('ping', {}, { timeoutMs: 150 }),
      (error) => error.code === 'E_TIMEOUT',
    );
    assert.equal(supervisor.state, 'ready');
    assert.equal(supervisor.pending.size, 0);
  } finally {
    await supervisor.stop('cleanup');
  }
});

test('a crashed daemon is restarted with backoff', async () => {
  const supervisor = makeSupervisor();
  try {
    const first = await supervisor.start();
    const exit = once(supervisor, 'exit');
    supervisor.child.kill('SIGKILL');
    await exit;
    await waitFor(() => supervisor.state === 'ready' && supervisor.readyInfo?.pid !== first.pid, 4000, 'restart');
    assert.ok(supervisor.restarts >= 1);
    const pong = await supervisor.send('ping', {}, { timeoutMs: 1000 });
    assert.equal(pong.pong, true);
  } finally {
    await supervisor.stop('cleanup');
  }
});

test('READY timeout rejects start and schedules a restart', async () => {
  const supervisor = makeSupervisor('hang', { readyTimeoutMs: 200 });
  try {
    await assert.rejects(supervisor.start(), (error) => error.code === 'E_TIMEOUT');
    await waitFor(() => supervisor.spawns >= 2, 3000, 'restart attempt');
  } finally {
    await supervisor.stop('cleanup');
  }
});

test('crash loop trips the breaker and stops spawning', async () => {
  const supervisor = makeSupervisor('crash-on-start', { readyTimeoutMs: 500 });
  try {
    await assert.rejects(supervisor.start());
    await waitFor(() => supervisor.state === 'circuit-open', 4000, 'breaker open');
    const spawns = supervisor.spawns;
    await new Promise((resolve) => setTimeout(resolve, 300));
    assert.equal(supervisor.spawns, spawns, 'no spawns after breaker trips');
    assert.ok(supervisor.failures.length >= 3);
  } finally {
    await supervisor.stop('cleanup');
  }
});

test('lost heartbeat is treated as death even though the process is alive', async () => {
  const supervisor = makeSupervisor('no-heartbeat', { heartbeatMs: 60, heartbeatMissLimit: 2 });
  try {
    await supervisor.start();
    const pid = supervisor.readyInfo.pid;
    const [failure] = await once(supervisor, 'failure', 3000);
    assert.equal(failure.kind, 'heartbeat-lost');
    await waitFor(() => !isAlive(pid), 3000, 'hung child killed');
  } finally {
    await supervisor.stop('cleanup');
  }
});

test('shutdown escalation kills a daemon that ignores the shutdown frame', async () => {
  const supervisor = makeSupervisor('ignore-shutdown', { shutdownGraceMs: 150 });
  try {
    const ready = await supervisor.start();
    const pid = ready.pid;
    const started = Date.now();
    await supervisor.stop('escalation');
    assert.equal(supervisor.state, 'stopped');
    assert.ok(Date.now() - started < 5000);
    await waitFor(() => !isAlive(pid), 3000, 'child killed after escalation');
  } finally {
    await supervisor.stop('cleanup');
  }
});

test('sequence gaps surface as E_SEQ but do not drop the daemon', async () => {
  const supervisor = makeSupervisor('gap');
  try {
    await supervisor.start();
    const [frame] = await once(supervisor, 'protocol-error', 3000);
    assert.equal(frame.code, 'E_SEQ');
    assert.match(frame.message, /sequence gap/);
    assert.equal(supervisor.state, 'ready');
  } finally {
    await supervisor.stop('cleanup');
  }
});

test('P17.4 heartbeat timeout automatically respawns the daemon', async () => {
  const supervisor = makeSupervisor('no-heartbeat', {
    heartbeatMs: 60,
    heartbeatMissLimit: 2,
    restartBackoffMinMs: 20,
    restartBackoffMaxMs: 200,
    breakerMaxFailures: 5,
  });
  try {
    await supervisor.start();
    assert.equal(supervisor.spawns, 1);
    const [failure] = await once(supervisor, 'failure', 3000);
    assert.equal(failure.kind, 'heartbeat-lost');
    await waitFor(() => supervisor.spawns >= 2, 3000, 'heartbeat respawn');
    assert.notEqual(supervisor.state, 'circuit-open');
  } finally {
    await supervisor.stop('cleanup');
  }
});

test('P1 supervisor injects absolute resource paths into the daemon env', async () => {
  const resourcePaths = {
    models: 'C:\vhcu\models',
    macros: 'C:\Users\vhcu\macros.json',
    logs: 'C:\Users\vhcu\logs',
  };
  const supervisor = new Supervisor({
    python: { cmd: process.execPath, args: [FIXTURE] },
    visionPath: HERE,
    daemonArgs: [],
    cwd: HERE,
    resourcePaths,
    logger: { debug() {}, info() {}, warn() {}, error() {} },
  });
  const env = supervisor._buildEnv();
  assert.equal(env.VHCU_MODEL_PATH, resourcePaths.models);
  assert.equal(env.VHCU_MACROS_PATH, resourcePaths.macros);
  assert.equal(env.VHCU_LOG_DIR, resourcePaths.logs);
  await supervisor.dispose();
});
