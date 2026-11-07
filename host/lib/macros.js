/**
 * @file macros.js — persistent user macro store for dsh-vhcu.
 *
 * Macros are plain JSON: { name, description, steps[], enabled }. The file
 * lives under %LOCALAPPDATA%\dsh-vhcu\macros.json (override: VHCU_MACROS_PATH),
 * is seeded once with the two built-in workflows, and survives DSH restarts.
 *
 * Step vocabulary (kept deliberately small; no nested macros, so execution can
 * never recurse):
 *   { op:'click', x, y }
 *   { op:'type', text }          ({{text}} is replaced by the caller's input)
 *   { op:'key', keys:[...] }
 *   { op:'scroll', amount }
 *   { op:'drag', x1, y1, x2, y2 }
 *   { op:'wait', ms }
 */

import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';

const MAX_MACROS = 100;
const MAX_STEPS = 100;
const MAX_NAME = 64;
const MAX_DESC = 240;

/** P20: mandatory recovery hint whenever a macro execution path fails. */
export const MACRO_RECOVERY_HINT =
  '⚠️ 宏执行失败：可能由于窗口位置改变或加载延迟导致。' +
  '建议：立刻切换为视觉慢车道（调用 vhcu_get_full_frame 观察屏幕），重新规划动作并执行。';

export function withMacroRecoveryHint(hint) {
  const base = String(hint ?? '').trim();
  if (base.includes('切换为视觉慢车道')) return base;
  return base ? `${base}；${MACRO_RECOVERY_HINT}` : MACRO_RECOVERY_HINT;
}
const MAX_TEXT = 2000;
const MAX_CATEGORY = 40;

export const STEP_OPS = Object.freeze([
  'click',
  'double_click',
  'right_click',
  'type',
  'key',
  'scroll',
  'drag',
  'middle_drag',
  'wait',
]);

export function defaultMacrosPath(env = process.env) {
  if (env.VHCU_MACROS_PATH) return env.VHCU_MACROS_PATH;
  const base = env.LOCALAPPDATA || env.XDG_DATA_HOME || join(homedir(), '.local', 'share');
  return join(base, 'dsh-vhcu', 'macros.json');
}

export const BUILTIN_CATEGORY = '系统基础宏';
export const DEFAULT_CATEGORY = '我的自定义宏';
const MAX_CATEGORIES = 40;

export function seedCategories() {
  return [
    { name: BUILTIN_CATEGORY, builtin: true },
    { name: DEFAULT_CATEGORY, builtin: false },
  ];
}

export function seedMacros() {
  return [
    {
      name: 'macro_launch_app',
      description: 'Win+R 启动应用并输入文本：win+r → {{app_name}} → 回车 → 1s → {{text}}',
      category: BUILTIN_CATEGORY,
      builtin: true,
      enabled: true,
      steps: [
        { op: 'key', keys: ['win', 'r'] },
        { op: 'wait', ms: 350 },
        { op: 'type', text: '{{app_name}}' },
        { op: 'key', keys: ['enter'] },
        { op: 'wait', ms: 1000 },
        { op: 'type', text: '{{text}}' },
      ],
    },
    {
      name: 'macro_create_folder',
      description: '资源管理器新建文件夹：Win+E → Ctrl+L → {{path}} → 回车 → Ctrl+Shift+N → {{folder_name}} → 回车',
      category: BUILTIN_CATEGORY,
      builtin: true,
      enabled: true,
      steps: [
        { op: 'key', keys: ['win', 'e'] },
        { op: 'wait', ms: 1500 },
        { op: 'key', keys: ['ctrl', 'l'] },
        { op: 'wait', ms: 300 },
        { op: 'type', text: '{{path}}' },
        { op: 'key', keys: ['enter'] },
        { op: 'wait', ms: 800 },
        { op: 'key', keys: ['ctrl', 'shift', 'n'] },
        { op: 'wait', ms: 600 },
        { op: 'type', text: '{{folder_name}}' },
        { op: 'key', keys: ['enter'] },
        { op: 'wait', ms: 500 },
      ],
    },
    {
      name: 'macro_browser_search',
      description: '打开默认浏览器并搜索：Win+R → 浏览器 → Ctrl+L → {{keyword}} → 回车',
      category: BUILTIN_CATEGORY,
      builtin: true,
      enabled: true,
      steps: [
        { op: 'key', keys: ['win', 'r'] },
        { op: 'wait', ms: 350 },
        { op: 'type', text: 'https://www.bing.com' },
        { op: 'key', keys: ['enter'] },
        { op: 'wait', ms: 1200 },
        { op: 'key', keys: ['ctrl', 'l'] },
        { op: 'wait', ms: 200 },
        { op: 'type', text: '{{keyword}}' },
        { op: 'key', keys: ['enter'] },
        { op: 'wait', ms: 500 },
      ],
    },
    {
      name: 'macro_mouse_double_click',
      description: '在 ({{x}},{{y}}) 双击左键（支持 is_relative_to_image 坐标映射）',
      category: BUILTIN_CATEGORY,
      builtin: true,
      enabled: true,
      steps: [{ op: 'double_click', x: '{{x}}', y: '{{y}}' }],
    },
    {
      name: 'macro_mouse_right_click',
      description: '在 ({{x}},{{y}}) 单击右键（支持 is_relative_to_image 坐标映射）',
      category: BUILTIN_CATEGORY,
      builtin: true,
      enabled: true,
      steps: [{ op: 'right_click', x: '{{x}}', y: '{{y}}' }],
    },
    {
      name: 'macro_window_drag',
      description:
        '按住左键从 ({{x1}},{{y1}}) 拖到 ({{x2}},{{y2}})（支持 is_relative_to_image）。' +
        '⚠️ 不要用它拖拽系统窗口（Explorer/UWP/WebView 会屏蔽模拟拖拽）；系统窗口请用 macro_snap_window_left/right。可用于画图等应用内部拖拽。',
      category: BUILTIN_CATEGORY,
      builtin: true,
      enabled: true,
      steps: [{ op: 'drag', x1: '{{x1}}', y1: '{{y1}}', x2: '{{x2}}', y2: '{{y2}}' }],
    },
    {
      name: 'macro_snap_window_left',
      description: '发送 Win+Left，把当前窗口贴靠到左半屏（移动系统窗口优先用快捷键，不要用鼠标拖拽）',
      category: BUILTIN_CATEGORY,
      builtin: true,
      enabled: true,
      steps: [{ op: 'key', keys: ['win', 'left'] }],
    },
    {
      name: 'macro_snap_window_right',
      description: '发送 Win+Right，把当前窗口贴靠到右半屏（移动系统窗口优先用快捷键，不要用鼠标拖拽）',
      category: BUILTIN_CATEGORY,
      builtin: true,
      enabled: true,
      steps: [{ op: 'key', keys: ['win', 'right'] }],
    },
    {
      name: 'macro_close_active_window',
      description:
        '发送 Alt+F4 关闭当前活动窗口。仅用于关闭你临时打开、任务完成后不再需要的窗口；不要关闭用户自己的窗口。' +
        '执行前会请求用户审批（HITL）；用户拒绝时不要重试或绕过。任务完成后请顺手调用本宏，保持用户桌面整洁。',
      category: BUILTIN_CATEGORY,
      builtin: true,
      enabled: true,
      is_destructive: true,
      steps: [{ op: 'key', keys: ['alt', 'f4'] }],
    },
    {
      name: 'save_file',
      description: '发送 Ctrl+S 保存当前文件',
      category: BUILTIN_CATEGORY,
      builtin: true,
      enabled: true,
      steps: [{ op: 'key', keys: ['ctrl', 's'] }],
    },
    {
      name: 'macro_switch_window',
      description: '切换窗口（Alt+Tab）',
      category: BUILTIN_CATEGORY,
      builtin: true,
      enabled: true,
      target_app: null,
      steps: [{ op: 'key', keys: ['alt', 'tab'] }],
    },
    {
      name: 'macro_show_desktop',
      description: '显示桌面（Win+D）',
      category: BUILTIN_CATEGORY,
      builtin: true,
      enabled: true,
      target_app: null,
      steps: [{ op: 'key', keys: ['win', 'd'] }],
    },
    {
      name: 'macro_copy',
      description: '复制（Ctrl+C）',
      category: BUILTIN_CATEGORY,
      builtin: true,
      enabled: true,
      target_app: null,
      steps: [{ op: 'key', keys: ['ctrl', 'c'] }],
    },
    {
      name: 'macro_paste',
      description: '粘贴（Ctrl+V）',
      category: BUILTIN_CATEGORY,
      builtin: true,
      enabled: true,
      target_app: null,
      steps: [{ op: 'key', keys: ['ctrl', 'v'] }],
    },
    {
      name: 'macro_undo',
      description: '撤销（Ctrl+Z）',
      category: BUILTIN_CATEGORY,
      builtin: true,
      enabled: true,
      target_app: null,
      steps: [{ op: 'key', keys: ['ctrl', 'z'] }],
    },
    {
      name: 'macro_select_all',
      description: '全选（Ctrl+A）',
      category: BUILTIN_CATEGORY,
      builtin: true,
      enabled: true,
      target_app: null,
      steps: [{ op: 'key', keys: ['ctrl', 'a'] }],
    },
    {
      name: 'macro_browser_new_tab',
      description: '浏览器新建标签页（Ctrl+T）',
      category: BUILTIN_CATEGORY,
      builtin: true,
      enabled: true,
      target_app: null,
      steps: [{ op: 'key', keys: ['ctrl', 't'] }],
    },
    {
      name: 'macro_browser_close_tab',
      description: '关闭标签页（Ctrl+W）',
      category: BUILTIN_CATEGORY,
      builtin: true,
      enabled: true,
      target_app: null,
      steps: [{ op: 'key', keys: ['ctrl', 'w'] }],
    },
  ];
}

/** Unique {{variable}} names referenced by type steps, in first-use order. */
export function macroVariables(steps) {
  const names = [];
  const pattern = /\{\{([a-zA-Z0-9_]+)\}\}/g;
  for (const step of steps ?? []) {
    if (!step || typeof step !== 'object') continue;
    for (const value of Object.values(step)) {
      if (typeof value !== 'string' || !value.includes('{{')) continue;
      for (const match of value.matchAll(pattern)) {
        if (!names.includes(match[1])) names.push(match[1]);
      }
    }
  }
  return names;
}

function fail(code, message, hint) {
  throw Object.assign(new Error(message), { code, hint });
}

function int(value, label, min, max) {
  const number = Number(value);
  if (!Number.isInteger(number) || number < min || number > max) {
    fail('E_PROTO', `${label} must be an integer in ${min}..${max}`);
  }
  return number;
}

export function normalizeName(raw) {
  const name = String(raw ?? '').trim();
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{1,63}$/.test(name) || name.length > MAX_NAME) {
    fail('E_PROTO', 'macro name must be 2-64 chars: letters, digits, _ or -');
  }
  return name;
}

function coord(value, label) {
  if (typeof value === 'string' && /\{\{[a-zA-Z0-9_]+\}\}/.test(value)) {
    const text = value.trim();
    if (text.length > 40) fail('E_PROTO', `${label} placeholder is too long`);
    return text;
  }
  return int(value, label, 0, 100000);
}

export function normalizeSteps(raw) {
  if (!Array.isArray(raw) || raw.length === 0 || raw.length > MAX_STEPS) {
    fail('E_PROTO', `steps must be an array of 1..${MAX_STEPS} operations`);
  }
  return raw.map((step, index) => {
    if (!step || typeof step !== 'object' || !STEP_OPS.includes(step.op)) {
      fail('E_PROTO', `step ${index + 1}: op must be one of ${STEP_OPS.join(', ')}`);
    }
    switch (step.op) {
      case 'click':
        return { op: 'click', x: coord(step.x, 'x'), y: coord(step.y, 'y') };
      case 'double_click':
        return { op: 'double_click', x: coord(step.x, 'x'), y: coord(step.y, 'y') };
      case 'right_click':
        return { op: 'right_click', x: coord(step.x, 'x'), y: coord(step.y, 'y') };
      case 'type': {
        const text = String(step.text ?? '');
        if (text.length > MAX_TEXT) fail('E_PROTO', `step ${index + 1}: text exceeds ${MAX_TEXT} chars`);
        return { op: 'type', text };
      }
      case 'key': {
        const keys = step.keys;
        if (!Array.isArray(keys) || keys.length < 1 || keys.length > 4) {
          fail('E_PROTO', `step ${index + 1}: keys must be an array of 1..4 strings`);
        }
        return { op: 'key', keys: keys.map((key) => String(key).slice(0, 16)) };
      }
      case 'scroll':
        return { op: 'scroll', amount: int(step.amount, 'amount', -20, 20) };
      case 'drag':
        return {
          op: 'drag',
          x1: coord(step.x1, 'x1'),
          y1: coord(step.y1, 'y1'),
          x2: coord(step.x2, 'x2'),
          y2: coord(step.y2, 'y2'),
        };
      case 'middle_drag':
        return {
          op: 'middle_drag',
          x1: coord(step.x1, 'x1'),
          y1: coord(step.y1, 'y1'),
          x2: coord(step.x2, 'x2'),
          y2: coord(step.y2, 'y2'),
        };
      case 'wait':
        return { op: 'wait', ms: int(step.ms, 'ms', 0, 5000) };
      default:
        return fail('E_PROTO', `step ${index + 1}: unsupported op`);
    }
  });
}

export function normalizeCategory(raw) {
  const category = String(raw ?? '').trim() || DEFAULT_CATEGORY;
  if (category.length > MAX_CATEGORY) {
    fail('E_PROTO', `category must be at most ${MAX_CATEGORY} chars`);
  }
  return category;
}

export function normalizeMacro(raw, { requireEnabled = false } = {}) {
  if (!raw || typeof raw !== 'object') fail('E_PROTO', 'macro must be an object');
  const name = normalizeName(raw.name);
  const description = String(raw.description ?? '').slice(0, MAX_DESC);
  const category = normalizeCategory(raw.category);
  const target_app = String(raw.target_app ?? '').trim().slice(0, 80) || null;
  const steps = normalizeSteps(raw.steps);
  const enabled = requireEnabled ? Boolean(raw.enabled) : raw.enabled !== false;
  const builtin = Boolean(raw.builtin);
  const is_destructive = Boolean(raw.is_destructive);
  return { name, description, category, target_app, builtin, steps, enabled, is_destructive };
}

function normalizeCategoryEntry(raw) {
  if (!raw || typeof raw !== 'object') fail('E_PROTO', 'category must be an object');
  return { name: normalizeCategory(raw.name), builtin: Boolean(raw.builtin) || raw.name === BUILTIN_CATEGORY };
}

/** P17.3: DSH/Electron host titles must never select an app-specific macro. */
const HOST_TITLE_PATTERNS = [
  /deepseek[\s-]*harness/i,
  /deepseek\s+desktop/i,
  /\bdsh\b/i,
];

export function isHostWindowTitle(activeTitle) {
  const title = String(activeTitle ?? '').trim();
  if (!title) return false;
  return HOST_TITLE_PATTERNS.some((pattern) => pattern.test(title));
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Word-boundary match; "calc" must not match "calculator". */
export function targetMatchesTitle(activeTitle, targetApp) {
  const title = String(activeTitle ?? '').trim();
  const target = String(targetApp ?? '').trim();
  if (!title || !target) return false;
  const pattern = new RegExp(
    `(^|[^\\p{L}\\p{N}])${escapeRegExp(target)}(?=$|[^\\p{L}\\p{N}])`,
    'iu',
  );
  return pattern.test(title);
}

/** P16/P17.3: generic macros plus strictly matched app macros. */
export function recommendMacros(macros, activeTitle) {
  const title = String(activeTitle ?? '').trim();
  const host = isHostWindowTitle(title);
  return (Array.isArray(macros) ? macros : []).filter(
    (macro) =>
      macro?.enabled !== false &&
      (!macro.target_app || (!host && targetMatchesTitle(title, macro.target_app))),
  );
}

function parsePayload(text) {
  const data = JSON.parse(text);
  if (!data || typeof data !== 'object' || !Array.isArray(data.macros)) {
    fail('E_PROTO', 'macros.json must contain {version, macros[]}');
  }
  const macros = data.macros.map((macro) => {
    const raw = macro && typeof macro === 'object' ? { ...macro } : macro;
    // P22.2: legacy stores may have up to 50 steps; migrate by keeping the
    // first 30 instead of treating the whole file as corrupted.
    if (raw && Array.isArray(raw.steps) && raw.steps.length > MAX_STEPS) {
      raw.steps = raw.steps.slice(0, MAX_STEPS);
    }
    return normalizeMacro(raw, { requireEnabled: true });
  });
  const categories = Array.isArray(data.categories)
    ? data.categories.map(normalizeCategoryEntry)
    : [];
  return { categories, macros };
}

export class MacroStore {
  constructor({ path = defaultMacrosPath() } = {}) {
    this.path = path;
    this.revision = 0;
    this.macros = [];
    this.categories = seedCategories();
    this.load();
  }

  load() {
    let migrated = false;
    try {
      const text = readFileSync(this.path, 'utf8');
      const data = JSON.parse(text);
      const parsed = parsePayload(text);
      this.macros = parsed.macros;
      this.categories = parsed.categories;
      if (!Array.isArray(data.categories)) migrated = true;
      if (
        Array.isArray(data.macros) &&
        data.macros.some((macro) => Array.isArray(macro?.steps) && macro.steps.length > MAX_STEPS)
      ) {
        migrated = true;
      }

      // P11.8: fold the two dead hardcoded macros into parameterized built-ins.
      const legacy = new Set(['open_notepad_and_type', 'macro_create_vhcu_folder']);
      const before = this.macros.length;
      this.macros = this.macros.filter((macro) => !(legacy.has(macro.name) && macro.builtin === false));
      if (this.macros.length !== before) migrated = true;

      if (!this.categories.some((category) => category.name === BUILTIN_CATEGORY)) {
        this.categories.unshift(...seedCategories());
        migrated = true;
      }
      for (const seedCategory of seedCategories()) {
        if (this.categories.some((category) => category.name === seedCategory.name)) continue;
        this.categories.push({ ...seedCategory });
        migrated = true;
      }
      for (const category of this.categories) {
        if (category.name === BUILTIN_CATEGORY && !category.builtin) {
          category.builtin = true;
          migrated = true;
        }
      }
      for (const macro of this.macros) {
        if (macro.name === 'macro_close_active_window' && !macro.is_destructive) {
          macro.is_destructive = true;
          migrated = true;
        }
        if (macro.category === BUILTIN_CATEGORY && !macro.builtin) {
          macro.builtin = true;
          migrated = true;
        }
        if (!this.categories.some((category) => category.name === macro.category)) {
          this.categories.push({ name: macro.category, builtin: false });
          migrated = true;
        }
      }
      // Built-ins ship with the plugin: restore missing seeds and upgrade any
      // same-named legacy macro into the built-in category (read-only).
      for (const seed of seedMacros()) {
        const existing = this.find(seed.name);
        if (!existing) {
          this.macros.push(normalizeMacro(seed, { requireEnabled: true }));
          migrated = true;
          continue;
        }
        if (!existing.builtin || existing.category !== BUILTIN_CATEGORY) {
          existing.builtin = true;
          existing.category = BUILTIN_CATEGORY;
          migrated = true;
        }
      }
      // P11.8 cleanup: the old default category is empty after migration.
      if (this.categories.some((category) => category.name === 'global') &&
          !this.macros.some((macro) => macro.category === 'global')) {
        this.categories = this.categories.filter((category) => category.name !== 'global');
        migrated = true;
      }
      this.categories.sort((left, right) => (left.builtin === right.builtin ? 0 : left.builtin ? -1 : 1));
      if (migrated) this.persist();
    } catch (error) {
      if (error?.code !== 'ENOENT') {
        // A corrupted store must not brick the plugin: keep a copy and reseed.
        try {
          renameSync(this.path, `${this.path}.broken`);
        } catch {
          // ignore
        }
      }
      this.categories = seedCategories();
      this.macros = seedMacros().map((macro) => normalizeMacro(macro, { requireEnabled: true }));
      this.persist();
    }
    return this.list();
  }

  list() {
    return this.macros.map((macro) => ({
      ...macro,
      variables: macroVariables(macro.steps),
      steps: macro.steps.map((step) => ({ ...step, keys: step.keys ? [...step.keys] : undefined })),
    }));
  }

  categoriesList() {
    return this.categories.map((category) => ({
      ...category,
      count: this.macros.filter((macro) => macro.category === category.name).length,
    }));
  }

  find(name) {
    return this.macros.find((macro) => macro.name === name) ?? null;
  }

  addCategory(rawName) {
    const name = normalizeCategory(rawName);
    if (this.categories.some((category) => category.name === name)) {
      fail('E_CONFLICT', `category ${name} already exists`);
    }
    if (this.categories.length >= MAX_CATEGORIES) fail('E_LIMIT', `at most ${MAX_CATEGORIES} categories`);
    const category = { name, builtin: false };
    this.categories.push(category);
    this.persist();
    this.revision += 1;
    return category;
  }

  deleteCategory(rawName) {
    const name = normalizeCategory(rawName);
    const category = this.categories.find((entry) => entry.name === name);
    if (!category) fail('E_NOT_FOUND', `category ${name} not found`);
    if (category.builtin || name === BUILTIN_CATEGORY) {
      fail('E_DENIED', `${BUILTIN_CATEGORY} 是内置分类，不可删除`, '内置分类随插件提供，只能启用/禁用其中的宏');
    }
    const used = this.macros.filter((macro) => macro.category === name).length;
    if (used > 0) fail('E_STATE', `分类 ${name} 下还有 ${used} 个宏`, '先删除或移走这些宏，再删除分类');
    this.categories = this.categories.filter((entry) => entry.name !== name);
    this.persist();
    this.revision += 1;
    return category;
  }

  ensureCategory(category) {
    if (this.categories.some((entry) => entry.name === category)) return;
    this.categories.push({ name: category, builtin: false });
  }

  add(raw) {
    if (this.macros.length >= MAX_MACROS) fail('E_LIMIT', `at most ${MAX_MACROS} macros`);
    const macro = normalizeMacro({ ...raw, builtin: false });
    if (this.find(macro.name)) fail('E_CONFLICT', `macro ${macro.name} already exists`, '换一个名称，或先删除旧宏');
    this.ensureCategory(macro.category);
    this.macros.push(macro);
    this.persist();
    this.revision += 1;
    return macro;
  }

  update(name, patch = {}) {
    const index = this.macros.findIndex((macro) => macro.name === name);
    if (index < 0) fail('E_NOT_FOUND', `macro ${name} not found`);
    if (this.macros[index].builtin) {
      fail('E_DENIED', '内置宏只读（可启用/禁用），不可编辑', '复制一份为用户宏后再修改');
    }
    const merged = normalizeMacro({ ...this.macros[index], ...patch, name, builtin: false });
    this.ensureCategory(merged.category);
    this.macros[index] = merged;
    this.persist();
    this.revision += 1;
    return merged;
  }

  setEnabled(name, enabled) {
    const macro = this.find(name);
    if (!macro) fail('E_NOT_FOUND', `macro ${name} not found`);
    macro.enabled = Boolean(enabled);
    this.persist();
    this.revision += 1;
    return macro;
  }

  remove(name) {
    const index = this.macros.findIndex((macro) => macro.name === name);
    if (index < 0) fail('E_NOT_FOUND', `macro ${name} not found`);
    if (this.macros[index].builtin) {
      fail('E_DENIED', '内置宏不可删除', '只能启用/禁用；如需修改请复制为自定义宏');
    }
    const [removed] = this.macros.splice(index, 1);
    this.persist();
    this.revision += 1;
    return removed;
  }

  persist() {
    mkdirSync(dirname(this.path), { recursive: true });
    const tmp = `${this.path}.tmp-${process.pid}`;
    writeFileSync(
      tmp,
      `${JSON.stringify({ version: 2, categories: this.categories, macros: this.macros }, null, 2)}\n`,
      'utf8',
    );
    renameSync(tmp, this.path);
  }
}

/**
 * @deprecated P11.8: variables are substituted in the Python daemon. Kept for
 * callers/tests that need a local preview; use replaceVariables for multi-var.
 */
export function substituteText(steps, text = '') {
  return steps.map((step) =>
    step.op === 'type' && step.text.includes('{{text}}') ? { ...step, text: step.text.replaceAll('{{text}}', String(text)) } : step,
  );
}

export { MAX_MACROS, MAX_STEPS };
