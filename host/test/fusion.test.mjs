import { test } from 'node:test';
import assert from 'node:assert/strict';

import { FusionBuffer, MULTIMODAL_WINDOW_MS } from '../lib/fusion.js';

function buffer() {
  const flushed = [];
  let now = 10_000;
  const fusion = new FusionBuffer({
    now: () => now,
    onFlush: (event) => flushed.push(event),
  });
  return {
    flushed,
    fusion,
    setNow(value) {
      now = value;
    },
  };
}

test('vision and hear within 1.5s fuse into one multimodal event', () => {
  const b = buffer();
  b.fusion.pushVision({ ts: 10_000, rois: [{ id: 1, x: 10, y: 20 }], summary: '画面变化' });
  b.fusion.pushHear({ ts: 11_200, text: '叮咚声' });
  assert.equal(b.flushed.length, 0);
  assert.equal(b.fusion.flushIfExpired(11_499), null);
  const event = b.fusion.flushIfExpired(11_500);
  assert.ok(event);
  assert.equal(b.flushed.length, 1);
  assert.equal(event.vision_count, 1);
  assert.equal(event.hear_count, 1);
  assert.equal(event.hear_text, '叮咚声');
  assert.equal(event.vision_rois.length, 1);
  assert.match(event.text, /叮咚声/);
});

test('single modality is preserved when there is no counterpart', () => {
  const b = buffer();
  b.fusion.pushHear({ ts: 20_000, text: '只有声音' });
  const event = b.fusion.flushIfExpired(21_500);
  assert.equal(event.vision, null);
  assert.deepEqual(event.vision_rois, []);
  assert.equal(event.hear_text, '只有声音');
  assert.equal(event.time, new Date(20_000).toTimeString().slice(0, 8));
});

test('a new event after the window expires closes the previous window first', () => {
  const b = buffer();
  b.fusion.pushVision({ ts: 30_000, rois: [], summary: 'v1' });
  b.fusion.pushHear({ ts: 31_600, text: 'later' });
  assert.equal(b.flushed.length, 1);
  assert.equal(b.flushed[0].hear, null);
  assert.equal(b.fusion.pendingStart, 31_600);
  b.fusion.flush(31_600, 'manual');
  assert.equal(b.flushed.length, 2);
  assert.equal(b.flushed[1].hear_text, 'later');
  assert.equal(MULTIMODAL_WINDOW_MS, 1500);
});

test('ambient audio joins the hear dimension and marks both', () => {
  const b = buffer();
  b.fusion.pushVision({ ts: 40_000, rois: [{ id: 1 }] });
  b.fusion.pushAudio({ ts: 40_400, rms: 0.15, rmsMax: 0.4, rmsMin: 0.02 });
  const event = b.fusion.flushIfExpired(41_500);
  assert.ok(event);
  assert.equal(event.modalities, 'both');
  assert.equal(event.audio_count, 1);
  assert.equal(event.audio_rms, 0.15);
  assert.equal(event.audio_rms_max, 0.4);
  assert.equal(event.audio_rms_min, 0.02);
  assert.equal(event.vision_count, 1);
  assert.match(event.hear_text, /\[RMS:0\.15 \| 峰值:0\.40\]/);
  assert.match(event.text, /RMS:/);
});

test('audio-only window uses hear modality', () => {
  const b = buffer();
  b.fusion.pushAudio({ ts: 50_000, rms: 0.2 });
  const event = b.fusion.flushIfExpired(51_500);
  assert.equal(event.modalities, 'hear');
  assert.equal(event.vision, null);
  assert.equal(event.audio_rms, 0.2);
  assert.match(event.hear_text, /RMS:0\.20/);
});

test('spike audio is marked in fusion event and text', () => {
  const b = buffer();
  b.fusion.pushVision({ ts: 60_000, rois: [] });
  b.fusion.pushAudio({ ts: 60_300, rms: 0.08, rmsMax: 0.5, rmsMin: 0.01, hasSpike: true });
  const event = b.fusion.flushIfExpired(61_500);
  assert.equal(event.has_spike, true);
  assert.match(event.hear_text, /💥 巨响\/音效/);
  assert.match(event.text, /💥 巨响\/音效/);
});
