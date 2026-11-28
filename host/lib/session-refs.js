/**
 * @file session-refs.js — which attachments are still referenced by session logs?
 *
 * The host attachment store is append-only and history keeps `ImageAttachmentRef`s
 * (id + media type + bytes + dimensions). Deleting an object that a session still
 * references makes that session unreadable: the next model request projects the
 * historical image block, `readImageFile` throws `ATTACHMENT_NOT_FOUND`, and the
 * provider wraps every non-LlmError into `TRANSPORT` ("transport failed") with
 * retries that can never succeed.
 *
 * Therefore VHCU only reclaims objects that no session references. This module
 * reads the session logs and returns that reference set.
 *
 * Format: `session.v4.jsonl.zstd` is a concatenation of independent zstd frames,
 * one per JSONL line. We split on the zstd magic and decode each frame; a false
 * magic inside a payload only splits one frame, which the merge retry repairs.
 *
 * Safety: a partial scan must never be treated as "nothing is referenced". Any
 * decode failure, read error, or budget exhaustion marks the scan incomplete and
 * the cleanup route refuses to delete anything (`E_ATTACHMENT_REFS_UNVERIFIED`).
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { Worker } from 'node:worker_threads';
import * as zlib from 'node:zlib';

const ZSTD_MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]);
const ATTACHMENT_ID_RE = /"attachmentId"\s*:\s*"(?:sha256:)?([a-f0-9]{64})"/gi;

/** Default scan envelope. Generous for a manual cleanup, bounded for the host. */
export const SESSION_SCAN_BUDGET = Object.freeze({
  maxFiles: 4000,
  maxBytes: 512 * 1024 * 1024,
  maxFileBytes: 128 * 1024 * 1024,
  timeoutMs: 20000,
  mergeFrames: 8,
});

/** path -> {mtimeMs, size, ids: string[]} — unchanged files are not decoded twice. */
const scanCache = new Map();
const CACHE_LIMIT = 4000;

/** Resolve the DSH session-log root without trusting variables the host may not set. */
export function resolveSessionsRoot({ env = process.env, homeFallback } = {}) {
  const explicit = String(env.VHCU_SESSIONS_ROOT ?? '').trim();
  if (explicit) return explicit;
  const dshHome = String(env.DSH_HOME ?? '').trim();
  if (dshHome) return join(dshHome, 'sessions');
  const home = String(homeFallback ?? '').trim() || String(homedir() ?? '').trim();
  if (!home) return null;
  return join(home, '.dsh', 'sessions');
}

function listSessionFiles(root) {
  const found = [];
  const walk = (dir, depth) => {
    if (depth > 6) return;
    let entries;
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const full = join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(full, depth + 1);
      } else if (entry.isFile() && /^session\..*\.jsonl\.zstd$/i.test(entry.name)) {
        let info;
        try {
          info = statSync(full);
        } catch {
          continue;
        }
        found.push({ path: full, size: info.size, mtimeMs: info.mtimeMs });
      }
    }
  };
  walk(root, 0);
  found.sort((a, b) => b.mtimeMs - a.mtimeMs);
  return found;
}

/**
 * One decoded segment must be non-empty printable text. Frame boundaries are
 * writer flush boundaries, not line boundaries (one frame can hold several
 * JSONL records, or split one), so per-frame JSON parsing would be wrong.
 */
function usableFrameText(text) {
  const value = String(text ?? '').replace(/\u0000/g, '');
  return value.trim().length > 0;
}

/** Does the concatenated log contain at least one real JSONL record? */
export function looksLikeJsonl(text) {
  const lines = String(text ?? '').split('\n');
  let checked = 0;
  for (const line of lines) {
    const trimmed = line.replace(/^\uFEFF/, '').trim();
    if (trimmed.length < 2 || trimmed[0] !== '{') continue;
    checked += 1;
    try {
      JSON.parse(trimmed);
      return true;
    } catch {
      // Partial lines are normal at frame boundaries; keep looking.
    }
    if (checked > 200) break;
  }
  return false;
}

/**
 * Decode every zstd frame in one buffer and concatenate the text.
 * @returns {{text: string, frames: number, failedFrames: number}}
 */
export function decodeZstdFrames(buf, { zstd = zlib.zstdDecompressSync, mergeFrames = SESSION_SCAN_BUDGET.mergeFrames } = {}) {
  const offsets = [];
  let cursor = 0;
  while (true) {
    const at = buf.indexOf(ZSTD_MAGIC, cursor);
    if (at < 0) break;
    offsets.push(at);
    cursor = at + ZSTD_MAGIC.length;
  }
  if (offsets.length === 0) return { text: '', frames: 0, failedFrames: 0 };

  const parts = [];
  let failedFrames = 0;
  let index = 0;
  while (index < offsets.length) {
    let consumed = 0;
    for (let span = 1; span <= mergeFrames && index + span <= offsets.length; span += 1) {
      const start = offsets[index];
      const end = index + span < offsets.length ? offsets[index + span] : buf.length;
      let text;
      try {
        text = zstd(buf.subarray(start, end)).toString('utf8');
      } catch {
        // A false magic split one frame; retry with a wider span.
        continue;
      }
      if (!usableFrameText(text)) continue; // empty/truncated decode: widen and retry
      parts.push(text);
      consumed = span;
      break;
    }
    if (consumed === 0) {
      failedFrames += 1;
      index += 1;
    } else {
      index += consumed;
    }
  }
  return { text: parts.join(''), frames: offsets.length, failedFrames };
}

/** Extract every `attachmentId` from one text blob. */
export function extractAttachmentIds(text) {
  const ids = new Set();
  ATTACHMENT_ID_RE.lastIndex = 0;
  let match;
  while ((match = ATTACHMENT_ID_RE.exec(text)) !== null) ids.add(match[1].toLowerCase());
  return ids;
}

/**
 * Collect the set of attachment ids referenced by every session log.
 *
 * @param {{
 *   root?: string | null,
 *   budget?: Partial<typeof SESSION_SCAN_BUDGET>,
 *   zstd?: Function | null,
 *   now?: () => number,
 * }} options
 * @returns {Promise<{
 *   ok: boolean,
 *   reason?: string,
 *   ids: Set<string>,
 *   files: number,
 *   bytes: number,
 *   cached: number,
 *   startedAt: number,
 *   elapsedMs: number,
 * }>}
 */
export async function collectReferencedAttachmentIds({ root, budget = {}, zstd = zlib.zstdDecompressSync, now = Date.now } = {}) {
  const ids = new Set();
  const startedAt = now();
  const limits = { ...SESSION_SCAN_BUDGET, ...budget };
  const result = (ok, reason, extra = {}) => ({
    ok,
    reason,
    ids,
    files: 0,
    bytes: 0,
    cached: 0,
    startedAt,
    elapsedMs: now() - startedAt,
    ...extra,
  });

  if (!root) return result(false, 'no-sessions-root');
  if (typeof zstd !== 'function') return result(false, 'zstd-unavailable');

  const files = listSessionFiles(root);
  let bytes = 0;
  let cached = 0;
  let incomplete = false;

  for (const file of files) {
    if (now() - startedAt > limits.timeoutMs) {
      incomplete = true;
      break;
    }
    if (bytes + file.size > limits.maxBytes || file.size > limits.maxFileBytes) {
      incomplete = true;
      continue;
    }
    const previous = scanCache.get(file.path);
    if (previous && previous.mtimeMs === file.mtimeMs && previous.size === file.size) {
      for (const id of previous.ids) ids.add(id);
      cached += 1;
      bytes += file.size;
      continue;
    }
    let buffer;
    try {
      buffer = readFileSync(file.path);
    } catch {
      incomplete = true;
      continue;
    }
    const decoded = decodeZstdFrames(buffer, { zstd, mergeFrames: limits.mergeFrames });
    if (decoded.failedFrames > 0 || !looksLikeJsonl(decoded.text)) {
      incomplete = true;
      continue;
    }
    const fileIds = [...extractAttachmentIds(decoded.text)];
    for (const id of fileIds) ids.add(id);
    if (scanCache.size >= CACHE_LIMIT) scanCache.clear();
    scanCache.set(file.path, { mtimeMs: file.mtimeMs, size: file.size, ids: fileIds });
    bytes += file.size;
    // Yield so a long scan never monopolises the host event loop.
    await new Promise((resolve) => setImmediate(resolve));
  }

  const seen = files.length;
  if (seen > 0 && bytes === 0) incomplete = true; // nothing readable at all
  return {
    ok: !incomplete,
    reason: incomplete ? 'scan-incomplete' : undefined,
    ids,
    files: seen,
    bytes,
    cached,
    startedAt,
    elapsedMs: now() - startedAt,
  };
}

/** Test/ops hook: forget cached session digests. */
export function clearSessionRefCache() {
  scanCache.clear();
}

function runScanWorker({ files, budget }) {
  return new Promise((resolve) => {
    let worker = null;
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      try {
        worker?.terminate();
      } catch {
        // ignore
      }
      resolve(value);
    };
    try {
      worker = new Worker(new URL('./session-scan-worker.js', import.meta.url), {
        workerData: { files, maxFileBytes: budget.maxFileBytes },
      });
    } catch {
      resolve(null);
      return;
    }
    const timer = setTimeout(() => finish(null), budget.timeoutMs + 5000);
    worker.on('message', (message) => {
      clearTimeout(timer);
      finish(message);
    });
    worker.on('error', () => {
      clearTimeout(timer);
      finish(null);
    });
    worker.on('exit', () => {
      if (!settled) {
        clearTimeout(timer);
        finish(null);
      }
    });
  });
}

/**
 * Worker-backed variant used by the cleanup route: the host event loop stays
 * responsive while session logs are decoded, and the per-file cache lives in the
 * caller thread so repeat cleanups only pay for changed logs.
 *
 * @returns {Promise<ReturnType<typeof collectReferencedAttachmentIds> extends Promise<infer T> ? T : never>}
 */
export async function collectReferencedAttachmentIdsAsync({ root, budget = {}, now = Date.now } = {}) {
  const startedAt = now();
  const limits = { ...SESSION_SCAN_BUDGET, ...budget };
  const ids = new Set();
  const elapsed = () => now() - startedAt;
  const make = (ok, reason, extra = {}) => ({
    ok,
    reason,
    ids,
    files: 0,
    bytes: 0,
    cached: 0,
    startedAt,
    elapsedMs: elapsed(),
    ...extra,
  });

  if (!root) return make(false, 'no-sessions-root');
  if (typeof zlib.zstdDecompressSync !== 'function') return make(false, 'zstd-unavailable');

  const files = listSessionFiles(root);
  if (files.length === 0) return make(true, undefined, { files: 0 });

  const sizeByPath = new Map(files.map((file) => [file.path, file.size]));
  let bytes = 0;
  let cached = 0;
  const changed = [];
  for (const file of files) {
    if (file.size > limits.maxFileBytes) {
      changed.push(file);
      continue;
    }
    const previous = scanCache.get(file.path);
    if (previous && previous.mtimeMs === file.mtimeMs && previous.size === file.size) {
      for (const id of previous.ids) ids.add(id);
      cached += 1;
      bytes += file.size;
    } else {
      changed.push(file);
    }
  }
  if (changed.length === 0) return make(true, undefined, { files: files.length, cached, bytes });

  const workerResult = await runScanWorker({
    files: changed.map((file) => ({ path: file.path, size: file.size })),
    budget: limits,
  });

  if (workerResult == null) {
    // Worker unavailable: decode inline as a last resort, still bounded per file.
    let failedCount = 0;
    for (const file of changed) {
      if (file.size > limits.maxFileBytes) {
        failedCount += 1;
        continue;
      }
      let buffer;
      try {
        buffer = readFileSync(file.path);
      } catch {
        failedCount += 1;
        continue;
      }
      const decoded = decodeZstdFrames(buffer);
      if (decoded.failedFrames > 0 || !looksLikeJsonl(decoded.text)) {
        failedCount += 1;
        continue;
      }
      const fileIds = [...extractAttachmentIds(decoded.text)];
      for (const id of fileIds) ids.add(id);
      if (scanCache.size >= CACHE_LIMIT) scanCache.clear();
      scanCache.set(file.path, { mtimeMs: file.mtimeMs, size: file.size, ids: fileIds });
      bytes += file.size;
    }
    return make(failedCount === 0, failedCount === 0 ? undefined : 'scan-incomplete', {
      files: files.length,
      cached,
      bytes,
    });
  }

  for (const item of workerResult.perFile ?? []) {
    const size = sizeByPath.get(item.path) ?? 0;
    if (scanCache.size >= CACHE_LIMIT) scanCache.clear();
    const source = changed.find((file) => file.path === item.path);
    scanCache.set(item.path, { mtimeMs: source?.mtimeMs ?? 0, size, ids: item.ids });
    for (const id of item.ids) ids.add(id);
    bytes += size;
  }
  const failedCount = (workerResult.failed ?? []).length;
  return make(failedCount === 0, failedCount === 0 ? undefined : 'scan-incomplete', {
    files: files.length,
    cached,
    bytes,
  });
}
