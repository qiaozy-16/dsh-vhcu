/**
 * @file settings.js — persistent dsh-vhcu settings.
 *
 * Load -> merge with defaults -> atomic save. The JSON file lives beside
 * macros.json in %LOCALAPPDATA%\dsh-vhcu\settings.json (override:
 * VHCU_SETTINGS_PATH). Saving is atomic: temp file + rename, so a crash or
 * disk error cannot leave a half-written settings file. A failed save throws
 * and the caller must not silently pretend the setting was applied.
 */

import { mkdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';

const MAX_BLACKLIST = 50;
const MAX_BLACKLIST_ITEM = 64;
const IME_SHORTCUTS = new Set(['ctrl+space', 'shift', 'none']);
const HEAR_SENSITIVITY = new Set(['strict', 'normal', 'sensitive']);
const HUMANIZE_PROFILES = new Set(['off', 'precise', 'balanced']);
const CURSOR_MODES = new Set(['physical', 'virtual', 'uia', 'hybrid']);

export const DEFAULT_SETTINGS = Object.freeze({
  debug: false,
  imeShortcut: 'ctrl+space',
  hearSensitivity: 'normal',
  hearRawTranscripts: false,
  privacyUnprotected: false,
  autoApproveDestructive: false,
  turboMode: false,
  humanizeProfile: 'off',
  cursorInjectionMode: 'hybrid',
  allowHighPrecisionVision: true,
  enableMacroAutoGeneration: false,
  allowUiaValueRead: false,
  privacyBlockMode: 'warning_image',
  retryMaxAttempts: 3,
  privacyBlacklist: Object.freeze(['密码', '银行', '隐私', 'Password', 'Login', '私人']),
  macros: Object.freeze({ open_notepad: true, save: true }),
});

export function defaultSettingsPath(env = process.env) {
  if (env.VHCU_SETTINGS_PATH) return resolve(env.VHCU_SETTINGS_PATH);
  const base = env.LOCALAPPDATA || env.XDG_DATA_HOME || join(homedir(), '.local', 'share');
  return join(base, 'dsh-vhcu', 'settings.json');
}

export function sanitizeBlacklist(values) {
  if (!Array.isArray(values)) return [];
  const seen = new Set();
  const cleaned = [];
  for (const raw of values) {
    const value = String(raw ?? '').trim();
    if (!value || value.length > MAX_BLACKLIST_ITEM) continue;
    const key = value.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    cleaned.push(value);
    if (cleaned.length >= MAX_BLACKLIST) break;
  }
  return cleaned;
}

function normalizedRetries(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return DEFAULT_SETTINGS.retryMaxAttempts;
  return Math.max(0, Math.min(5, Math.trunc(parsed)));
}

function baseSettings(current = {}) {
  const blacklistProvided = Array.isArray(current.privacyBlacklist);
  const blacklist = sanitizeBlacklist(current.privacyBlacklist);
  return {
    debug: Boolean(current.debug),
    imeShortcut: IME_SHORTCUTS.has(current.imeShortcut)
      ? current.imeShortcut
      : DEFAULT_SETTINGS.imeShortcut,
    hearSensitivity: HEAR_SENSITIVITY.has(current.hearSensitivity)
      ? current.hearSensitivity
      : DEFAULT_SETTINGS.hearSensitivity,
    hearRawTranscripts: Boolean(current.hearRawTranscripts),
    privacyUnprotected: Boolean(current.privacyUnprotected),
    autoApproveDestructive: Boolean(current.autoApproveDestructive),
    turboMode: Boolean(current.turboMode),
    humanizeProfile: HUMANIZE_PROFILES.has(current.humanizeProfile)
      ? current.humanizeProfile
      : DEFAULT_SETTINGS.humanizeProfile,
    cursorInjectionMode: CURSOR_MODES.has(current.cursorInjectionMode)
      ? current.cursorInjectionMode
      : DEFAULT_SETTINGS.cursorInjectionMode,
    allowHighPrecisionVision: current.allowHighPrecisionVision !== false,
    enableMacroAutoGeneration: current.enableMacroAutoGeneration === true,
    allowUiaValueRead: Boolean(current.allowUiaValueRead),
    privacyBlockMode: current.privacyBlockMode === 'black_image' ? 'black_image' : 'warning_image',
    retryMaxAttempts: normalizedRetries(current.retryMaxAttempts ?? DEFAULT_SETTINGS.retryMaxAttempts),
    privacyBlacklist: blacklistProvided ? blacklist : [...DEFAULT_SETTINGS.privacyBlacklist],
    macros: {
      open_notepad: current.macros?.open_notepad !== false,
      save: current.macros?.save !== false,
    },
  };
}

/** Merge one validated patch into current settings. Unknown fields are ignored. */
export function mergeSettings(current, patch) {
  const next = baseSettings(current && typeof current === 'object' ? current : {});
  if (!patch || typeof patch !== 'object') return next;
  if (typeof patch.debug === 'boolean') next.debug = patch.debug;
  if (typeof patch.imeShortcut === 'string' && IME_SHORTCUTS.has(patch.imeShortcut)) {
    next.imeShortcut = patch.imeShortcut;
  }
  if (typeof patch.hearSensitivity === 'string' && HEAR_SENSITIVITY.has(patch.hearSensitivity)) {
    next.hearSensitivity = patch.hearSensitivity;
  }
  if (typeof patch.hearRawTranscripts === 'boolean') {
    next.hearRawTranscripts = patch.hearRawTranscripts;
  }
  if (typeof patch.privacyUnprotected === 'boolean') {
    next.privacyUnprotected = patch.privacyUnprotected;
  }
  if (typeof patch.autoApproveDestructive === 'boolean') {
    next.autoApproveDestructive = patch.autoApproveDestructive;
  }
  if (typeof patch.turboMode === 'boolean') next.turboMode = patch.turboMode;
  if (HUMANIZE_PROFILES.has(patch.humanizeProfile)) {
    next.humanizeProfile = patch.humanizeProfile;
  }
  if (CURSOR_MODES.has(patch.cursorInjectionMode)) {
    next.cursorInjectionMode = patch.cursorInjectionMode;
  }
  if (typeof patch.allowHighPrecisionVision === 'boolean') {
    next.allowHighPrecisionVision = patch.allowHighPrecisionVision;
  }
  if (typeof patch.enableMacroAutoGeneration === 'boolean') {
    next.enableMacroAutoGeneration = patch.enableMacroAutoGeneration;
  }
  if (typeof patch.allowUiaValueRead === 'boolean') {
    next.allowUiaValueRead = patch.allowUiaValueRead;
  }
  if (patch.privacyBlockMode === 'warning_image' || patch.privacyBlockMode === 'black_image') {
    next.privacyBlockMode = patch.privacyBlockMode;
  }
  if (patch.retryMaxAttempts !== undefined) {
    const parsed = Number(patch.retryMaxAttempts);
    if (Number.isInteger(parsed) && parsed >= 0 && parsed <= 5) {
      next.retryMaxAttempts = parsed;
    }
  }
  if (patch.privacyBlacklist !== undefined) {
    next.privacyBlacklist = sanitizeBlacklist(patch.privacyBlacklist);
  }
  if (patch.macros && typeof patch.macros === 'object') {
    if (typeof patch.macros.open_notepad === 'boolean') {
      next.macros.open_notepad = patch.macros.open_notepad;
    }
    if (typeof patch.macros.save === 'boolean') {
      next.macros.save = patch.macros.save;
    }
  }
  return next;
}

export class SettingsStore {
  constructor({ path = defaultSettingsPath() } = {}) {
    if (!path) throw new TypeError('SettingsStore requires a path');
    this.path = resolve(path);
    this.settings = mergeSettings(DEFAULT_SETTINGS, {});
    this.revision = 0;
    this.lastError = null;
    this.lastMtimeMs = 0;
    this.lastSize = -1;
    this.load();
  }

  load() {
    try {
      const text = readFileSync(this.path, 'utf8');
      const data = JSON.parse(text);
      const candidate = data && typeof data === 'object' && data.settings && typeof data.settings === 'object'
        ? data.settings
        : data;
      this.settings = mergeSettings(DEFAULT_SETTINGS, candidate);
      this.revision += 1;
      this.lastError = null;
      this.rememberFile();
      return this.settings;
    } catch (error) {
      if (error?.code === 'ENOENT') {
        this.settings = mergeSettings(DEFAULT_SETTINGS, {});
        try {
          this.persist(this.settings);
        } catch (persistError) {
          this.lastError = this.describeError(persistError);
        }
        return this.settings;
      }
      // A corrupted file must not brick the plugin, but it must not be silent:
      // preserve the bad bytes beside the real path and surface lastError.
      this.lastError = this.describeError(error);
      try {
        renameSync(this.path, `${this.path}.broken`);
      } catch {
        // best effort only
      }
      this.settings = mergeSettings(DEFAULT_SETTINGS, {});
      try {
        this.persist(this.settings);
      } catch (persistError) {
        this.lastError = this.describeError(persistError);
      }
      return this.settings;
    }
  }

  rememberFile() {
    try {
      const stat = statSync(this.path);
      this.lastMtimeMs = Number(stat.mtimeMs) || 0;
      this.lastSize = Number(stat.size);
    } catch {
      this.lastMtimeMs = 0;
      this.lastSize = -1;
    }
  }

  /** Re-read the shared file when another DSH window changed it. */
  refresh() {
    let stat;
    try {
      stat = statSync(this.path);
    } catch (error) {
      if (error?.code === 'ENOENT') return null;
      this.lastError = this.describeError(error);
      return null;
    }
    if (
      Number(stat.mtimeMs) === this.lastMtimeMs &&
      Number(stat.size) === this.lastSize
    ) {
      return null;
    }
    try {
      const text = readFileSync(this.path, 'utf8');
      const data = JSON.parse(text);
      const candidate = data && typeof data === 'object' && data.settings && typeof data.settings === 'object'
        ? data.settings
        : data;
      this.settings = mergeSettings(DEFAULT_SETTINGS, candidate);
      this.revision += 1;
      this.lastError = null;
      this.rememberFile();
      return this.settings;
    } catch (error) {
      // Do not rename/quarantine here: another window may be mid-write.
      this.lastError = this.describeError(error);
      return null;
    }
  }

  update(patch) {
    const previous = this.settings;
    // External changes must not be silently overwritten by a stale window.
    this.refresh();
    const changed = {};
    if (patch && typeof patch === 'object') {
      for (const key of Object.keys(patch)) {
        if (JSON.stringify(previous[key]) !== JSON.stringify(patch[key])) {
          changed[key] = patch[key];
        }
      }
    }
    if (!Object.keys(changed).length) return this.settings;
    const next = mergeSettings(this.settings, changed);
    this.persist(next);
    this.settings = next;
    this.revision += 1;
    this.lastError = null;
    return this.settings;
  }

  persist(settings) {
    const directory = dirname(this.path);
    mkdirSync(directory, { recursive: true });
    const temp = `${this.path}.${process.pid}.${Date.now()}.tmp`;
    const body = `${JSON.stringify({ version: 1, settings }, null, 2)}\n`;
    try {
      writeFileSync(temp, body, { encoding: 'utf8', mode: 0o600 });
      renameSync(temp, this.path);
      this.rememberFile();
    } catch (error) {
      try {
        rmSync(temp, { force: true });
      } catch {
        // ignore cleanup failure; original error matters
      }
      throw Object.assign(
        new Error(`settings persistence failed: ${error?.message ?? String(error)}`),
        { code: 'E_SETTINGS_PERSIST', cause: error },
      );
    }
  }

  describeError(error) {
    return {
      code: error?.code ?? 'E_SETTINGS_LOAD',
      message: error?.message ?? String(error),
    };
  }
}

export default SettingsStore;
