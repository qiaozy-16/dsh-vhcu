/**
 * @file session-refs.test.mjs — session-log attachment reference scanning.
 *
 * Cleanup may only reclaim objects no session references, so the scanner is the
 * safety boundary. These tests pin:
 *   - multi-frame zstd decoding (DSH stores one frame per JSONL line);
 *   - id extraction and cache reuse;
 *   - fail-closed behaviour: no zstd, unreadable frames, or budget exhaustion
 *     must report `ok: false`, never an empty (and therefore "safe") set.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import * as zlib from 'node:zlib';

import {
  collectReferencedAttachmentIds,
  decodeZstdFrames,
  extractAttachmentIds,
  resolveSessionsRoot,
} from '../lib/session-refs.js';

const HEX_A = 'a'.repeat(64);
const HEX_B = 'b'.repeat(64);

function tempDir(label) {
  return mkdtempSync(join(tmpdir(), `vhcu-refs-${label}-`));
}

function frame(text) {
  return zlib.zstdCompressSync(Buffer.from(text, 'utf8'));
}

test('session-refs: DSH session files are concatenated zstd frames', () => {
  if (typeof zlib.zstdCompressSync !== 'function') return;
  const lineOne = JSON.stringify({ type: 'tool/result', data: { attachmentId: `sha256:${HEX_A}` } });
  const lineTwo = JSON.stringify({ type: 'user/message', data: { attachmentId: HEX_B } });
  const buffer = Buffer.concat([frame(lineOne), frame(lineTwo)]);
  const decoded = decodeZstdFrames(buffer);
  assert.equal(decoded.frames, 2);
  assert.equal(decoded.failedFrames, 0);
  assert.match(decoded.text, new RegExp(HEX_A));
  assert.match(decoded.text, new RegExp(HEX_B));
});

test('session-refs: ids are extracted only from attachmentId fields', () => {
  const text = [
    JSON.stringify({ attachmentId: `sha256:${HEX_A}` }),
    JSON.stringify({ image: { attachment: { attachmentId: HEX_B } } }),
    JSON.stringify({ note: 'a bare 64-hex blob must not count: ' + 'c'.repeat(64) }),
  ].join('\n');
  const ids = [...extractAttachmentIds(text)];
  assert.deepEqual(ids.sort(), [HEX_A, HEX_B].sort());
});

test('session-refs: the root honours the explicit override, then DSH_HOME, then ~/.dsh', () => {
  assert.equal(
    resolveSessionsRoot({ env: { VHCU_SESSIONS_ROOT: 'D:/logs' } }),
    'D:/logs',
  );
  assert.equal(resolveSessionsRoot({ env: { DSH_HOME: 'D:/home' } }), join('D:/home', 'sessions'));
  assert.equal(resolveSessionsRoot({ env: {}, homeFallback: 'C:/Users/x' }), join('C:/Users/x', '.dsh', 'sessions'));
});

test('session-refs: a scan collects referenced ids and reuses the per-file cache', async () => {
  if (typeof zlib.zstdCompressSync !== 'function') return;
  const dir = tempDir('collect');
  try {
    mkdirSync(join(dir, 'ws', 'session-a'), { recursive: true });
    const file = join(dir, 'ws', 'session-a', 'session.v4.jsonl.zstd');
    writeFileSync(file, frame(JSON.stringify({ attachmentId: `sha256:${HEX_A}` }) + '\n'));

    const first = await collectReferencedAttachmentIds({ root: dir });
    assert.equal(first.ok, true);
    assert.equal(first.files, 1);
    assert.equal(first.ids.has(HEX_A), true);
    assert.equal(first.cached, 0);

    const second = await collectReferencedAttachmentIds({ root: dir });
    assert.equal(second.ok, true);
    assert.equal(second.cached, 1, 'unchanged files are not decoded twice');
    assert.equal(second.ids.has(HEX_A), true);

    // Append a different id: the size changes, so the digest cache must refresh.
    writeFileSync(
      file,
      Buffer.concat([
        frame(JSON.stringify({ attachmentId: `sha256:${HEX_A}` }) + '\n'),
        frame(JSON.stringify({ attachmentId: HEX_B }) + '\n'),
      ]),
    );
    const third = await collectReferencedAttachmentIds({ root: dir });
    assert.equal(third.ok, true);
    assert.equal(third.cached, 0);
    assert.equal(third.ids.has(HEX_A), true);
    assert.equal(third.ids.has(HEX_B), true);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('session-refs: fail-closed without zstd, on corrupt frames, or past the budget', async () => {
  const dir = tempDir('fail');
  try {
    assert.equal((await collectReferencedAttachmentIds({ root: dir, zstd: null })).reason, 'zstd-unavailable');

    mkdirSync(join(dir, 'ws', 'bad'), { recursive: true });
    writeFileSync(join(dir, 'ws', 'bad', 'session.v4.jsonl.zstd'), Buffer.from([0x28, 0xb5, 0x2f, 0xfd, 1, 2, 3]));
    const corrupt = await collectReferencedAttachmentIds({ root: dir });
    assert.equal(corrupt.ok, false, 'a corrupt frame must not be treated as "no references"');
    assert.equal(corrupt.reason, 'scan-incomplete');

    const empty = await collectReferencedAttachmentIds({ root: join(dir, 'missing-root') });
    assert.equal(empty.ok, true, 'a fresh install has no sessions; that is a complete empty scan');
    assert.equal(empty.files, 0);

    let clock = 0;
    const timedOut = await collectReferencedAttachmentIds({
      root: dir,
      budget: { timeoutMs: 1 },
      now: () => (clock += 1000),
    });
    assert.equal(timedOut.ok, false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
