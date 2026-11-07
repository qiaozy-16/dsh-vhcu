import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';

import {
  MACRO_RECOVERY_HINT,
  MAX_STEPS,
  MacroStore,
  normalizeMacro,
  normalizeSteps,
  isHostWindowTitle,
  recommendMacros,
  targetMatchesTitle,
  seedMacros,
  substituteText,
  withMacroRecoveryHint,
} from '../../host/lib/macros.js';

function tempStore() {
  const dir = mkdtempSync(join(tmpdir(), 'vhcu-macros-'));
  const path = join(dir, 'macros.json');
  return { dir, path };
}

test('macro store seeds, persists and reloads user macros', () => {
  const { dir, path } = tempStore();
  try {
    const store = new MacroStore({ path });
    assert.equal(store.list().length, 18);
    assert.ok(store.find('macro_launch_app'));
    for (const name of [
      'macro_switch_window',
      'macro_show_desktop',
      'macro_copy',
      'macro_paste',
      'macro_undo',
      'macro_select_all',
      'macro_browser_new_tab',
      'macro_browser_close_tab',
    ]) {
      const macro = store.find(name);
      assert.ok(macro, `${name} missing`);
      assert.equal(macro.category, '系统基础宏');
      assert.equal(macro.target_app, null);
      assert.equal(macro.builtin, true);
      assert.equal(macro.steps[0].op, 'key');
    }
    assert.ok(store.find('macro_create_folder'));
    assert.ok(store.find('macro_browser_search'));
    assert.ok(store.find('macro_mouse_double_click'));
    assert.ok(store.find('macro_mouse_right_click'));
    assert.ok(store.find('macro_window_drag'));
    assert.ok(store.find('macro_snap_window_left'));
    assert.ok(store.find('macro_snap_window_right'));
    assert.ok(store.find('macro_close_active_window'));
    assert.match(store.find('macro_window_drag').description, /不要用它拖拽系统窗口/);
    assert.equal(store.find('macro_launch_app').category, '系统基础宏');
    assert.equal(JSON.parse(readFileSync(path, 'utf8')).version, 2);
    assert.ok(store.categoriesList().some((category) => category.name === '系统基础宏' && category.builtin));

    const saved = store.add({
      name: 'open_settings',
      description: 'demo',
      steps: [{ op: 'click', x: 10, y: 20 }, { op: 'wait', ms: 50 }],
    });
    assert.equal(saved.enabled, true);

    // A new process (new store instance) sees the same JSON.
    const reloaded = new MacroStore({ path });
    const found = reloaded.find('open_settings');
    assert.ok(found);
    assert.deepEqual(found.steps.map((step) => step.op), ['click', 'wait']);

    reloaded.setEnabled('open_settings', false);
    const again = new MacroStore({ path });
    assert.equal(again.find('open_settings').enabled, false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('macro store rejects invalid and duplicate macros', () => {
  const { dir, path } = tempStore();
  try {
    const store = new MacroStore({ path });
    assert.throws(
      () => store.add({ name: 'bad name!', steps: [{ op: 'wait', ms: 1 }] }),
      (error) => error.code === 'E_PROTO',
    );
    assert.throws(
      () => store.add({ name: 'bad_steps', steps: [{ op: 'eval', code: 'x' }] }),
      (error) => error.code === 'E_PROTO',
    );
    assert.throws(
      () => store.add({ name: 'too_far', steps: [{ op: 'click', x: -1, y: 0 }] }),
      (error) => error.code === 'E_PROTO',
    );
    store.add({ name: 'ok_macro', steps: [{ op: 'wait', ms: 1 }] });
    assert.throws(
      () => store.add({ name: 'ok_macro', steps: [{ op: 'wait', ms: 1 }] }),
      (error) => error.code === 'E_CONFLICT',
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('steps normalize and {{text}} substitution', () => {
  const steps = normalizeSteps([
    { op: 'key', keys: ['ctrl', 's'] },
    { op: 'type', text: 'hello {{text}}' },
    { op: 'scroll', amount: -3 },
    { op: 'drag', x1: 0, y1: 0, x2: 5, y2: 6 },
  ]);
  assert.equal(steps.length, 4);
  const filled = substituteText(steps, 'VHCU');
  assert.equal(filled[1].text, 'hello VHCU');
  assert.equal(steps[1].text, 'hello {{text}}', 'original steps must stay immutable');
});

test('category defaults, updates and legacy migration', () => {
  const { dir, path } = tempStore();
  try {
    const legacy = {
      version: 1,
      macros: [{ name: 'old_macro', description: '', enabled: true, steps: [{ op: 'wait', ms: 1 }] }],
    };
    writeFileSync(path, JSON.stringify(legacy), 'utf8');
    const store = new MacroStore({ path });
    assert.equal(store.find('old_macro').category, '我的自定义宏');
    assert.equal(JSON.parse(readFileSync(path, 'utf8')).macros[0].category, '我的自定义宏');

    store.add({ name: 'folder_learn', category: 'explorer', steps: [{ op: 'wait', ms: 1 }] });
    assert.equal(store.find('folder_learn').category, 'explorer');

    store.update('folder_learn', { steps: [{ op: 'key', keys: ['win', 'e'] }], description: 'keyboard flow' });
    const reloaded = new MacroStore({ path });
    const updated = reloaded.find('folder_learn');
    assert.equal(updated.description, 'keyboard flow');
    assert.deepEqual(updated.steps, [{ op: 'key', keys: ['win', 'e'] }]);
    assert.equal(updated.category, 'explorer');
    assert.equal(updated.enabled, true);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('built-in category/macros are protected; custom groups CRUD', () => {
  const { dir, path } = tempStore();
  try {
    const store = new MacroStore({ path });
    assert.throws(
      () => store.deleteCategory('系统基础宏'),
      (error) => error.code === 'E_DENIED',
    );
    assert.throws(
      () => store.update('macro_launch_app', { description: 'x' }),
      (error) => error.code === 'E_DENIED',
    );
    assert.throws(
      () => store.remove('macro_launch_app'),
      (error) => error.code === 'E_DENIED',
    );
    // Built-in macros can still be enabled/disabled.
    store.setEnabled('macro_launch_app', false);
    assert.equal(store.find('macro_launch_app').enabled, false);

    store.addCategory('demo_group');
    assert.ok(store.categoriesList().some((category) => category.name === 'demo_group'));
    store.add({ name: 'in_demo', category: 'demo_group', steps: [{ op: 'wait', ms: 1 }] });
    assert.throws(
      () => store.deleteCategory('demo_group'),
      (error) => error.code === 'E_STATE',
    );
    store.remove('in_demo');
    store.deleteCategory('demo_group');
    assert.equal(store.categoriesList().some((category) => category.name === 'demo_group'), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('macroVariables derives every {{name}} placeholder', () => {
  const store = new MacroStore({ path: join(mkdtempSync(join(tmpdir(), 'vhcu-vars-')), 'macros.json') });
  try {
    const launch = store.list().find((macro) => macro.name === 'macro_create_folder');
    assert.deepEqual(launch.variables, ['path', 'folder_name']);
    const search = store.list().find((macro) => macro.name === 'macro_browser_search');
    assert.deepEqual(search.variables, ['keyword']);
  } finally {
    rmSync(dirname(store.path), { recursive: true, force: true });
  }
});

test('mouse macro placeholders and coordinate normalization', () => {
  const { dir, path } = tempStore();
  try {
    const store = new MacroStore({ path });
    const listed = store.list();
    const doubleClick = listed.find((macro) => macro.name === 'macro_mouse_double_click');
    assert.deepEqual(doubleClick.variables, ['x', 'y']);
    assert.equal(doubleClick.steps[0].op, 'double_click');
    assert.equal(doubleClick.steps[0].x, '{{x}}');
    assert.equal(doubleClick.steps[0].y, '{{y}}');
    const drag = listed.find((macro) => macro.name === 'macro_window_drag');
    assert.deepEqual(drag.variables, ['x1', 'y1', 'x2', 'y2']);
    assert.equal(drag.steps[0].op, 'drag');

    const custom = store.add({
      name: 'mixed_coords',
      steps: [
        { op: 'double_click', x: '{{x}}', y: 10 },
        { op: 'drag', x1: 0, y1: 0, x2: '{{x2}}', y2: 20 },
      ],
    });
    assert.deepEqual(custom.steps[0], { op: 'double_click', x: '{{x}}', y: 10 });
    assert.deepEqual(custom.steps[1], { op: 'drag', x1: 0, y1: 0, x2: '{{x2}}', y2: 20 });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('P16 target_app is normalized and recommended by active window', () => {
  const base = {
    name: 'open_notepad',
    description: 'notepad',
    category: 'global',
    steps: [{ op: 'wait', ms: 100 }],
    enabled: true,
  };
  const generic = normalizeMacro({ ...base, name: 'generic_macro', target_app: '' });
  const notepad = normalizeMacro({ ...base, name: 'notepad_macro', target_app: '  Notepad ' });
  const chrome = normalizeMacro({ ...base, name: 'chrome_macro', target_app: 'chrome' });
  assert.equal(generic.target_app, null);
  assert.equal(notepad.target_app, 'Notepad');
  assert.deepEqual(
    recommendMacros([generic, notepad, chrome, { ...chrome, name: 'off', enabled: false }], '未命名 - 记事本 Notepad').map((m) => m.name),
    ['generic_macro', 'notepad_macro'],
  );
  assert.deepEqual(
    recommendMacros([generic, notepad, chrome], 'Google Chrome').map((m) => m.name),
    ['generic_macro', 'chrome_macro'],
  );
  assert.deepEqual(recommendMacros([notepad, chrome], null).map((m) => m.name), []);
});

test('P17.1 global keyboard macros use expected key chords', () => {
  const expected = {
    macro_switch_window: ['alt', 'tab'],
    macro_show_desktop: ['win', 'd'],
    macro_copy: ['ctrl', 'c'],
    macro_paste: ['ctrl', 'v'],
    macro_undo: ['ctrl', 'z'],
    macro_select_all: ['ctrl', 'a'],
    macro_browser_new_tab: ['ctrl', 't'],
    macro_browser_close_tab: ['ctrl', 'w'],
  };
  for (const [name, keys] of Object.entries(expected)) {
    const seed = seedMacros().find((macro) => macro.name === name);
    assert.ok(seed, `${name} missing`);
    assert.deepEqual(seed.steps, [{ op: 'key', keys }]);
    assert.equal(seed.target_app, null);
  }
});

test('P17.3 host title exclusion and word-boundary target matching', () => {
  const generic = { name: 'g', enabled: true, target_app: null };
  const calc = { name: 'calc', enabled: true, target_app: '计算器' };
  const notepad = { name: 'np', enabled: true, target_app: 'notepad' };
  assert.equal(isHostWindowTitle('计算器 - DeepSeek Harness'), true);
  assert.equal(isHostWindowTitle('DSH 计算器测试'), true);
  assert.equal(isHostWindowTitle('Calculator'), false);
  assert.deepEqual(
    recommendMacros([generic, calc], '计算器 - DeepSeek Harness').map((m) => m.name),
    ['g'],
  );
  assert.deepEqual(recommendMacros([generic, calc], '计算器').map((m) => m.name), ['g', 'calc']);
  assert.equal(targetMatchesTitle('calculator', 'calc'), false);
  assert.equal(targetMatchesTitle('Calculator', 'Calculator'), true);
  assert.equal(targetMatchesTitle('Google Chrome', 'chrome'), true);
  assert.equal(targetMatchesTitle('未命名 - 记事本 Notepad', 'notepad'), true);
});

test('P20 macro failures carry the visual slow-lane recovery hint', () => {
  assert.match(MACRO_RECOVERY_HINT, /窗口位置改变/);
  assert.match(MACRO_RECOVERY_HINT, /vhcu_get_full_frame/);
  assert.match(MACRO_RECOVERY_HINT, /视觉慢车道/);
  assert.equal(withMacroRecoveryHint(''), MACRO_RECOVERY_HINT);
  assert.match(withMacroRecoveryHint('原错误'), /^原错误；⚠️ 宏执行失败/);
  assert.equal(withMacroRecoveryHint(MACRO_RECOVERY_HINT), MACRO_RECOVERY_HINT);
});


test('P24 macro steps are capped at 100', () => {
  assert.equal(MAX_STEPS, 100);
  const steps = Array.from({ length: 101 }, () => ({ op: 'wait', ms: 1 }));
  assert.throws(() => normalizeSteps(steps), /1\.\.100/);
});


test('P24 legacy macros over 100 steps are migrated, not marked broken', () => {
  const { dir, path } = tempStore();
  try {
    const steps = Array.from({ length: 101 }, (_, index) => ({ op: 'wait', ms: index + 1 }));
    writeFileSync(
      path,
      JSON.stringify({
        version: 2,
        categories: [{ name: '我的自定义宏', builtin: false }],
        macros: [{ name: 'legacy_50', category: '我的自定义宏', enabled: true, steps }],
      }),
      'utf8',
    );
    const store = new MacroStore({ path });
    const macro = store.find('legacy_50');
    assert.ok(macro);
    assert.equal(macro.steps.length, 100);
    assert.equal(JSON.parse(readFileSync(path, 'utf8')).macros[0].steps.length, 100);
    assert.equal(existsSync(`${path}.broken`), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('P23 destructive macro field is seeded, persisted and normalized', () => {
  const { dir, path } = tempStore();
  try {
    const store = new MacroStore({ path });
    assert.equal(store.find('macro_close_active_window').is_destructive, true);
    assert.equal(store.find('macro_launch_app').is_destructive, false);
    store.add({
      name: 'my_destructive',
      category: '我的自定义宏',
      description: 'test',
      is_destructive: true,
      steps: [{ op: 'wait', ms: 10 }],
    });
    const reloaded = new MacroStore({ path });
    assert.equal(reloaded.find('my_destructive').is_destructive, true);
    assert.equal(reloaded.find('macro_close_active_window').is_destructive, true);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
