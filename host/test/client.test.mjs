import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const CLIENT = join(HERE, '..', '..', 'lib', 'client.js');

function loadClientModule() {
  const source = readFileSync(CLIENT, 'utf8');
  let loaded = null;
  const sandbox = {
    window: {
      __ModuleLoader__: {
        load(definition) {
          loaded = definition;
        },
      },
    },
    document: {
      getElementById: () => null,
      createElement: () => ({ id: '', textContent: '', appendChild() {} }),
      head: { appendChild() {} },
    },
    EventSource: class {},
    fetch: async () => ({ json: async () => ({ ok: true }) }),
    console,
    setTimeout,
    clearTimeout,
  };
  vm.runInNewContext(source, sandbox, { filename: 'client.js' });
  assert.ok(loaded, 'client must call window.__ModuleLoader__.load');
  return loaded;
}

function reactStub() {
  const createElement = (type, props, ...children) => ({ type, props: props ?? {}, children });
  return {
    createElement,
    useState: (initial) => [typeof initial === 'function' ? initial() : initial, () => {}],
    useEffect: () => {},
    useCallback: (fn) => fn,
    useRef: () => ({ current: null }),
  };
}

test('client bundle exposes the cordis module shape and registers the tab', () => {
  const loaded = loadClientModule();
  assert.equal(loaded.id, 'dsh-vhcu');
  const module = loaded.factory((name) => {
    if (name === 'react') return reactStub();
    throw new Error(`unexpected require ${name}`);
  });
  assert.equal(module.name, 'dsh-vhcu');
  assert.deepEqual([...module.inject], ['betterSidebar']);
  assert.equal(typeof module.apply, 'function');

  let descriptor = null;
  const ctx = {
    betterSidebar: {
      registerTab(value) {
        descriptor = value;
        return () => {};
      },
    },
    effect(body) {
      body();
    },
  };
  module.apply(ctx);
  assert.ok(descriptor, 'registerTab was not called');
  assert.equal(descriptor.id, 'vhcu:monitor');
  assert.equal(descriptor.title, 'VHCU');
  assert.equal(descriptor.single, true);
  assert.equal(typeof descriptor.component, 'function');
  assert.equal(descriptor.icon, undefined);
});

test('panel renders four data tabs, gear and the dark shell', () => {
  const loaded = loadClientModule();
  const module = loaded.factory((name) => {
    if (name === 'react') return reactStub();
    throw new Error(`unexpected require ${name}`);
  });
  let descriptor = null;
  module.apply({
    betterSidebar: { registerTab: (value) => ((descriptor = value), () => {}) },
    effect: (body) => body(),
  });
  const shell = descriptor.component({});
  const tree = shell.type(shell.props); // unwrap <Panel /> into its rendered tree
  assert.equal(tree.props.className, 'vhcu-root');
  const text = JSON.stringify(tree);
  for (const needle of ['VHCU', 'Vision', 'Action', 'Hear', '融合', '⚙', '开启视觉', '还没有视觉事件', 'SSE', 'vhcu-tab-dot']) {
    assert.ok(text.includes(needle), `panel is missing ${needle}`);
  }
});

test('apply is a no-op when betterSidebar is absent', () => {
  const loaded = loadClientModule();
  const module = loaded.factory((name) => {
    if (name === 'react') return reactStub();
    throw new Error(`unexpected require ${name}`);
  });
  assert.doesNotThrow(() => module.apply({}));
});

test('Hear tab exposes the enable button and status dot', () => {
  const loaded = loadClientModule();
  const module = loaded.factory((name) => {
    if (name === 'react') return reactStub();
    throw new Error(`unexpected require ${name}`);
  });
  let descriptor = null;
  module.apply({
    betterSidebar: { registerTab: (value) => ((descriptor = value), () => {}) },
    effect: (body) => body(),
  });
  const shell = descriptor.component({ initialTab: 'hear' });
  const tree = shell.type(shell.props);
  const text = JSON.stringify(tree);
  for (const needle of ['vhcu-hear-bar', '开启听觉', '系统内录（Loopback）', 'vhcu-tab-dot']) {
    assert.ok(text.includes(needle), `hear tab is missing ${needle}`);
  }
});

test('P16.3 fps shows waiting instead of zero before first diff', () => {
  const source = readFileSync(CLIENT, 'utf8');
  assert.match(source, /-- 等待中/);
  assert.match(source, /\(state\.counters\?\.diffs \?\? 0\) === 0/);
});

test('P16.4 hear panel tells users to raise system volume', () => {
  const source = readFileSync(CLIENT, 'utf8');
  assert.match(source, /音量偏低时请调高系统音量/);
});

test('P16.7 settings exposes hear sensitivity levels', () => {
  const source = readFileSync(CLIENT, 'utf8');
  assert.match(source, /vhcu-sensitivity/);
  assert.match(source, /严格（过滤噪音优先）/);
  assert.match(source, /灵敏（保留说话优先）/);
});

test('P6 settings Save posts the complete draft for persistence', () => {
  const source = readFileSync(CLIENT, 'utf8');
  assert.match(source, /fetch\(`\$\{API\}\/settings`/);
  assert.match(source, /body: JSON\.stringify\(\{ settings: draft \}\)/);
  assert.match(source, /设置保存到 settings\.json/);
});

test('P17.1 settings fields stack vertically with spacing', () => {
  const source = readFileSync(CLIENT, 'utf8');
  assert.match(source, /\.vhcu-field \{ display:flex; flex-direction:column/);
  assert.match(source, /\.vhcu-field \+ \.vhcu-field \{ margin-top:12px/);
});

test('P18.1 emergency button is a compact header icon with hotkey', () => {
  const source = readFileSync(CLIENT, 'utf8');
  assert.match(source, /vhcu-emergency-btn/);
  assert.match(source, /紧急制动/);
  assert.match(source, /Ctrl\+Shift\+X/);
  assert.match(source, /addEventListener\('keydown'/);
  assert.match(source, /emergency-stop/);
  assert.doesNotMatch(source, /className: 'vhcu-emergency'/);
});

test('P17.4 recovering phase is visible in the panel', () => {
  const source = readFileSync(CLIENT, 'utf8');
  assert.match(source, /recovering: '恢复中'/);
  assert.match(source, /phase === 'recovering'/);
});

test('P19.2 recording controls are always visible above the tabs', () => {
  const source = readFileSync(CLIENT, 'utf8');
  assert.match(source, /vhcu-record-strip/);
  assert.match(source, /🔴 开始录制宏/);
  assert.match(source, /正在录制中，请操作屏幕/);
  assert.match(source, /停止并保存/);
  assert.match(source, /vhcu-pulse/);
  assert.match(source, /\/recording/);
  assert.ok(source.indexOf('recordStrip') < source.indexOf("className: 'vhcu-tabs'"));
});

test('P19.3 record button renders above tabs without opening any popover', () => {
  const loaded = loadClientModule();
  const module = loaded.factory((name) => {
    if (name === 'react') return reactStub();
    throw new Error(`unexpected require ${name}`);
  });
  let descriptor = null;
  module.apply({
    betterSidebar: { registerTab: (value) => ((descriptor = value), () => {}) },
    effect: (body) => body(),
  });
  const shell = descriptor.component({});
  const tree = shell.type(shell.props);
  const text = JSON.stringify(tree);
  assert.ok(text.includes('vhcu-record-strip'));
  assert.ok(text.includes('🔴 开始录制宏'));
  assert.ok(text.includes('录制键鼠为宏'));
  assert.ok(text.indexOf('vhcu-record-strip') < text.indexOf('vhcu-tabs'));
});

test('P19.4 stop opens an inline name/category form (no window.prompt)', () => {
  const source = readFileSync(CLIENT, 'utf8');
  assert.doesNotMatch(source, /window\.prompt/);
  assert.match(source, /vhcu-record-form/);
  assert.match(source, /确认保存/);
  assert.match(source, /取消/);
  assert.match(source, /macroForm\.category/);
  assert.match(source, /我的自定义宏/);
  assert.match(source, /setPendingRecording/);
  assert.ok(source.indexOf('recordForm') < source.indexOf("className: 'vhcu-tabs'"));
});

test('P20 macro delete/import/export controls are present', () => {
  const source = readFileSync(CLIENT, 'utf8');
  assert.match(source, /🗑 删除此宏/);
  assert.match(source, /再点一次确认删除/);
  assert.match(source, /📥 导入宏/);
  assert.match(source, /📤 导出/);
  assert.match(source, /vhcu-danger/);
  assert.match(source, /navigator\.clipboard/);
  assert.match(source, /onDeleteMacro\(selected\)/);
  assert.doesNotMatch(source, /window\.confirm/);
  assert.match(source, /importMacros/);
});

test('P22.2 privacy, raw Hear, reconnecting and 30-step UI are present', () => {
  const source = readFileSync(CLIENT, 'utf8');
  assert.match(source, /vhcu-privacy-banner/);
  assert.match(source, /隐私保护触发：AI 已暂停/);
  assert.ok(source.includes('关闭隐私屏蔽（开发者模式）'));
  assert.match(source, /确认开启/);
  assert.match(source, /显示原始转写 \(仅调试用\)/);
  assert.match(source, /reconnecting/);
  assert.match(source, /音频设备断开，尝试重连中/);
  assert.match(source, /最多 100 步/);
  assert.match(source, /已达到 100 步上限/);
  assert.match(source, /privacyUnprotected/);
  assert.match(source, /关闭隐私屏蔽（开发者模式）/);
  assert.match(source, /截图与键鼠按配置放行/);
  assert.match(source, /vhcu-unprotected-banner/);
});

test('P23 soft pause banner and HITL approval modal are present', () => {
  const source = readFileSync(CLIENT, 'utf8');
  assert.match(source, /您正在接管键鼠，AI 已暂停/);
  assert.match(source, /interaction\?\.notice/);
  assert.match(source, /AI 请求执行敏感操作/);
  assert.match(source, /vhcu-approval/);
  assert.match(source, /\/approval/);
  assert.match(source, /is_destructive/);
  assert.doesNotMatch(source, /window\.confirm/);
});

test('P23.1 auto-approve sensitive-operation macros has two-step confirmation and red banner', () => {
  const source = readFileSync(CLIENT, 'utf8');
  assert.ok(source.includes('自动批准敏感宏（托管模式）'));
  assert.ok(source.includes('启用后 AI 执行关闭、删除等敏感操作宏时不再逐次弹窗'));
  assert.ok(source.includes('⚠️ 自动批准敏感宏（托管模式）已开启，请保持任务在受控范围内。'));
  assert.match(source, /vhcu-autoapprove-banner/);
  assert.match(source, /autoApproveDestructive/);
  assert.match(source, /confirmAutoApprove/);
});

test('P24 Turbo mode setting and 100-step UI copy are present', () => {
  const source = readFileSync(CLIENT, 'utf8');
  assert.ok(source.includes('启用 Turbo 极速模式'));
  assert.ok(source.includes('用户软暂停后 50ms 即自动恢复并刷新截图'));
  assert.ok(source.includes('极不稳定的界面可能因缺乏回读而产生误操作'));
  assert.match(source, /turboMode/);
  assert.match(source, /humanizeProfile/);
  assert.match(source, /vhcu-humanize/);
  assert.match(source, /拟人化鼠标轨迹/);
  assert.match(source, /balanced · 拟人曲线/);
  assert.match(source, /Turbo 模式会自动降级为 precise/);
  assert.match(source, /cursorInjectionMode/);
  assert.match(source, /vhcu-cursor-mode/);
  assert.match(source, /virtual 不抢物理鼠标/);
  assert.match(source, /vhcu-cursor-warning/);
  assert.match(source, /cursorWarning/);
  assert.match(source, /最多 100 步/);
  assert.match(source, /【常规设置】/);
  assert.match(source, /【高级调试】/);
  assert.match(source, /【高级设置】/);
  assert.match(source, /建议保持默认设置/);
});

test('P26.7 plugin-list icon is configured; sidebar icons stay removed', () => {
  const source = readFileSync(CLIENT, 'utf8');
  assert.ok(!source.includes('vhcu-sidebar-icon'));
  assert.ok(!/icon:\s*\(size\)/.test(source));
  const root = join(HERE, '..', '..');
  const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
  const plugin = JSON.parse(readFileSync(join(root, 'dsh.plugin.json'), 'utf8'));
  assert.equal(pkg.icon, 'assets/icon.png');
  assert.equal(plugin.icon, 'assets/icon.png');
  const icon = join(root, 'assets', 'icon.png');
  assert.equal(existsSync(icon), true);
  assert.ok(statSync(icon).size <= 256 * 1024, 'plugin-list icon must stay under 256 KiB');
  assert.equal(existsSync(join(root, 'lib', 'assets', 'icon.png')), false);
});

test('P30 web client warns and disables desktop-only actions', () => {
  const source = readFileSync(CLIENT, 'utf8');
  assert.match(source, /const IS_DESKTOP/);
  assert.match(source, /const IS_WEB = !IS_DESKTOP/);
  assert.ok(
    source.includes(
      '❌ VHCU 仅支持 DSH 桌面版 (Desktop App)。浏览器版受系统沙盒限制，无法控制本地电脑。',
    ),
  );
  assert.match(source, /disabled: IS_WEB \|\| busy/);
  assert.match(source, /disabled: IS_WEB \|\| hearBusy/);
  assert.match(source, /disabled: IS_WEB \|\| recordingBusy/);
  assert.match(source, /macroQueue\?\.status === 'hung'/);
  const readme = readFileSync(join(HERE, '..', '..', 'README.md'), 'utf8');
  assert.match(readme, /cannot start the local Python engine or inject input/);
});

test('P7 recovery banner is top-level, explicit, and user-confirmed', () => {
  const source = readFileSync(CLIENT, 'utf8');
  assert.match(source, /vhcu-recovery-banner/);
  assert.match(source, /发现未完成的宏任务/);
  assert.match(source, /是否恢复/);
  assert.match(source, /resolveRecovery\('resume'\)/);
  assert.match(source, /resolveRecovery\('discard'\)/);
  assert.match(source, /fetch\(`\$\{API\}\/recovery`/);
  assert.match(source, /action, queueId/);
  assert.ok(
    source.indexOf('vhcu-recovery-banner') < source.indexOf("className: 'vhcu-tabs'"),
    'recovery banner must render above the tabs',
  );
});

test('V1.1 onboarding is a non-invasive three-step top guide', () => {
  const source = readFileSync(CLIENT, 'utf8');
  assert.match(source, /vhcu\.onboarding\.v1/);
  assert.match(source, /首次使用 VHCU/);
  assert.match(source, /开启视觉/);
  assert.match(source, /检查权限/);
  assert.match(source, /录第一个宏/);
  assert.match(source, /localStorage/);
  assert.match(source, /checkOnboardingPermissions/);
  assert.match(source, /resolveRecovery/);
  assert.doesNotMatch(source, /window\.confirm/);
  assert.ok(
    source.indexOf('vhcu-onboarding') < source.indexOf("className: 'vhcu-tabs'"),
    'onboarding must render above tabs',
  );
});
