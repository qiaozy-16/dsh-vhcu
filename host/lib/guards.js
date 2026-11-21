/**
 * @file guards.js — P14 safety guards.
 *
 * 1. Destructive hotkey classification (requires explicit confirmation).
 * 2. Idle-watchdog arithmetic (5 minutes without a valid event).
 *
 * Pure functions only: no timers, no I/O, safe for unit tests.
 */

export const DESTRUCTIVE_HOTKEYS = Object.freeze([
  Object.freeze(['alt', 'f4']),
  Object.freeze(['ctrl', 's']),
  Object.freeze(['delete']),
]);

export const IDLE_TIMEOUT_MS = 5 * 60 * 1000;
export const IDLE_CHECK_MS = 15 * 1000;
export const FRAME_MAX_AGE_MS = 1000;
export const USER_TAKEOVER_RESUME_MS = 1500;
/** Turbo mode: resume after a very short quiet window, not the normal guard delay. */
export const USER_TAKEOVER_TURBO_RESUME_MS = 50;
export const MEMORY_LIMIT_BYTES = 1.5 * 1024 * 1024 * 1024;
export const MEMORY_CHECK_MS = 5000;

export const HUMANIZE_PROFILES = Object.freeze(['off', 'precise', 'balanced']);

/** Turbo keeps a humanization hint but never the 200–800ms balanced curve. */
export function effectiveHumanizeProfile(profile, turboMode = false) {
  if (profile !== 'precise' && profile !== 'balanced') return 'off';
  return turboMode ? 'precise' : profile;
}

/** Soft-pause resume delay: normal anti-jitter guard vs Turbo instant resume. */
export function userTakeoverResumeMs(turboMode = false) {
  return turboMode ? USER_TAKEOVER_TURBO_RESUME_MS : USER_TAKEOVER_RESUME_MS;
}

function normalizeKeys(keys) {
  if (!Array.isArray(keys)) return [];
  return keys.map((key) => String(key).trim().toLowerCase()).filter(Boolean);
}

/** True when the pressed combination is one of the destructive hotkeys. */
export function isDestructiveHotkey(keys) {
  const normalized = normalizeKeys(keys);
  if (!normalized.length) return false;
  const pressed = new Set(normalized);
  return DESTRUCTIVE_HOTKEYS.some(
    (combo) => combo.length === pressed.size && combo.every((key) => pressed.has(key)),
  );
}

/** True when an engine has had no valid event for the idle timeout. */
export function idleExpired(lastActivityAt, now = Date.now(), timeoutMs = IDLE_TIMEOUT_MS) {
  if (!Number.isFinite(lastActivityAt) || lastActivityAt <= 0) return false;
  return now - lastActivityAt >= timeoutMs;
}

/** P23: real user input pauses AI unless it is VHCU's own injection. */
export function shouldPauseForUserTakeover(isAiInjecting, running) {
  return Boolean(running) && !isAiInjecting;
}

/**
 * P1 8.10 — uiohook delivers our own injected key events asynchronously, so a
 * trailing keyup can be dispatched after `isAiInjecting` has flipped false.
 * Events inside this window are treated as leftovers, never as user takeover.
 */
export const AI_INJECTION_GRACE_MS = 150;

/** True while input events still belong to the batch VHCU just injected. */
export function inInjectionGraceWindow(now, graceUntil) {
  const at = Number(now);
  const until = Number(graceUntil);
  return Number.isFinite(at) && Number.isFinite(until) && at < until;
}

/** P22.3: high-risk privacy bypass allows both capture and input injection. */
export function inputAllowedUnderPrivacy(privacyBlocked, unprotected) {
  return !privacyBlocked || Boolean(unprotected);
}

/** P22.3: never let uiohook record input that VHCU itself is injecting. */
export function shouldCaptureInput(isAiInjecting, recordingActive = true) {
  return Boolean(recordingActive) && !isAiInjecting;
}

/** True when a supplied screenshot timestamp is older than the click budget. */
export function isStaleFrame(frameTimestamp, now = Date.now(), maxAgeMs = FRAME_MAX_AGE_MS) {
  const timestamp = Number(frameTimestamp);
  if (!Number.isFinite(timestamp) || timestamp <= 0) return false;
  return now - timestamp > maxAgeMs;
}

/** True when the host process RSS exceeds the V1.0 circuit-breaker limit. */
export function memoryLimitExceeded(rssBytes, limitBytes = MEMORY_LIMIT_BYTES) {
  const rss = Number(rssBytes);
  const limit = Number(limitBytes);
  if (!Number.isFinite(rss) || rss <= 0 || !Number.isFinite(limit) || limit <= 0) return false;
  return rss > limit;
}
