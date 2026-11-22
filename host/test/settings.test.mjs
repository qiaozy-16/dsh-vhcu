import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
  DEFAULT_SETTINGS,
  SettingsStore,
  mergeSettings,
  sanitizeBlacklist,
} from '../lib/settings.js';

function tempPath(label) {
  const dir = mkdtempSync(join(tmpdir(), `vhcu-settings-${label}-`));
  return { dir, path: join(dir, 'settings.json') };
}

test('P6 settings persist across a fresh store instance', () => {
  const { dir, path } = tempPath('roundtrip');
  try {
    const first = new SettingsStore({ path });
    assert.equal(first.settings.cursorInjectionMode, 'hybrid');
    assert.equal(first.settings.humanizeProfile, 'off');
    first.update({ cursorInjectionMode: 'virtual', humanizeProfile: 'balanced', turboMode: true });
    const raw = JSON.parse(readFileSync(path, 'utf8'));
    assert.equal(raw.version, 1);
    assert.equal(raw.settings.cursorInjectionMode, 'virtual');
    assert.equal(raw.settings.humanizeProfile, 'balanced');
    const second = new SettingsStore({ path });
    assert.equal(second.settings.cursorInjectionMode, 'virtual');
    assert.equal(second.settings.humanizeProfile, 'balanced');
    assert.equal(second.settings.turboMode, true);
    assert.equal(second.lastError, null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('P6 settings merge fills missing defaults and ignores bad values', () => {
  const merged = mergeSettings(DEFAULT_SETTINGS, {
    retryMaxAttempts: 'not-a-number',
    humanizeProfile: 'robot',
    cursorInjectionMode: 'teleport',
    privacyBlacklist: [' 密码 ', 'password', 'PASSWORD', '', ' 银行 '],
  });
  assert.equal(merged.retryMaxAttempts, 3);
  assert.equal(merged.humanizeProfile, 'off');
  assert.equal(merged.cursorInjectionMode, 'hybrid');
  assert.deepEqual(merged.privacyBlacklist, ['密码', 'password', '银行']);
  assert.equal(sanitizeBlacklist('not-array').length, 0);
});

test('UIA depth is configurable, defaulting to 6 and clamped to 1..10', () => {
  assert.equal(DEFAULT_SETTINGS.uiaDefaultDepth, 6);
  assert.equal(mergeSettings(DEFAULT_SETTINGS, { uiaDefaultDepth: 9 }).uiaDefaultDepth, 9);
  assert.equal(mergeSettings(DEFAULT_SETTINGS, { uiaDefaultDepth: 0 }).uiaDefaultDepth, 6);
  assert.equal(mergeSettings(DEFAULT_SETTINGS, { uiaDefaultDepth: 11 }).uiaDefaultDepth, 6);
  assert.equal(mergeSettings(DEFAULT_SETTINGS, { uiaDefaultDepth: '7' }).uiaDefaultDepth, 7);
});

test('P6 corrupt settings are backed up, never silently ignored', () => {
  const { dir, path } = tempPath('corrupt');
  try {
    writeFileSync(path, '{broken json', 'utf8');
    const store = new SettingsStore({ path });
    assert.equal(store.settings.cursorInjectionMode, 'hybrid');
    assert.ok(store.lastError);
    const names = readdirSync(dir);
    assert.ok(names.includes('settings.json.broken'), names.join(','));
    assert.ok(readFileSync(path, 'utf8').includes('"version"'));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('P6 persistence failure throws instead of pretending the save worked', () => {
  const { dir, path } = tempPath('failure');
  try {
    // A directory at the target path makes rename fail on every platform.
    const store = new SettingsStore({ path });
    const before = store.settings.cursorInjectionMode;
    rmSync(path, { force: true });
    // Replace the file with a directory after load.
    mkdirSync(path);
    assert.throws(
      () => store.update({ cursorInjectionMode: 'virtual' }),
      (error) => error.code === 'E_SETTINGS_PERSIST',
    );
    assert.equal(store.settings.cursorInjectionMode, before);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('P12 shared settings refresh and do not overwrite another window stale draft', () => {
  const { dir, path } = tempPath('shared');
  try {
    const store = new SettingsStore({ path });
    const stale = new SettingsStore({ path });
    // Simulate another DSH window writing the shared file.
    const external = {
      version: 1,
      settings: {
        ...store.settings,
        autoApproveDestructive: true,
        privacyUnprotected: true,
        turboMode: true,
      },
    };
    writeFileSync(path, `${JSON.stringify(external, null, 2)}\n`, 'utf8');
    assert.equal(store.refresh().autoApproveDestructive, true);

    // A second, stale store represents another DSH window. Its full-draft
    // save must not silently overwrite the external true.
    const merged = stale.update({
      autoApproveDestructive: false,
      privacyUnprotected: true,
      turboMode: true,
    });
    assert.equal(merged.autoApproveDestructive, true);
    assert.equal(merged.privacyUnprotected, true);
    assert.equal(merged.turboMode, true);

    // An actual local toggle is still applied, and other fields survive.
    const toggled = stale.update({ autoApproveDestructive: false });
    assert.equal(toggled.autoApproveDestructive, false);
    assert.equal(toggled.privacyUnprotected, true);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
