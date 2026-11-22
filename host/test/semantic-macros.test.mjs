import { test } from 'node:test';
import assert from 'node:assert/strict';

import { normalizeMacro } from '../lib/macros.js';
import { normalizeSemantic, resolveSemanticSteps } from '../lib/semantic-macros.js';

test('semantic metadata is generic and does not add an app-specific adapter', () => {
  const semantic = normalizeSemantic({
    app: 'blender',
    function: 'transform.move_axis',
    params: { axis: 'x', start_x: '{{x1}}', start_y: 100, end_x: 500, end_y: 100 },
  });
  assert.equal(semantic.app, 'blender');
  assert.equal(semantic.function, 'transform.move_axis');
  assert.equal(semantic.params.axis, 'x');

  const macro = normalizeMacro({
    name: 'move_x',
    semantic,
    steps: [
      { op: 'key', keys: ['g'] },
      { op: 'key', keys: ['x'] },
      { op: 'drag', x1: '{{x1}}', y1: 100, x2: 500, y2: 100 },
    ],
  });
  assert.equal(macro.semantic.function, 'transform.move_axis');
  assert.equal(resolveSemanticSteps(macro).length, 3);
  assert.equal(resolveSemanticSteps({ steps: [{ op: 'wait', ms: 1 }] }).length, 1);
});

test('semantic macros still require a generic action plan', () => {
  assert.throws(
    () => normalizeMacro({
      name: 'empty_semantic',
      semantic: { app: 'blender', function: 'transform.move_axis', params: {} },
      steps: [],
    }),
    (error) => error.code === 'E_PROTO',
  );
});
