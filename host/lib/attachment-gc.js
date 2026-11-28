/**
 * @file attachment-gc.js — reclaim VHCU's own attachments into the recycle bin.
 *
 * Safety rules (non-negotiable):
 *   - never enumerate or touch the attachment store in general; only ids present
 *     in VHCU's ledger are candidates;
 *   - ids are validated as 64 hex chars before a path is built (no traversal);
 *   - deletions go through the OS recycle bin, never `unlink`;
 *   - failed items stay in the ledger so a later run can retry;
 *   - the newest entries are never reclaimed while older ones exist.
 *
 * The trash function is injectable so tests can exercise the policy without
 * touching a real recycle bin.
 */

import { existsSync } from 'node:fs';

import { attachmentObjectPath } from './attachment-registry.js';
import { trashFiles } from './trash.js';

/**
 * @param {{
 *   registry: import('./attachment-registry.js').AttachmentRegistry,
 *   root: string,
 *   trash?: (paths: string[]) => Promise<Array<{path: string, ok: boolean, error?: string}>>,
 *   keepCount?: number,
 *   keepBytes?: number,
 *   logger?: {warn?: Function},
 * }} options
 */
export async function cleanupAttachments({ registry, root, trash = trashFiles, keepCount, keepBytes, logger } = {}) {
  if (!registry) throw new TypeError('cleanupAttachments requires a registry');
  if (!root) throw new TypeError('cleanupAttachments requires the attachment root');

  const selected = registry.selectForReclaim({ keepCount, keepBytes });
  const candidates = selected.map((item) => ({
    ...item,
    path: attachmentObjectPath(root, item.id),
  }));
  // Ledger entries whose object already vanished are simply forgotten.
  const missing = candidates.filter((item) => !item.path || !existsSync(item.path));
  const present = candidates.filter((item) => item.path && existsSync(item.path));

  let results = [];
  if (present.length > 0) {
    try {
      results = await trash(present.map((item) => item.path));
    } catch (error) {
      logger?.warn?.('vhcu: recycle-bin batch failed', error?.message ?? String(error));
      results = present.map((item) => ({ path: item.path, ok: false, error: error?.message ?? String(error) }));
    }
  }

  // A recycle-bin call that reports success but leaves the file in place is a
  // failure, not a freed byte. The verified post-condition is what protects the
  // ledger: forgetting an entry whose object is still on disk orphans it forever.
  const stillPresent = [];
  const okPaths = new Set();
  for (const entry of results) {
    if (!entry?.ok || !entry.path) continue;
    if (existsSync(entry.path)) {
      stillPresent.push(entry.path);
      continue;
    }
    okPaths.add(entry.path);
  }
  // Which recycle-bin backend actually handled the batch ('electron' / 'powershell').
  // Only reported for files that are really gone, not for claimed successes.
  const via = [
    ...new Set(
      results
        .filter((entry) => entry?.ok && entry.via && okPaths.has(entry.path))
        .map((entry) => String(entry.via)),
    ),
  ];
  const trashed = present.filter((item) => okPaths.has(item.path));
  const byPath = new Map(present.map((item) => [item.path, item]));
  const failed = [
    ...results
      .filter((entry) => !entry?.ok)
      .map((entry) => ({ path: entry?.path, error: String(entry?.error ?? 'unknown') })),
    ...stillPresent.map((path) => ({ path, error: 'still present after the recycle-bin call' })),
  ].map((entry) => ({
    // Do not leak absolute paths to the UI; report the id and the reason.
    id: byPath.get(entry.path)?.id ?? null,
    error: entry.error.slice(0, 200),
  }));

  const forgetIds = [...trashed.map((item) => item.id), ...missing.map((item) => item.id)];
  const forgotten = registry.forget(forgetIds);
  const trashedBytes = trashed.reduce((sum, item) => sum + item.bytes, 0);
  const missingBytes = missing.reduce((sum, item) => sum + item.bytes, 0);
  // `freedBytes` keeps its old meaning (all ledger entries dropped) for callers
  // that already read it; the UI must use `trashedBytes` so a batch of vanished
  // objects can never look like a successful recycle-bin move.
  const freedBytes = trashedBytes + missingBytes;

  return {
    ok: failed.length === 0,
    considered: selected.length,
    trashed: trashed.length,
    missing: missing.length,
    forgotten,
    freedBytes,
    trashedBytes,
    missingBytes,
    via,
    failed,
    keptCount: registry.count,
    keptBytes: registry.bytes,
    stats: registry.stats(),
    revision: registry.revision,
  };
}

export default cleanupAttachments;
