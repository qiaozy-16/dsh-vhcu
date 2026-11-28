/**
 * @file payload.test.mjs — URL / Base64 dual-track for vision payloads.
 *
 * Covers the pure assembly helpers, the settings contract, and the renderer
 * preference used by every image-returning tool. The last group drives the real
 * plugin through the mock context, so the schema/renderer stay wired even when
 * the helper module is edited later.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
  BASE64_MAX_WIDTH,
  base64WidthPlan,
  imageMimeFromBuffer,
  normalizeVisionPayloadMode,
  payloadNotice,
  pickImageSource,
  pickRoiImageSource,
  resolveVisionPayloadMode,
  toImageDataUrl,
} from '../lib/payload.js';
import { DEFAULT_SETTINGS, mergeSettings } from '../lib/settings.js';
import { apply } from '../../lib/index.js';
import { createMockCtx } from '../../dev/mock-ctx.mjs';

const PNG = Buffer.from([
  0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x01, 0x02, 0x03,
]);
const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46]);

test('payload: mode normalization is total (bad input falls back)', () => {
  assert.equal(normalizeVisionPayloadMode('url'), 'url');
  assert.equal(normalizeVisionPayloadMode('base64'), 'base64');
  assert.equal(normalizeVisionPayloadMode('  BASE64 '), 'base64');
  assert.equal(normalizeVisionPayloadMode('jpeg'), 'url');
  assert.equal(normalizeVisionPayloadMode(undefined), 'url');
  assert.equal(normalizeVisionPayloadMode(null, 'base64'), 'base64');
  assert.equal(normalizeVisionPayloadMode('nope', 'nope'), 'url');
});

test('payload: a valid override wins, an invalid one never flips the setting', () => {
  assert.equal(resolveVisionPayloadMode({ override: 'base64', setting: 'url' }), 'base64');
  assert.equal(resolveVisionPayloadMode({ override: 'url', setting: 'base64' }), 'url');
  assert.equal(resolveVisionPayloadMode({ override: 'png', setting: 'base64' }), 'base64');
  assert.equal(resolveVisionPayloadMode({ setting: 'base64' }), 'base64');
  assert.equal(resolveVisionPayloadMode({}), 'url');
});

test('payload: base64 + precision is clamped to 1280 with a hint', () => {
  const clamped = base64WidthPlan({ mode: 'base64', precision: true, maxWidth: 1920 });
  assert.equal(clamped.maxWidth, BASE64_MAX_WIDTH);
  assert.equal(clamped.clamped, true);
  assert.match(clamped.hint, /1280/);
  assert.match(clamped.hint, /payload_mode_override/);

  const small = base64WidthPlan({ mode: 'base64', precision: true, maxWidth: 1000 });
  assert.equal(small.maxWidth, 1000);
  assert.equal(small.clamped, false);
  assert.equal(small.hint, null);

  const urlMode = base64WidthPlan({ mode: 'url', precision: true, maxWidth: 1920 });
  assert.equal(urlMode.maxWidth, 1920);
  assert.equal(urlMode.clamped, false);

  const noPrecision = base64WidthPlan({ mode: 'base64', precision: false, maxWidth: 1920 });
  assert.equal(noPrecision.maxWidth, 1920);
  assert.equal(noPrecision.clamped, false);
});

test('payload: data URI sniffs the real container instead of assuming jpeg', () => {
  assert.equal(imageMimeFromBuffer(PNG), 'image/png');
  assert.equal(imageMimeFromBuffer(JPEG), 'image/jpeg');
  assert.equal(imageMimeFromBuffer(Buffer.from('not an image')), null);

  const png = toImageDataUrl(PNG);
  assert.match(png.dataUrl, /^data:image\/png;base64,/);
  assert.equal(png.mime, 'image/png');
  assert.equal(png.bytes, PNG.length);
  assert.equal(toImageDataUrl(Buffer.from('nope')), null);
  assert.equal(toImageDataUrl(PNG, { maxBytes: 4 }), null, 'oversize must degrade, not throw');
});

test('payload: renderer source prefers inline pixels and falls back to the URL', () => {
  assert.equal(
    pickImageSource({ frame_url: 'http://x/f', frame_data_url: 'data:image/png;base64,AA' }),
    'data:image/png;base64,AA',
  );
  assert.equal(pickImageSource({ frame_url: 'http://x/f' }), 'http://x/f');
  assert.equal(pickImageSource({}), null);
  assert.equal(pickRoiImageSource({ frame_url: 'u', frame_data_url: 'd' }), 'd');
  assert.equal(pickRoiImageSource({ frame_url: 'u' }), 'u');
  assert.equal(pickRoiImageSource(null), null);
});

test('payload: notices distinguish delivered, downgraded and url modes', () => {
  assert.equal(payloadNotice({ mode: 'url' }), null);
  assert.match(payloadNotice({ mode: 'base64', bytes: 1234 }), /1234/);
  assert.match(payloadNotice({ mode: 'base64', downgraded: true }), /回退/);
});

test('payload: settings default to url and reject unknown modes', () => {
  assert.equal(DEFAULT_SETTINGS.visionPayloadMode, 'url');
  assert.equal(mergeSettings({}, { visionPayloadMode: 'base64' }).visionPayloadMode, 'base64');
  assert.equal(mergeSettings({}, { visionPayloadMode: 'jpeg' }).visionPayloadMode, 'url');
  assert.equal(
    mergeSettings({ visionPayloadMode: 'base64' }, { debug: true }).visionPayloadMode,
    'base64',
    'unrelated patches must not reset the track',
  );
});

// ── plugin wiring ────────────────────────────────────────────────────────
const suffix = `${process.pid}-${Date.now()}`;
const tempFiles = {
  VHCU_MACROS_PATH: join(tmpdir(), `vhcu-payload-macros-${suffix}.json`),
  VHCU_SETTINGS_PATH: join(tmpdir(), `vhcu-payload-settings-${suffix}.json`),
  VHCU_SESSION_STATE_PATH: join(tmpdir(), `vhcu-payload-session-${suffix}.json`),
};
const silentLogger = { debug() {}, info() {}, warn() {}, error() {} };

function withPlugin(fn) {
  const saved = { ...process.env };
  Object.assign(process.env, tempFiles);
  process.env.VHCU_DISABLE_USER_TAKEOVER = '1';
  const mock = createMockCtx({ logger: silentLogger });
  try {
    apply(mock.ctx);
    return Promise.resolve(fn(mock));
  } finally {
    mock.dispose();
    process.env = saved;
    for (const path of Object.values(tempFiles)) rmSync(path, { force: true });
  }
}

test('payload: the three image tools accept payload_mode_override', async () => {
  await withPlugin(async ({ tools }) => {
    for (const name of ['vhcu_get_full_frame', 'vhcu_capture_target', 'vhcu_get_vision_events']) {
      const def = tools.get(name);
      assert.ok(def, `${name} missing`);
      const schema = def.parameters.properties.payload_mode_override;
      assert.ok(schema, `${name} has no payload_mode_override`);
      assert.deepEqual(schema.enum, ['url', 'base64']);
    }
  });
});

test('payload: full-frame renderer embeds the data URI, not the signed URL', async () => {
  await withPlugin(async ({ tools }) => {
    const def = tools.get('vhcu_get_full_frame');
    const value = {
      ok: true,
      message: '全屏快照 1920x1080',
      frame_url: 'http://127.0.0.1/vhcu/frame?sig=abc',
      frame_data_url: 'data:image/png;base64,AAAA',
      payload_mode: 'base64',
    };
    const text = def.output.render({}, value)[0].text;
    assert.match(text, /data:image\/png;base64,AAAA/);
    assert.doesNotMatch(text, /sig=abc/);
  });
});

test('payload: url mode still renders the signed URL unchanged', async () => {
  await withPlugin(async ({ tools }) => {
    const def = tools.get('vhcu_get_full_frame');
    const text = def.output.render({}, {
      ok: true,
      message: '全屏快照',
      frame_url: 'http://127.0.0.1/vhcu/frame?sig=abc',
      payload_mode: 'url',
    })[0].text;
    assert.match(text, /sig=abc/);
    assert.doesNotMatch(text, /data:image/);
  });
});

test('payload: events renderer prefers inlined ROI pixels', async () => {
  await withPlugin(async ({ tools }) => {
    const def = tools.get('vhcu_get_vision_events');
    const text = def.output.render({}, {
      ok: true,
      events: [
        {
          id: 1,
          ts: Date.now(),
          text: 'screen',
          frame_url: 'http://x/full',
          rois: [
            { frame_url: 'http://x/roi', frame_data_url: 'data:image/png;base64,ROI' },
            { frame_url: 'http://x/roi2' },
          ],
        },
      ],
    })[0].text;
    assert.match(text, /data:image\/png;base64,ROI/);
    assert.match(text, /http:\/\/x\/roi2/, 'non-inlined ROI must keep its URL');
  });
});

test('payload: base64 request without a daemon fails soft with E_STATE', async () => {
  await withPlugin(async ({ tools }) => {
    const def = tools.get('vhcu_get_full_frame');
    const result = await def.execute({ payload_mode_override: 'base64' }, {});
    assert.equal(result.ok, false);
    assert.equal(result.code, 'E_STATE');
    assert.equal(result.frame_data_url, undefined, 'no pixels must be fabricated without a daemon');
  });
});
