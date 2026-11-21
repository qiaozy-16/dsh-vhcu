import { test } from 'node:test';
import assert from 'node:assert/strict';

import { computeImageScale, parseFrameSize, scaleImagePoint } from '../lib/coords.js';

test('P6 dynamic image scale 1280x720 -> 1920x1080 is exactly 1.5', () => {
  const scale = computeImageScale(1920, 1080, 1280, 720);
  assert.equal(scale.scaleX, 1.5);
  assert.equal(scale.scaleY, 1.5);
  assert.equal(scale.scaleFactor, 1.5);
  assert.deepEqual(scaleImagePoint(100, 50, scale.scaleX, scale.scaleY), [150, 75]);
});

test('multi-screen image origin maps a negative desktop origin', () => {
  const scale = computeImageScale(6400, 1080, 1280, 216);
  assert.deepEqual(scaleImagePoint(100, 50, scale.scaleX, scale.scaleY, -1920, 0), [-1420, 250]);
  assert.deepEqual(scaleImagePoint(0, 0, 1, 1, -1920, 0), [-1920, 0]);
});

test('P6 non-uniform image scales use independent axes', () => {
  const scale = computeImageScale(2000, 900, 1000, 600);
  assert.equal(scale.scaleX, 2);
  assert.equal(scale.scaleY, 1.5);
  assert.equal(scale.scaleFactor, null);
  assert.deepEqual(scaleImagePoint(10, 20, scale.scaleX, scale.scaleY), [20, 30]);
});

test('P6 frame-size header parsing is defensive', () => {
  assert.deepEqual(parseFrameSize('1280x720'), { width: 1280, height: 720 });
  assert.equal(parseFrameSize('1280x0'), null);
  assert.equal(parseFrameSize('garbage'), null);
});

test('P6 zero/invalid dimensions never divide', () => {
  for (const args of [[0, 1080, 1280, 720], [1920, 0, 1280, 720], [1920, 1080, 0, 720], [1920, 1080, 1280, 0]]) {
    assert.throws(() => computeImageScale(...args), (error) => error.code === 'E_COORDINATES');
  }
});
