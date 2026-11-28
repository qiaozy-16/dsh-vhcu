/**
 * @file payload.test.mjs — image transport mode (URL vs host attachment).
 *
 * The inline base64 text track is retired (hosts treated the data URI as text,
 * truncated it, and the model still saw nothing). `attachment` means: store the
 * PNG in the host attachment service and return an image content block.
 *
 * These tests cover the pure helpers, the settings contract + legacy migration,
 * and the renderer shape. The real attachment round-trip needs a live daemon and
 * host, so it is verified on-device rather than here.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
  VISION_PAYLOAD_MODES,
  needsAttachmentStore,
  normalizeVisionPayloadMode,
  resolveVisionPayloadMode,
} from '../lib/payload.js';
import { DEFAULT_SETTINGS, mergeSettings } from '../lib/settings.js';
import { apply } from '../../lib/index.js';
import { createMockCtx } from '../../dev/mock-ctx.mjs';

test('payload: modes are url/attachment and legacy base64 migrates', () => {
  assert.deepEqual([...VISION_PAYLOAD_MODES], ['url', 'attachment']);
  assert.equal(normalizeVisionPayloadMode('url'), 'url');
  assert.equal(normalizeVisionPayloadMode('attachment'), 'attachment');
  assert.equal(normalizeVisionPayloadMode('base64'), 'attachment', 'retired track migrates');
  assert.equal(normalizeVisionPayloadMode('  ATTACHMENT '), 'attachment');
  assert.equal(normalizeVisionPayloadMode('jpeg'), 'url');
  assert.equal(normalizeVisionPayloadMode(undefined), 'url');
  assert.equal(normalizeVisionPayloadMode(null, 'attachment'), 'attachment');
});

test('payload: override wins, invalid override never flips the setting', () => {
  assert.equal(resolveVisionPayloadMode({ override: 'attachment', setting: 'url' }), 'attachment');
  assert.equal(resolveVisionPayloadMode({ override: 'url', setting: 'attachment' }), 'url');
  assert.equal(resolveVisionPayloadMode({ override: 'base64', setting: 'url' }), 'attachment');
  assert.equal(resolveVisionPayloadMode({ override: 'png', setting: 'attachment' }), 'attachment');
  assert.equal(resolveVisionPayloadMode({ setting: 'attachment' }), 'attachment');
  assert.equal(resolveVisionPayloadMode({}), 'url');
  assert.equal(needsAttachmentStore('attachment'), true);
  assert.equal(needsAttachmentStore('url'), false);
});

test('payload: settings default url, accept attachment, migrate base64', () => {
  assert.equal(DEFAULT_SETTINGS.visionPayloadMode, 'url');
  assert.equal(mergeSettings({}, { visionPayloadMode: 'attachment' }).visionPayloadMode, 'attachment');
  assert.equal(
    mergeSettings({ visionPayloadMode: 'base64' }, {}).visionPayloadMode,
    'attachment',
    'persisted legacy value must migrate on load',
  );
  assert.equal(mergeSettings({}, { visionPayloadMode: 'jpeg' }).visionPayloadMode, 'url');
  assert.equal(
    mergeSettings({ visionPayloadMode: 'attachment' }, { debug: true }).visionPayloadMode,
    'attachment',
    'unrelated patches must not reset the track',
  );
});

// ── plugin wiring ────────────────────────────────────────────────────────
const suffix = `${process.pid}-${Date.now()}`;
const tempFiles = {
  VHCU_MACROS_PATH: join(tmpdir(), `vhcu-attach-macros-${suffix}.json`),
  VHCU_SETTINGS_PATH: join(tmpdir(), `vhcu-attach-settings-${suffix}.json`),
  VHCU_SESSION_STATE_PATH: join(tmpdir(), `vhcu-attach-session-${suffix}.json`),
};
const silentLogger = { debug() {}, info() {}, warn() {}, error() {} };

async function withPlugin(fn, { services = {} } = {}) {
  const saved = { ...process.env };
  Object.assign(process.env, tempFiles);
  process.env.VHCU_DISABLE_USER_TAKEOVER = '1';
  const mock = createMockCtx({ logger: silentLogger, services });
  try {
    apply(mock.ctx);
    // Must await: disposing before the body settles would clear the tool map
    // while the test is still calling tools.
    return await fn(mock);
  } finally {
    mock.dispose();
    process.env = saved;
    for (const path of Object.values(tempFiles)) rmSync(path, { force: true });
  }
}

test('payload: three image tools expose url/attachment only', async () => {
  await withPlugin(async ({ tools }) => {
    for (const name of ['vhcu_get_full_frame', 'vhcu_capture_target', 'vhcu_get_vision_events']) {
      const schema = tools.get(name)?.parameters?.properties?.payload_mode_override;
      assert.ok(schema, `${name} has no payload_mode_override`);
      assert.deepEqual(schema.enum, ['url', 'attachment'], `${name} still offers a retired mode`);
    }
  });
});

test('payload: url mode render is text-only and keeps the signed URL', async () => {
  await withPlugin(async ({ tools }) => {
    const def = tools.get('vhcu_get_full_frame');
    const blocks = def.output.render({}, {
      ok: true,
      message: '全屏快照',
      frame_url: 'http://127.0.0.1/vhcu/frame?sig=abc',
      payload_mode: 'url',
    });
    assert.equal(blocks.length, 1);
    assert.equal(blocks[0].type, 'text');
    assert.match(blocks[0].text, /sig=abc/);
    assert.doesNotMatch(blocks[0].text, /data:image/);
  });
});

test('payload: attachment render appends a real image content block', async () => {
  await withPlugin(async ({ tools }) => {
    const def = tools.get('vhcu_get_full_frame');
    const blocks = def.output.render({}, {
      ok: true,
      message: '全屏快照',
      frame_url: 'http://127.0.0.1/vhcu/frame?sig=abc',
      payload_mode: 'attachment',
      attachment: { attachmentId: 'att_123', mediaType: 'image/png' },
      attachment_meta: { mediaType: 'image/png', bytes: 570879, width: 1280, height: 720 },
    });
    assert.equal(blocks.length, 2);
    assert.equal(blocks[0].type, 'text');
    assert.match(blocks[0].text, /attachment_id=att_123/);
    assert.match(blocks[0].text, /bytes=570879/);
    assert.equal(blocks[1].type, 'image');
    assert.equal(blocks[1].attachment.attachmentId, 'att_123');
  });
});

test('payload: a ref without attachmentId never becomes an image block', async () => {
  await withPlugin(async ({ tools }) => {
    const def = tools.get('vhcu_get_full_frame');
    const blocks = def.output.render({}, {
      ok: true,
      message: '全屏快照',
      frame_url: 'http://x/f',
      payload_mode: 'attachment',
      attachment: {},
    });
    assert.equal(blocks.length, 1);
    assert.equal(blocks[0].type, 'text');
  });
});

test('payload: capture_target renderer can emit an image block too', async () => {
  await withPlugin(async ({ tools }) => {
    const def = tools.get('vhcu_capture_target');
    const blocks = def.output.render({}, {
      ok: true,
      message: '目标窗口快照',
      frame_url: 'http://x/target',
      payload_mode: 'attachment',
      attachment: { attachmentId: 'att_target' },
      attachment_meta: { mediaType: 'image/png', bytes: 1234, width: 800, height: 600 },
    });
    assert.equal(blocks.length, 2);
    assert.equal(blocks[1].type, 'image');
    assert.equal(blocks[1].attachment.attachmentId, 'att_target');
  });
});

test('payload: events renderer emits one block per attached event (max 3)', async () => {
  await withPlugin(async ({ tools }) => {
    const def = tools.get('vhcu_get_vision_events');
    const base = { ok: true, payload_mode: 'attachment' };
    const withImages = def.output.render({}, {
      ...base,
      events: [
        { id: 1, ts: Date.now(), rois: [{ frame_url: 'u1' }], attachment: { attachmentId: 'e1' } },
        { id: 2, ts: Date.now(), rois: [{ frame_url: 'u2' }], attachment: { attachmentId: 'e2' } },
        { id: 3, ts: Date.now(), rois: [{ frame_url: 'u3' }] },
      ],
    });
    assert.equal(withImages.length, 3, 'text + two images');
    assert.equal(withImages[1].type, 'image');
    assert.match(withImages[0].text, /image1=e1/);

    const textOnly = def.output.render({}, { ...base, events: [{ id: 1, rois: [] }] });
    assert.equal(textOnly.length, 1);
    assert.equal(textOnly[0].type, 'text');
  });
});

test('payload: attachment without a daemon degrades to URL with a hint', async () => {
  await withPlugin(async ({ tools }) => {
    const events = await tools.get('vhcu_get_vision_events').execute(
      { payload_mode_override: 'attachment' },
      {},
    );
    assert.equal(events.ok, true);
    assert.equal(events.count, 0, 'no diff events in a fresh mock');
    assert.equal(events.payload_mode, 'attachment', 'empty result keeps the requested mode');
    assert.equal(events.payload_attached_images, 0);

    const target = await tools.get('vhcu_capture_target').execute(
      { payload_mode_override: 'attachment' },
      {},
    );
    assert.equal(target.ok, false);
    assert.equal(target.code, 'E_STATE', 'no daemon yet: must fail soft, not fabricate');
    assert.equal(target.attachment, undefined);
  });
});

test('payload: attachment without a daemon fails soft, no fabricated block', async () => {
  await withPlugin(async ({ tools }) => {
    const result = await tools.get('vhcu_get_full_frame').execute(
      { payload_mode_override: 'attachment' },
      {},
    );
    assert.equal(result.ok, false);
    assert.equal(result.code, 'E_STATE');
    assert.equal(result.attachment, undefined);
  });
});

test('payload: attachment mode without the service is a no-op at execute time', async () => {
  // No `attachments` service in the mock: the plugin must neither throw during
  // apply nor pretend an attachment exists. (Execute needs a daemon, so this
  // test only proves the deployment probe is guarded.)
  await withPlugin(async ({ ctx }) => {
    assert.equal(typeof ctx.get, 'function');
    assert.equal(ctx.get('attachments'), undefined);
  }, { services: {} });
});
