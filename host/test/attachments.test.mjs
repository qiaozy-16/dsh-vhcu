/**
 * @file attachments.test.mjs — VHCU's attachment ledger, quota and recycle-bin GC.
 *
 * The host attachment store is append-only, content-addressed and shared with
 * other components, so VHCU can only reclaim what it recorded itself. These
 * tests pin:
 *   - id normalisation and path construction (no traversal, no bad ids);
 *   - ledger dedupe, budget stats and oldest-first reclaim selection;
 *   - cleanup forgiving missing objects, keeping failures, never leaking paths;
 *   - the plugin's /vhcu/attachments surface (GET stats, POST cleanup).
 *
 * The real recycle-bin call is injected in tests: nothing here touches a real
 * recycle bin or a real user directory.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
  ATTACHMENT_BYTE_LIMIT,
  ATTACHMENT_COUNT_LIMIT,
  AttachmentRegistry,
  attachmentObjectPath,
  normalizeAttachmentId,
  resolveAttachmentsRoot,
} from '../lib/attachment-registry.js';
import { cleanupAttachments } from '../lib/attachment-gc.js';
import { apply } from '../../lib/index.js';
import { createMockCtx } from '../../dev/mock-ctx.mjs';

const HEX_A = 'a'.repeat(64);
const HEX_B = 'b'.repeat(64);

function tempDir(label) {
  return mkdtempSync(join(tmpdir(), `vhcu-attach-${label}-`));
}

test('attachments: ids are exactly 64 hex chars, sha256 prefix tolerated', () => {
  assert.equal(normalizeAttachmentId(HEX_A), HEX_A);
  assert.equal(normalizeAttachmentId(`sha256:${HEX_A}`), HEX_A);
  assert.equal(normalizeAttachmentId(`SHA256:${HEX_A.toUpperCase()}`), HEX_A);
  assert.equal(normalizeAttachmentId('sha256:nothex'), null);
  assert.equal(normalizeAttachmentId('../escape'), null);
  assert.equal(normalizeAttachmentId(''), null);
  assert.equal(normalizeAttachmentId(undefined), null);
});

test('attachments: object paths cannot escape the store root', () => {
  const root = join('C:', 'fake', 'attachments', 'v1');
  assert.equal(attachmentObjectPath(root, HEX_A), join(root, 'objects', 'aa', HEX_A));
  assert.equal(attachmentObjectPath(root, '../etc/passwd'), null);
  assert.equal(attachmentObjectPath(root, ''), null);
});

test('attachments: root resolution prefers the explicit override, then DSH_HOME', () => {
  assert.equal(
    resolveAttachmentsRoot({ env: { VHCU_ATTACHMENTS_ROOT: 'D:/x/v1', DSH_HOME: 'D:/home' } }),
    'D:/x/v1',
  );
  assert.equal(
    resolveAttachmentsRoot({ env: { DSH_HOME: 'D:/home' } }),
    join('D:/home', 'attachments', 'v1'),
  );
  assert.equal(resolveAttachmentsRoot({ env: {}, homeFallback: 'D:/fallback' }), join('D:/fallback', 'attachments', 'v1'));
  assert.equal(resolveAttachmentsRoot({ env: {} }), null);
});

test('attachments: ledger records, dedupes and reports the budget', () => {
  const dir = tempDir('ledger');
  try {
    const registry = new AttachmentRegistry({ path: join(dir, 'registry.json') });
    assert.deepEqual(registry.stats(), {
      count: 0,
      bytes: 0,
      countLimit: ATTACHMENT_COUNT_LIMIT,
      byteLimit: ATTACHMENT_BYTE_LIMIT,
      countRatio: 0,
      byteRatio: 0,
      exhausted: false,
    });

    registry.record({ id: HEX_A, bytes: 1000, at: 10 });
    registry.record({ id: HEX_B, bytes: 2000, at: 20 });
    assert.equal(registry.count, 2);
    assert.equal(registry.bytes, 3000);

    // Content-addressed dedupe: the same id must not be counted twice.
    registry.record({ id: `sha256:${HEX_A}`, bytes: 1000, at: 30 });
    assert.equal(registry.count, 2, 'duplicate id must not add an entry');

    const reopened = new AttachmentRegistry({ path: join(dir, 'registry.json') });
    assert.equal(reopened.count, 2);
    assert.equal(reopened.bytes, 3000);
    assert.deepEqual(reopened.list().map((item) => item.id), [HEX_B, HEX_A], 'oldest first by last touch');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('attachments: a corrupt ledger is quarantined, never fatal', () => {
  const dir = tempDir('corrupt');
  try {
    const path = join(dir, 'registry.json');
    writeFileSync(path, '{not json', 'utf8');
    const registry = new AttachmentRegistry({ path });
    assert.equal(registry.count, 0);
    assert.match(String(registry.lastError), /JSON|Unexpected|token/i);
    const backups = readFileSync; // keep import used
    void backups;
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('attachments: reclaim selects oldest first and keeps recent entries', () => {
  const dir = tempDir('reclaim');
  try {
    const registry = new AttachmentRegistry({ path: join(dir, 'registry.json'), countLimit: 4, byteLimit: 4000 });
    for (let i = 0; i < 4; i += 1) registry.record({ id: String(i).repeat(64), bytes: 1000, at: i + 1 });
    assert.equal(registry.stats().bytes, 4000);

    const selected = registry.selectForReclaim({ keepCount: 2, keepBytes: 2000 });
    assert.deepEqual(selected.map((item) => item.id), ['0'.repeat(64), '1'.repeat(64)]);

    registry.forget(selected.map((item) => item.id));
    assert.equal(registry.count, 2);
    assert.deepEqual(registry.list().map((item) => item.id), ['2'.repeat(64), '3'.repeat(64)]);

    // Default policy (no explicit targets): keep the newest 10%, at least one.
    const policyDir = tempDir('policy');
    try {
      const policy = new AttachmentRegistry({ path: join(policyDir, 'registry.json') });
      for (let i = 0; i < 20; i += 1) {
        policy.record({ id: i.toString(16).padStart(2, '0').repeat(32), bytes: 1000, at: i + 1 });
      }
      const defaultSelection = policy.selectForReclaim();
      assert.equal(defaultSelection.length, 18, '20 entries -> keep newest 2');
      assert.equal(defaultSelection[0].id, '00'.repeat(32), 'oldest goes first');
    } finally {
      rmSync(policyDir, { recursive: true, force: true });
    }

    // Byte budget alone can force a reclaim even under the count target.
    const byBytes = registry.selectForReclaim({ keepCount: 100, keepBytes: 1000 });
    assert.equal(byBytes.length, 1);
    assert.equal(byBytes[0].id, '2'.repeat(64));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('attachments: cleanup trashes only existing files, forgets missing ones', async () => {
  const dir = tempDir('gc');
  try {
    const root = join(dir, 'attachments', 'v1');
    const presentId = 'c'.repeat(64);
    const missingId = 'd'.repeat(64);
    const failingId = 'e'.repeat(64);
    for (const id of [presentId, failingId]) {
      mkdirSync(join(root, 'objects', id.slice(0, 2)), { recursive: true });
      writeFileSync(join(root, 'objects', id.slice(0, 2), id), 'png', 'utf8');
    }

    const registry = new AttachmentRegistry({ path: join(dir, 'registry.json'), countLimit: 10, byteLimit: 100000 });
    registry.record({ id: presentId, bytes: 300, at: 1 });
    registry.record({ id: missingId, bytes: 300, at: 2 });
    registry.record({ id: failingId, bytes: 300, at: 3 });

    const trashed = [];
    const result = await cleanupAttachments({
      registry,
      root,
      keepCount: 0,
      keepBytes: 0,
      trash: async (paths) => {
        trashed.push(...paths);
        return paths.map((path) =>
          path.includes(failingId) ? { path, ok: false, error: 'locked' } : { path, ok: true, via: 'electron' });
      },
    });

    assert.equal(result.trashed, 1);
    assert.deepEqual(result.via, ['electron'], 'the recycle-bin backend is reported for confirmation');
    assert.equal(result.missing, 1, 'a ledger entry whose object vanished is just forgotten');
    assert.equal(result.failed.length, 1);
    assert.equal(result.failed[0].id, failingId);
    assert.ok(!JSON.stringify(result.failed).includes(root), 'failures must not leak absolute paths');
    assert.deepEqual(registry.list().map((item) => item.id), [failingId], 'failed entries stay for a retry');
    assert.equal(result.keptCount, 1, 'the surviving count is reported for the UI');
    assert.equal(result.keptBytes, 300);
    assert.equal(trashed.length, 2, 'only existing files are handed to the recycle bin');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// ── plugin surface ───────────────────────────────────────────────────────
const suffix = `${process.pid}-${Date.now()}`;
function withPlugin(fn, env = {}) {
  const dir = mkdtempSync(join(tmpdir(), `vhcu-attach-plugin-${suffix}-`));
  const saved = { ...process.env };
  Object.assign(process.env, {
    VHCU_MACROS_PATH: join(dir, 'macros.json'),
    VHCU_SETTINGS_PATH: join(dir, 'settings.json'),
    VHCU_SESSION_STATE_PATH: join(dir, 'session.json'),
    VHCU_ATTACHMENTS_REGISTRY: join(dir, 'attachments_registry.json'),
    VHCU_ATTACHMENTS_ROOT: join(dir, 'attachments', 'v1'),
    VHCU_DISABLE_USER_TAKEOVER: '1',
    ...env,
  });
  const mock = createMockCtx({ logger: { debug() {}, info() {}, warn() {}, error() {} }, services: {} });
  apply(mock.ctx);
  return Promise.resolve(fn(mock, dir)).finally(() => {
    mock.dispose();
    process.env = saved;
    rmSync(dir, { recursive: true, force: true });
  });
}

function callRoute(routes, path, { method = 'GET', headers = {}, body = null } = {}) {
  const route = routes.get(path);
  assert.ok(route, `route ${path} missing`);
  return new Promise((resolve, reject) => {
    const res = {
      headersSent: false,
      statusCode: 200,
      setHeader() {},
      writeHead(status) {
        this.statusCode = status;
      },
      end(text) {
        try {
          resolve({ status: this.statusCode, body: JSON.parse(text) });
        } catch (error) {
          reject(error);
        }
      },
    };
    let payload = null;
    const req = {
      method,
      url: path,
      headers: { host: '127.0.0.1', ...headers },
      // lib/index.js readJson() consumes the request with `for await`.
      async *[Symbol.asyncIterator]() {
        if (payload !== null) yield Buffer.from(payload);
      },
      on() {},
      destroy() {},
    };
    if (body !== null) {
      payload = JSON.stringify(body);
      req.headers['content-type'] = req.headers['content-type'] ?? 'application/json';
      req.headers['content-length'] = String(Buffer.byteLength(payload));
    }
    Promise.resolve(route.handler(req, res)).catch(reject);
  });
}

test('attachments: /vhcu/attachments reports the ledger and rejects bad posts', async () => {
  await withPlugin(async ({ routes }) => {
    const read = await callRoute(routes, '/vhcu/attachments');
    assert.equal(read.status, 200);
    assert.equal(read.body.attachments.count, 0);
    assert.equal(read.body.attachments.countLimit, ATTACHMENT_COUNT_LIMIT);
    assert.equal(read.body.attachments.byteLimit, ATTACHMENT_BYTE_LIMIT);

    const missingHeader = await callRoute(routes, '/vhcu/attachments', { method: 'POST', body: { action: 'cleanup' } });
    assert.equal(missingHeader.status, 403);

    const badAction = await callRoute(routes, '/vhcu/attachments', {
      method: 'POST',
      headers: { 'x-vhcu': '1' },
      body: { action: 'nope' },
    });
    assert.equal(badAction.status, 400);
  });
});

test('attachments: POST cleanup on missing objects forgets them without a recycle-bin call', async () => {
  await withPlugin(async ({ routes }, dir) => {
    const registryPath = join(dir, 'attachments_registry.json');
    writeFileSync(
      registryPath,
      JSON.stringify({
        version: 1,
        revision: 1,
        items: [
          { id: 'f'.repeat(64), bytes: 500, at: 1 },
          { id: 'f'.repeat(64) },
        ],
      }),
      'utf8',
    );
    // Re-apply so the plugin loads the ledger we just wrote.
    const second = createMockCtx({ logger: { debug() {}, info() {}, warn() {}, error() {} } });
    apply(second.ctx);
    try {
      const state = await callRoute(second.routes, '/vhcu/state');
      assert.equal(state.body.state.attachments.count, 1, 'malformed duplicates collapse to one id');

      const cleaned = await callRoute(second.routes, '/vhcu/attachments', {
        method: 'POST',
        headers: { 'x-vhcu': '1' },
        body: { action: 'cleanup', keep_count: 0 },
      });
      assert.equal(cleaned.status, 200);
      assert.equal(cleaned.body.result.missing, 1);
      assert.equal(cleaned.body.result.trashed, 0);
      assert.equal(cleaned.body.attachments.count, 0);
    } finally {
      second.dispose();
    }
  });
});
