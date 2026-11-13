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
  for (const value of Object.values(paths)) assert.ok(isAbsolute(value), value);
});

test('P1 resource paths honor injected absolute env values', () => {
  const root = join(tmpdir(), 'vhcu-repo');
  const env = {
    VHCU_MODEL_PATH: join(tmpdir(), 'vhcu-models'),
    VHCU_MACROS_PATH: join(tmpdir(), 'vhcu-macros.json'),
    VHCU_LOG_DIR: join(tmpdir(), 'vhcu-logs'),
  };
  const paths = resolveRuntimePaths({ root, env });
  assert.equal(paths.models, env.VHCU_MODEL_PATH);
  assert.equal(paths.macros, env.VHCU_MACROS_PATH);
  assert.equal(paths.logs, env.VHCU_LOG_DIR);
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
