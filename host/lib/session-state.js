/**
 * @file session-state.js — crash-safe macro queue progress.
 *
 * The Node plugin is the single owner of macro queue state. Python only runs
 * one macro at a time, so recovery is macro-level (at-least-once), never
 * step-level. The file is atomically replaced and validated on load; a
 * corrupt/stale file is quarantined and never blocks plugin startup.
 */

import {
  closeSync,
  existsSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  rmSync,
  writeSync,
} from 'node:fs';
import { dirname, resolve } from 'node:path';

export const SESSION_STATE_VERSION = 1;
export const MAX_QUEUE_ITEMS = 20;
export const MAX_AGE_MS = 24 * 60 * 60 * 1000;
const STATUSES = new Set(['running', 'waiting_ack', 'paused', 'recovery_ready']);
const MAX_NAME = 64;
const MAX_VARIABLE_TEXT = 2000;

function integer(value, minimum, maximum) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= minimum && parsed <= maximum ? parsed : null;
}

export function validateSessionState(value, now = Date.now()) {
  if (!value || typeof value !== 'object') return null;
  if (Number(value.version) !== SESSION_STATE_VERSION) return null;
  if (typeof value.queue_id !== 'string' || !/^q_[a-z0-9]{8,64}$/i.test(value.queue_id)) return null;
  if (!STATUSES.has(value.status)) return null;
  const total = integer(value.total, 1, MAX_QUEUE_ITEMS);
  const index = integer(value.current_index, 0, total ?? -1);
  if (total === null || index === null || index > total) return null;
  if (!Array.isArray(value.items) || value.items.length !== total) return null;
  const items = [];
  for (const raw of value.items) {
    if (!raw || typeof raw !== 'object') return null;
    const name = String(raw.name ?? '').trim();
    if (!name || name.length > MAX_NAME) return null;
    const variables = raw.variables && typeof raw.variables === 'object' && !Array.isArray(raw.variables)
      ? Object.fromEntries(
          Object.entries(raw.variables)
            .slice(0, 20)
            .map(([key, entry]) => [String(key).slice(0, 40), String(entry).slice(0, MAX_VARIABLE_TEXT)]),
        )
      : {};
    items.push({
      name,
      variables,
      ...(typeof raw.text === 'string' ? { text: raw.text.slice(0, MAX_VARIABLE_TEXT) } : {}),
    });
  }
  const optionsRaw = value.options && typeof value.options === 'object' ? value.options : {};
  const options = {
    skipReadback: Boolean(optionsRaw.skipReadback),
    imageRelative: Boolean(optionsRaw.imageRelative),
    ...(Number.isFinite(Number(optionsRaw.scaleFactor)) ? { scaleFactor: Number(optionsRaw.scaleFactor) } : {}),
    ...(Number.isFinite(Number(optionsRaw.scaleX)) ? { scaleX: Number(optionsRaw.scaleX) } : {}),
    ...(Number.isFinite(Number(optionsRaw.scaleY)) ? { scaleY: Number(optionsRaw.scaleY) } : {}),
    ...(Number.isFinite(Number(optionsRaw.originX)) ? { originX: Number(optionsRaw.originX) } : {}),
    ...(Number.isFinite(Number(optionsRaw.originY)) ? { originY: Number(optionsRaw.originY) } : {}),
  };
  const createdAt = Number(value.created_at);
  const updatedAt = Number(value.updated_at);
  if (!Number.isFinite(createdAt) || !Number.isFinite(updatedAt)) return null;
  if (updatedAt < createdAt || now - updatedAt > MAX_AGE_MS) return null;
  return {
    version: SESSION_STATE_VERSION,
    queue_id: value.queue_id,
    status: value.status,
    current_index: index,
    total,
    items,
    options,
    created_at: Math.round(createdAt),
    updated_at: Math.round(updatedAt),
  };
}

export class SessionStateStore {
  constructor({ path, now = Date.now } = {}) {
    if (!path) throw new TypeError('SessionStateStore requires a path');
    this.path = resolve(path);
    this.now = now;
    this.state = null;
    this.lastError = null;
    this.load();
  }

  load() {
    this.lastError = null;
    if (!existsSync(this.path)) return null;
    try {
      const raw = JSON.parse(readFileSync(this.path, 'utf8'));
      const validated = validateSessionState(raw, this.now());
      if (!validated) {
        this.lastError = { code: 'E_SESSION_STATE_INVALID', message: 'session_state.json is invalid or stale' };
        this._quarantine();
        return null;
      }
      this.state = validated;
      return this.state;
    } catch (error) {
      this.lastError = {
        code: 'E_SESSION_STATE_LOAD',
        message: error?.message ?? String(error),
      };
      this._quarantine();
      return null;
    }
  }

  save(state, { durable = true } = {}) {
    const validated = validateSessionState(
      { ...state, version: SESSION_STATE_VERSION, updated_at: this.now() },
      this.now(),
    );
    if (!validated) {
      throw Object.assign(new Error('invalid macro session state'), { code: 'E_MACRO_STATE_PERSIST' });
    }
    const directory = dirname(this.path);
    mkdirSync(directory, { recursive: true });
    const temp = `${this.path}.${process.pid}.${Date.now()}.tmp`;
    const body = `${JSON.stringify(validated, null, 2)}\n`;
    try {
      const fd = openSync(temp, 'w', 0o600);
      try {
        writeSync(fd, body, null, 'utf8');
        // Progress writes rely on the atomic rename (process-crash safe).
        // fsync is reserved for callers that explicitly need power-loss
        // durability; doing it for every macro made queue latency disk-bound.
        if (durable) fsyncSync(fd);
      } finally {
        closeSync(fd);
      }
      renameSync(temp, this.path);
      this.state = validated;
      this.lastError = null;
      return validated;
    } catch (error) {
      try {
        rmSync(temp, { force: true });
      } catch {
        // best effort
      }
      throw Object.assign(
        new Error(`macro session state persistence failed: ${error?.message ?? String(error)}`),
        { code: 'E_MACRO_STATE_PERSIST', cause: error },
      );
    }
  }

  clear() {
    this.state = null;
    try {
      rmSync(this.path, { force: true });
      this.lastError = null;
      return true;
    } catch (error) {
      this.lastError = { code: 'E_SESSION_STATE_CLEAR', message: error?.message ?? String(error) };
      return false;
    }
  }

  _quarantine() {
    try {
      renameSync(this.path, `${this.path}.broken`);
    } catch {
      // best effort; do not block startup on a bad recovery file
    }
    this.state = null;
  }
}

export default SessionStateStore;
