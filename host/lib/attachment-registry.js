/**
 * @file attachment-registry.js — VHCU's own ledger of attachments it created.
 *
 * Why a ledger: the host attachment store is content-addressed and explicitly
 * never auto-deletes (its README says so), and the service exposes no delete or
 * list API. We cannot enumerate the store safely — sessions are zstd-compressed
 * and other components share the same objects. So VHCU remembers exactly what
 * IT wrote, and the GC only ever touches those ids.
 *
 * Ledger fields per item: sha256 id (hex, no prefix), stored byte size, and the
 * moment VHCU created the reference. Oldest first.
 */

import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

/** Hard budgets (user decision 2026-11-28): stop generating at either limit. */
export const ATTACHMENT_COUNT_LIMIT = 2000;
export const ATTACHMENT_BYTE_LIMIT = 500 * 1024 * 1024;

/** After an automatic reclaim, keep this share of the budget as headroom. */
export const RECLAIM_KEEP_RATIO = 0.5;

/**
 * A user-initiated cleanup keeps the newest N attachments (≈90 MB with the
 * filtered PNG encoder) and moves everything older to the recycle bin, so the
 * button always has a bounded, explainable effect instead of "delete all".
 */
export const MANUAL_KEEP_COUNT = 200;

const ID_PATTERN = /^[a-f0-9]{64}$/;

/** Strip the `sha256:` prefix the host refs carry; lowercase hex only. */
export function normalizeAttachmentId(value) {
  const raw = String(value ?? '').trim().toLowerCase();
  const hex = raw.startsWith('sha256:') ? raw.slice('sha256:'.length) : raw;
  return ID_PATTERN.test(hex) ? hex : null;
}

/**
 * Store object path for one id. The host layout is `<root>/objects/<2>/<rest>`
 * where root is `<DSH_HOME>/attachments/v1`. Ids that are not 64 hex chars are
 * rejected here, so a corrupt ledger can never escape the directory.
 */
export function attachmentObjectPath(root, id) {
  const hex = normalizeAttachmentId(id);
  if (!hex) return null;
  return join(root, 'objects', hex.slice(0, 2), hex);
}

/** Resolve the attachment root without trusting VHCU env paths (absolute only). */
export function resolveAttachmentsRoot({ env = process.env, homeFallback } = {}) {
  const explicit = String(env.VHCU_ATTACHMENTS_ROOT ?? '').trim();
  if (explicit) return explicit;
  const home = String(env.DSH_HOME ?? '').trim() || homeFallback;
  if (!home) return null;
  return join(home, 'attachments', 'v1');
}

export class AttachmentRegistry {
  /**
   * @param {{path: string, countLimit?: number, byteLimit?: number, now?: () => number}} options
   */
  constructor({ path, countLimit = ATTACHMENT_COUNT_LIMIT, byteLimit = ATTACHMENT_BYTE_LIMIT, now = Date.now } = {}) {
    if (!path) throw new TypeError('AttachmentRegistry requires a path');
    this.path = path;
    this.countLimit = countLimit;
    this.byteLimit = byteLimit;
    this.now = now;
    this.revision = 0;
    this.lastError = null;
    this.items = [];
    this.load();
  }

  load() {
    this.items = [];
    this.lastError = null;
    if (!existsSync(this.path)) {
      this.revision = 0;
      return;
    }
    try {
      const parsed = JSON.parse(readFileSync(this.path, 'utf8'));
      const raw = Array.isArray(parsed?.items) ? parsed.items : [];
      this.items = raw
        .map((entry) => {
          const id = normalizeAttachmentId(entry?.id);
          if (!id) return null;
          const bytes = Number(entry?.bytes);
          const at = Number(entry?.at);
          return {
            id,
            bytes: Number.isFinite(bytes) && bytes > 0 ? Math.trunc(bytes) : 0,
            at: Number.isFinite(at) && at > 0 ? Math.trunc(at) : 0,
          };
        })
        .filter(Boolean)
        // Dedupe defensively: ids are content-addressed, so a hand-edited or
        // partially-written ledger may repeat one. Keep the most recent record.
        .reduce((acc, item) => {
          const existing = acc.get(item.id);
          if (!existing || item.at >= existing.at) acc.set(item.id, item);
          return acc;
        }, new Map())
        .values();
      this.items = [...this.items]
        // oldest first, stable even for identical timestamps
        .sort((a, b) => a.at - b.at)
        .slice(-this.countLimit);
      this.revision = Number(parsed?.revision) || 0;
    } catch (error) {
      // A corrupt ledger must never break capture: quarantine and start clean.
      this.lastError = error?.message ?? String(error);
      try {
        renameSync(this.path, `${this.path}.corrupt-${Date.now()}`);
      } catch {
        // best effort
      }
      this.items = [];
      this.revision = 0;
    }
  }

  get count() {
    return this.items.length;
  }

  get bytes() {
    return this.items.reduce((sum, item) => sum + item.bytes, 0);
  }

  stats() {
    const count = this.count;
    const bytes = this.bytes;
    const exhausted = count >= this.countLimit || bytes >= this.byteLimit;
    return {
      count,
      bytes,
      countLimit: this.countLimit,
      byteLimit: this.byteLimit,
      countRatio: this.countLimit > 0 ? Number((count / this.countLimit).toFixed(4)) : 0,
      byteRatio: this.byteLimit > 0 ? Number((bytes / this.byteLimit).toFixed(4)) : 0,
      exhausted,
    };
  }

  /** Record one saved attachment. Re-recording the same id (host dedupe) counts once. */
  record({ id, bytes, at } = {}) {
    const hex = normalizeAttachmentId(id);
    if (!hex) return { ok: false, reason: 'bad-id' };
    const size = Number(bytes);
    const stamp = Number(at);
    const existing = this.items.find((item) => item.id === hex);
    if (existing) {
      existing.bytes = Number.isFinite(size) && size > 0 ? Math.trunc(size) : existing.bytes;
      existing.at = Number.isFinite(stamp) && stamp > 0 ? Math.trunc(stamp) : this.now();
    } else {
      this.items.push({
        id: hex,
        bytes: Number.isFinite(size) && size > 0 ? Math.trunc(size) : 0,
        at: Number.isFinite(stamp) && stamp > 0 ? Math.trunc(stamp) : this.now(),
      });
      if (this.items.length > this.countLimit) this.items = this.items.slice(-this.countLimit);
    }
    this.items.sort((a, b) => a.at - b.at);
    this.save();
    return { ok: true };
  }

  list() {
    return this.items.map((item) => ({ ...item }));
  }

  /**
   * Which entries the GC may move to the recycle bin: the oldest ones, until the
   * remaining set fits inside the keep budget. Never returns the newest entries
   * while older ones exist.
   */
  selectForReclaim({ keepCount, keepBytes } = {}) {
    const countTarget = Number.isFinite(Number(keepCount))
      ? Math.max(0, Math.trunc(Number(keepCount)))
      : Math.floor(this.countLimit * RECLAIM_KEEP_RATIO);
    const byteTarget = Number.isFinite(Number(keepBytes))
      ? Math.max(0, Math.trunc(Number(keepBytes)))
      : Math.floor(this.byteLimit * RECLAIM_KEEP_RATIO);

    const selected = [];
    let count = this.items.length;
    let bytes = this.bytes;
    for (const item of this.items) { // oldest first
      if (count <= countTarget && bytes <= byteTarget) break;
      selected.push({ ...item });
      count -= 1;
      bytes -= item.bytes;
    }
    return selected;
  }

  /** Drop ids we successfully moved to the recycle bin. */
  forget(ids = []) {
    const drop = new Set(
      (Array.isArray(ids) ? ids : [])
        .map((id) => normalizeAttachmentId(id))
        .filter(Boolean),
    );
    if (drop.size === 0) return 0;
    const before = this.items.length;
    this.items = this.items.filter((item) => !drop.has(item.id));
    const removed = before - this.items.length;
    if (removed > 0) this.save();
    return removed;
  }

  save() {
    const payload = {
      version: 1,
      countLimit: this.countLimit,
      byteLimit: this.byteLimit,
      revision: this.revision + 1,
      items: this.items,
    };
    const text = `${JSON.stringify(payload, null, 2)}\n`;
    const dir = dirname(this.path);
    mkdirSync(dir, { recursive: true });
    const temp = `${this.path}.tmp-${process.pid}-${Date.now()}`;
    try {
      writeFileSync(temp, text, 'utf8');
      renameSync(temp, this.path);
      this.revision += 1;
      this.lastError = null;
    } catch (error) {
      try {
        rmSync(temp, { force: true });
      } catch {
        // ignore
      }
      // Persistence failure must be visible, never silently swallowed.
      this.lastError = error?.message ?? String(error);
      throw error;
    }
  }
}
