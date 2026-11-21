import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  AI_INJECTION_GRACE_MS,
  IDLE_TIMEOUT_MS,
  idleExpired,
  inInjectionGraceWindow,
  isDestructiveHotkey,
  inputAllowedUnderPrivacy,
  isStaleFrame,
  MEMORY_LIMIT_BYTES,
  memoryLimitExceeded,
  shouldCaptureInput,
  shouldPauseForUserTakeover,
  effectiveHumanizeProfile,
  USER_TAKEOVER_RESUME_MS,
  USER_TAKEOVER_TURBO_RESUME_MS,
  userTakeoverResumeMs,
} from '../lib/guards.js';

test('destructive hotkeys require confirmation', () => {
  assert.equal(isDestructiveHotkey(['alt', 'f4']), true);
  assert.equal(isDestructiveHotkey(['F4', 'ALT']), true);
  assert.equal(isDestructiveHotkey(['ctrl', 's']), true);
  assert.equal(isDestructiveHotkey(['delete']), true);
  assert.equal(isDestructiveHotkey(['ctrl', 'a']), false);
  assert.equal(isDestructiveHotkey(['shift', 'delete']), false);
  assert.equal(isDestructiveHotkey([]), false);
});

test('idle watchdog expires only after five quiet minutes', () => {
  const now = 1_000_000;
  assert.equal(IDLE_TIMEOUT_MS, 300_000);
  assert.equal(idleExpired(now - IDLE_TIMEOUT_MS, now), true);
  assert.equal(idleExpired(now - IDLE_TIMEOUT_MS + 1, now), false);
  assert.equal(idleExpired(0, now), false);
  assert.equal(idleExpired(now, now), false);
});

test('P22.2 stale-frame and memory guards are pure boundaries', () => {
  const now = 1_000_000;
  assert.equal(isStaleFrame(now - 1001, now), true);
  assert.equal(isStaleFrame(now - 1000, now), false);
  assert.equal(isStaleFrame(now + 500, now), false);
  assert.equal(isStaleFrame(0, now), false);
  assert.equal(memoryLimitExceeded(MEMORY_LIMIT_BYTES + 1), true);
  assert.equal(memoryLimitExceeded(MEMORY_LIMIT_BYTES), false);
  assert.equal(memoryLimitExceeded(Number.NaN), false);
});

test('P22.3 privacy bypass and AI-injection recording guards', () => {
  assert.equal(inputAllowedUnderPrivacy(false, false), true);
  assert.equal(inputAllowedUnderPrivacy(true, false), false);
  assert.equal(inputAllowedUnderPrivacy(true, true), true);
  assert.equal(shouldCaptureInput(false, true), true);
  assert.equal(shouldCaptureInput(true, true), false);
  assert.equal(shouldCaptureInput(false, false), false);
});


test('P23 user takeover pauses only real input and resumes at 1.5s', () => {
  assert.equal(USER_TAKEOVER_RESUME_MS, 1500);
  assert.equal(shouldPauseForUserTakeover(false, true), true);
  assert.equal(shouldPauseForUserTakeover(true, true), false);
  assert.equal(shouldPauseForUserTakeover(false, false), false);
});

test('P4 turbo soft-pause resumes in 50ms instead of 1.5s', () => {
  assert.equal(USER_TAKEOVER_TURBO_RESUME_MS, 50);
  assert.equal(userTakeoverResumeMs(false), 1500);
  assert.equal(userTakeoverResumeMs(true), 50);
  assert.equal(userTakeoverResumeMs(undefined), 1500);
});

test('P5 turbo downgrades balanced humanization to precise', () => {
  assert.equal(effectiveHumanizeProfile('off', false), 'off');
  assert.equal(effectiveHumanizeProfile('balanced', false), 'balanced');
  assert.equal(effectiveHumanizeProfile('precise', false), 'precise');
  assert.equal(effectiveHumanizeProfile('balanced', true), 'precise');
  assert.equal(effectiveHumanizeProfile('precise', true), 'precise');
  assert.equal(effectiveHumanizeProfile('bogus', false), 'off');
});

test('P1 8.10 injected keyups get a 100-200ms grace window', () => {
  assert.ok(AI_INJECTION_GRACE_MS >= 100 && AI_INJECTION_GRACE_MS <= 200);
  const until = 1_000 + AI_INJECTION_GRACE_MS;
  assert.equal(inInjectionGraceWindow(1_000, until), true);
  assert.equal(inInjectionGraceWindow(until - 1, until), true);
  assert.equal(inInjectionGraceWindow(until, until), false);
  assert.equal(inInjectionGraceWindow(until + 1, until), false);
  assert.equal(inInjectionGraceWindow(1_000, 0), false);
  assert.equal(inInjectionGraceWindow(Number.NaN, until), false);
  // A user who keeps typing after the window still pauses AI.
  assert.equal(shouldPauseForUserTakeover(false, true) && !inInjectionGraceWindow(until + 1, until), true);
});
