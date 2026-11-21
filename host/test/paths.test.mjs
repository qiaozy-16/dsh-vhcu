import { test } from 'node:test';
import assert from 'node:assert/strict';
import { isAbsolute, join, resolve } from 'node:path';
import { tmpdir } from 'node:os';

import { resolveRuntimePaths, resourceEnv } from '../lib/paths.js';

test('P1 resource paths are absolute and preserve Windows defaults', () => {
  const root = 'C:\\repo\\dsh-vhcu';
  const data = join(tmpdir(), 'vhcu-paths-test');
  const paths = resolveRuntimePaths({ root, data, env: {} });
  assert.equal(paths.models, resolve(root, 'models'));
  assert.equal(paths.macros, resolve(data, 'dsh-vhcu', 'macros.json'));
  assert.equal(paths.logs, resolve(data, 'dsh-vhcu', 'logs'));
  assert.equal(paths.settings, resolve(data, 'dsh-vhcu', 'settings.json'));
  assert.equal(paths.sessionState, resolve(data, 'dsh-vhcu', 'session_state.json'));
  for (const value of Object.values(paths)) assert.ok(isAbsolute(value), value);
});

test('P1 resource paths honor injected absolute env values', () => {
  const root = join(tmpdir(), 'vhcu-repo');
  const env = {
    VHCU_MODEL_PATH: join(tmpdir(), 'vhcu-models'),
    VHCU_MACROS_PATH: join(tmpdir(), 'vhcu-macros.json'),
    VHCU_LOG_DIR: join(tmpdir(), 'vhcu-logs'),
    VHCU_SETTINGS_PATH: join(tmpdir(), 'vhcu-settings.json'),
    VHCU_SESSION_STATE_PATH: join(tmpdir(), 'vhcu-session.json'),
  };
  const paths = resolveRuntimePaths({ root, env });
  assert.equal(paths.models, env.VHCU_MODEL_PATH);
  assert.equal(paths.macros, env.VHCU_MACROS_PATH);
  assert.equal(paths.logs, env.VHCU_LOG_DIR);
  assert.equal(paths.settings, env.VHCU_SETTINGS_PATH);
  assert.equal(paths.sessionState, env.VHCU_SESSION_STATE_PATH);
  assert.deepEqual(resourceEnv(paths), {
    VHCU_MODEL_PATH: env.VHCU_MODEL_PATH,
    VHCU_MACROS_PATH: env.VHCU_MACROS_PATH,
    VHCU_LOG_DIR: env.VHCU_LOG_DIR,
  });
});

test('P1 resource paths reject relative overrides', () => {
  assert.throws(
    () => resolveRuntimePaths({ root: join(tmpdir(), 'vhcu-repo'), env: { VHCU_MODEL_PATH: 'models' } }),
    (error) => error.code === 'E_CONFIG',
  );
});

test('P12 profile state paths are hashed and isolated', () => {
  const root = join(tmpdir(), 'vhcu-repo');
  const data = join(tmpdir(), 'vhcu-paths-profile');
  const desktop = resolveRuntimePaths({ root, data, env: { DSH_PROFILE: 'desktop' } });
  const work = resolveRuntimePaths({ root, data, env: { DSH_PROFILE: 'work' } });
  assert.notEqual(desktop.settings, work.settings);
  assert.notEqual(desktop.macros, work.macros);
  assert.notEqual(desktop.sessionState, work.sessionState);
  for (const value of [desktop.settings, desktop.macros, desktop.sessionState]) {
    assert.match(value, /[\\/]dsh-vhcu[\\/]profiles[\\/][a-f0-9]{16}[\\/]/);
  }
  assert.equal(desktop.logs, work.logs);
});
