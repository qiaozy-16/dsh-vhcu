#!/usr/bin/env node
/**
 * P19.1 uiohook-napi 独立 POC。
 *
 * 只验证原生模块能否加载、全局钩子能否启动、键盘/鼠标/滚轮事件能否打印；
 * 不接入 VHCU 插件主逻辑，也不生成或执行任何宏。
 *
 * 运行：
 *   cd dsh-vhcu
 *   node vision/test_uiohook.js
 * 退出：Ctrl+C
 */

import process from 'node:process';
import { uIOhook, UiohookKey } from 'uiohook-napi';

const keyNames = new Map();
for (const [name, code] of Object.entries(UiohookKey)) {
  if (!keyNames.has(code)) keyNames.set(code, name);
}

function keyName(code) {
  return keyNames.get(code) ?? `未知(${code})`;
}

function modifiers(event) {
  return [
    event.ctrlKey ? 'Ctrl' : '',
    event.altKey ? 'Alt' : '',
    event.shiftKey ? 'Shift' : '',
    event.metaKey ? 'Meta' : '',
  ]
    .filter(Boolean)
    .join('+') || '-';
}

function stamp() {
  return new Date().toISOString().slice(11, 23);
}

function safeLog(line) {
  try {
    console.log(line);
  } catch {
    // A closed terminal must not crash the hook callback.
  }
}

uIOhook.on('keydown', (event) => {
  safeLog(
    `[事件] 键盘按下 键=${keyName(event.keycode)} 键码=${event.keycode} 修饰=${modifiers(event)} 时间=${stamp()}`,
  );
});

uIOhook.on('mousedown', (event) => {
  safeLog(
    `[事件] 鼠标按下 按钮=${event.button ?? '?'} 坐标=(${event.x},${event.y}) 点击次数=${event.clicks ?? 1} 修饰=${modifiers(event)} 时间=${stamp()}`,
  );
});

uIOhook.on('wheel', (event) => {
  safeLog(
    `[事件] 滚轮 方向=${event.direction ?? '?'} 数量=${event.amount ?? '?'} 旋转=${event.rotation ?? '?'} 坐标=(${event.x},${event.y}) 时间=${stamp()}`,
  );
});

let stopping = false;
function shutdown(signal) {
  if (stopping) return;
  stopping = true;
  try {
    uIOhook.stop();
  } catch (error) {
    console.error(`[警告] 停止钩子失败：${error?.message ?? error}`);
  }
  console.log(`\n[退出] 收到 ${signal}，钩子已停止。`);
  process.exit(0);
}

process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));

try {
  uIOhook.start();
  console.log('==============================================');
  console.log('[VHCU] uiohook-napi POC 已启动');
  console.log('[VHCU] 请按键、点击鼠标或滚动滚轮，终端会打印事件。');
  console.log('[VHCU] 按 Ctrl+C 退出。');
  console.log('==============================================');
} catch (error) {
  console.error(`[错误] 无法启动 uiohook-napi：${error?.message ?? error}`);
  console.error('[提示] 若被 Windows Defender 拦截，请在“保护历史记录”中查看并放行。');
  process.exit(1);
}
