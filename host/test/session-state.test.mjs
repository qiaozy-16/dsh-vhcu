import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
  MAX_AGE_MS,
  SessionStateStore,
  validateSessionState,
} from '../lib/session-state.js';

function state(overrides = {}) {
  const now = Date.now();
  return {
    version: 1,
    queue_id: 'q_1234abcd',
    status: 'running',
    current_index: 1,
    total: 2,
    items: [
      { name: 'macro_a', variables: { text: 'hello' } },
      { name: 'macro_b', variables: {} },
    ],
    options: { skipReadback: false, imageRelative: false, scaleX: 1.5 },
    created_at: now - 1000,
    updated_at: now - 500,
    ...overrides,
  };
}

test('session state saves atomically and loads a validated queue', () => {
  const dir = mkdtempSync(join(tmpdir(), 'vhcu-session-'));
  const path = join(dir, 'session_state.json');
  try {
    const store = new SessionStateStore({ path });
    const saved = store.save(state());
    assert.equal(saved.queue_id, 'q_1234abcd');
    assert.equal(store.state.current_index, 1);
    assert.equal(existsSync(path), true);
    assert.equal(readFileSync(path, 'utf8').includes('q_1234abcd'), true);
    assert.equal(readdirSync(dir).filter((name) => name.endsWith('.tmp')).length, 0);
    const loaded = new SessionStateStore({ path });
    assert.equal(loaded.state.current_index, 1);
    assert.equal(loaded.state.items.length, 2);
    assert.equal(loaded.lastError, null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('non-durable progress save is still atomic and reloadable', () => {
  const dir = mkdtempSync(join(tmpdir(), 'vhcu-session-'));
  const path = join(dir, 'session_state.json');
  try {
    const store = new SessionStateStore({ path });
    store.save(state(), { durable: false });
    assert.equal(existsSync(path), true);
    assert.equal(readdirSync(dir).filter((name) => name.endsWith('.tmp')).length, 0);
    const loaded = new SessionStateStore({ path });
    assert.equal(loaded.state.current_index, 1);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('corrupt or stale session state is quarantined, never thrown into startup', () => {
  const dir = mkdtempSync(join(tmpdir(), 'vhcu-session-'));
  const path = join(dir, 'session_state.json');
  try {
    writeFileSync(path, '{not-json');
    const corrupted = new SessionStateStore({ path });
    assert.equal(corrupted.state, null);
    assert.match(corrupted.lastError.code, /E_SESSION_STATE/);
    assert.equal(existsSync(`${path}.broken`), true);

    const stale = state({ updated_at: Date.now() - MAX_AGE_MS - 5000 });
    writeFileSync(path, JSON.stringify(stale));
    const expired = new SessionStateStore({ path });
    assert.equal(expired.state, null);
    assert.equal(existsSync(`${path}.broken`), true);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('invalid state fails closed and clear removes the file', () => {
  const dir = mkdtempSync(join(tmpdir(), 'vhcu-session-'));
  const path = join(dir, 'session_state.json');
  try {
    const store = new SessionStateStore({ path });
    assert.throws(
      () => store.save(state({ total: 3 })),
      (error) => error.code === 'E_MACRO_STATE_PERSIST',
    );
    assert.equal(existsSync(path), false);
    store.save(state());
    assert.equal(store.clear(), true);
    assert.equal(existsSync(path), false);
    assert.equal(store.load(), null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('validation caps queue size and rejects an out-of-range index', () => {
  assert.ok(validateSessionState(state()));
  assert.equal(validateSessionState(state({ total: 21, items: [] })), null);
  assert.equal(validateSessionState(state({ current_index: 3 })), null);
  assert.equal(validateSessionState(state({ queue_id: 'bad' })), null);
  assert.equal(validateSessionState(state({ options: { imageRelative: true } })).options.imageRelative, true);
});
