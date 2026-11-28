/**
 * @file profile-scope.test.mjs — is profile isolation actually active?
 *
 * The plugin only isolates user state when the host injects one of
 * `DSH_PROFILE_KEY` / `DSH_PROFILE` / `DSH_PROFILE_DIR`; otherwise every profile
 * shares `<data>/dsh-vhcu`. That used to be an unobservable guess ("does the
 * host inject it?"). `/vhcu/state` now reports the resolved scope, so these
 * tests pin the contract: with a profile env the state dir is hashed, without
 * it the scope is null and the shared directory is used.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { apply } from '../../lib/index.js';
import { createMockCtx } from '../../dev/mock-ctx.mjs';

const suffix = `${process.pid}-${Date.now()}`;
const tempFiles = {
  VHCU_MACROS_PATH: join(tmpdir(), `vhcu-scope-macros-${suffix}.json`),
  VHCU_SETTINGS_PATH: join(tmpdir(), `vhcu-scope-settings-${suffix}.json`),
  VHCU_SESSION_STATE_PATH: join(tmpdir(), `vhcu-scope-session-${suffix}.json`),
};
const PROFILE_KEYS = ['DSH_PROFILE', 'DSH_PROFILE_KEY', 'DSH_PROFILE_DIR'];

const silentLogger = { debug() {}, info() {}, warn() {}, error() {} };

/** GET /vhcu/state against the mock route table, returning the state object. */
function readState(routes) {
  const route = routes.get('/vhcu/state');
  assert.ok(route, 'plugin did not register /vhcu/state');
  return new Promise((resolve, reject) => {
    const res = {
      headersSent: false,
      statusCode: 200,
      setHeader() {},
      writeHead() {},
      end(text) {
        try {
          resolve(JSON.parse(text).state);
        } catch (error) {
          reject(error);
        }
      },
    };
    const req = { method: 'GET', url: '/vhcu/state', headers: { host: '127.0.0.1' } };
    Promise.resolve(route.handler(req, res)).catch(reject);
  });
}

async function withProfileEnv(env, fn) {
  const saved = { ...process.env };
  for (const key of Object.keys(tempFiles)) process.env[key] = tempFiles[key];
  for (const key of PROFILE_KEYS) delete process.env[key];
  process.env.VHCU_DISABLE_USER_TAKEOVER = '1';
  Object.assign(process.env, env);
  const mock = createMockCtx({ logger: silentLogger });
  try {
    apply(mock.ctx);
    return await fn(mock);
  } finally {
    mock.dispose();
    process.env = saved;
    for (const path of Object.values(tempFiles)) rmSync(path, { force: true });
  }
}

test('no DSH_PROFILE* => shared state dir and null scope', async () => {
  await withProfileEnv({}, async ({ routes }) => {
    const state = await readState(routes);
    assert.equal(state.paths.profileScope, null);
    assert.match(state.paths.stateDir, /dsh-vhcu$/);
    assert.doesNotMatch(state.paths.stateDir, /profiles/);
  });
});

test('DSH_PROFILE => hashed per-profile state dir', async () => {
  await withProfileEnv({ DSH_PROFILE: 'desktop' }, async ({ routes }) => {
    const state = await readState(routes);
    assert.match(state.paths.profileScope, /^[a-f0-9]{16}$/);
    assert.match(state.paths.stateDir, /[\\/]dsh-vhcu[\\/]profiles[\\/][a-f0-9]{16}$/);
  });
});

test('DSH_PROFILE_KEY and DSH_PROFILE_DIR also activate isolation', async () => {
  for (const key of ['DSH_PROFILE_KEY', 'DSH_PROFILE_DIR']) {
    await withProfileEnv({ [key]: 'alpha' }, async ({ routes }) => {
      const state = await readState(routes);
      assert.equal(
        typeof state.paths.profileScope,
        'string',
        `${key} did not activate profile isolation`,
      );
      assert.match(state.paths.stateDir, /[\\/]profiles[\\/][a-f0-9]{16}$/);
    });
  }
});

test('scope is a stable hash, never the raw profile value', async () => {
  await withProfileEnv({ DSH_PROFILE: '../escape/attempt' }, async ({ routes }) => {
    const state = await readState(routes);
    assert.match(state.paths.profileScope, /^[a-f0-9]{16}$/);
    assert.doesNotMatch(state.paths.stateDir, /escape|\.\./);
  });
});
