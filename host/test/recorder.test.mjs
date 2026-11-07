import { test } from 'node:test';
import assert from 'node:assert/strict';

import { MacroRecorder } from '../lib/recorder.js';

function makeRecorder(options = {}) {
  let clock = 1000;
  const recorder = new MacroRecorder({ now: () => clock, ...options });
  return {
    recorder,
    at(ms) {
      clock = 1000 + ms;
      return clock;
    },
    clock() {
      return clock;
    },
  };
}

test('P19.2 recorder aggregates printable keys and inserts waits', () => {
  const h = makeRecorder();
  const r = h.recorder;
  r.start();
  r.handle({ type: 'keydown', time: h.at(50), key: 'A' });
  r.handle({ type: 'keydown', time: h.at(100), key: 'B' });
  r.handle({ type: 'keydown', time: h.at(200), key: 'C' });
  assert.deepEqual(r.summary().steps, [{ op: 'type', text: 'abc' }]);

  r.handle({ type: 'mousedown', time: h.at(1000), x: 640.4, y: 360.6, button: 1 });
  const steps = r.summary().steps;
  assert.equal(steps[0].op, 'type');
  assert.equal(steps[1].op, 'wait');
  assert.ok(steps[1].ms >= 700 && steps[1].ms <= 900);
  assert.deepEqual(steps[2], { op: 'click', x: 640, y: 361 });

  r.handle({ type: 'keydown', time: h.at(1100), key: 'C', ctrlKey: true });
  assert.deepEqual(r.summary().steps.at(-1), { op: 'key', keys: ['ctrl', 'c'] });
  r.stop();
});

test('P19.2 recorder handles shift, right click and wheel', () => {
  const h = makeRecorder();
  const r = h.recorder;
  r.start();
  r.handle({ type: 'keydown', time: h.at(50), key: 'A', shiftKey: true });
  r.handle({ type: 'keydown', time: h.at(100), key: '1', shiftKey: true });
  assert.deepEqual(r.summary().steps, [{ op: 'type', text: 'A!' }]);
  r.handle({ type: 'mousedown', time: h.at(200), x: 10, y: 20, button: 2 });
  r.handle({ type: 'wheel', time: h.at(250), direction: 3, rotation: -1, amount: 2, x: 10, y: 20 });
  assert.deepEqual(r.summary().steps, [
    { op: 'type', text: 'A!' },
    { op: 'right_click', x: 10, y: 20 },
    { op: 'scroll', amount: -2 },
  ]);
  r.stop();
});

test('P19.2 recorder flushes text on stop and caps steps', () => {
  const h = makeRecorder({ maxSteps: 2 });
  const r = h.recorder;
  r.start();
  r.handle({ type: 'keydown', time: h.at(10), key: 'X' });
  r.handle({ type: 'keydown', time: h.at(20), key: 'Y' });
  r.handle({ type: 'mousedown', time: h.at(700), x: 1, y: 2, button: 1 });
  const result = r.stop();
  assert.equal(result.truncated, true);
  assert.equal(result.steps.length, 2);
  assert.equal(result.steps[0].op, 'type');
  assert.equal(result.steps[1].op, 'wait');
  assert.ok(result.duration_ms >= 0);
});

test('P19.2 recorder ignores unsupported keys and modifier presses', () => {
  const h = makeRecorder();
  const r = h.recorder;
  r.start();
  r.handle({ type: 'keydown', time: h.at(10), key: 'Ctrl', ctrlKey: true });
  r.handle({ type: 'keydown', time: h.at(20), key: 'UnknownKey' });
  r.handle({ type: 'keydown', time: h.at(30), key: 'F5' });
  const steps = r.summary().steps;
  assert.deepEqual(steps, [{ op: 'key', keys: ['f5'] }]);
  assert.ok(r.summary().ignored >= 2);
  r.stop();
});


test('P24 recorder default cap is 100 steps', () => {
  const recorder = new MacroRecorder();
  assert.equal(recorder.maxSteps, 100);
});
