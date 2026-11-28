/**
 * @file contract.js — VHCU protocol v1 single source of truth (Node side).
 *
 * Rules:
 *  - One JSON object per line (NDJSON), UTF-8, LF only, max 64 KiB per line.
 *  - seq is monotonic per sender, starting at 1. Receivers report gaps but
 *    still accept the frame (vision is latest-wins; state events carry seq).
 *  - Every frame is validated against contract/schema/v1/*.json.
 *  - Images NEVER travel on this plane. Only JSON facts; the image plane is a
 *    separate loopback HTTP pull (see READY.port/token).
 */

import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { validate } from './validate.js';

export const PROTOCOL_VERSION = 1;
export const MAX_LINE_BYTES = 65536;
export const MAX_CMD_QUEUE = 256;
export const MAX_ROIS = 8;
export const HEARTBEAT_MS = 1000;
export const HEARTBEAT_MISS_LIMIT = 10;
export const READY_TIMEOUT_MS = 15000;
export const DEFAULT_CMD_TIMEOUT_MS = 3000;
export const SHUTDOWN_GRACE_MS = 3000;
export const TERM_GRACE_MS = 2000;
export const RESTART_BACKOFF_MIN_MS = 500;
export const RESTART_BACKOFF_MAX_MS = 30000;
export const BREAKER_MAX_FAILURES = 3;
export const BREAKER_WINDOW_MS = 60000;

/** Command whitelist. Anything else is rejected with E_DENIED, never guessed. */
export const OPS = Object.freeze([
  'ping',
  'sys.info',
  'sys.active_window',
  'vision.start',
  'vision.stop',
  'vision.status',
  'vision.configure',
  'vision.snapshot',
  'vision.capture',
  'uia.find',
  'uia.click',
  'uia.click_point',
  'input.click',
  'input.type',
  'input.hotkey',
  'input.scroll',
  'input.drag',
  'input.draw_shape',
  'input.middle_drag',
  'input.macro.open_notepad',
  'input.macro.save',
  'input.macro.run',
  'hear.start',
  'hear.stop',
  'hear.status',
  'hear.configure',
  'shutdown',
]);

/** Vision sources the daemon may advertise. */
export const SOURCES = Object.freeze(['fake', 'mss', 'window']);

/**
 * Error taxonomy. `retryable` drives supervisor policy; `hint` is what the UI
 * shows verbatim, so it must be actionable and never contain a stack trace.
 *
 * Scope note: this enum is the set of codes that may appear in a protocol
 * `error`/`ack` frame, and it must mirror `vision/vhcu_daemon/protocol.py`
 * ERROR_CODES (cross-checked by vision/tests/test_error_codes.py).
 * `E_CONFIG` is deliberately NOT here: relative VHCU_* paths are rejected
 * locally by host/lib/paths.js and by the daemon's PathConfigError before
 * READY, so that code never reaches the wire.
 */
export const ERROR_CODES = Object.freeze({
  E_PROTO: { retryable: false, hint: '协议帧不合法：检查字段/版本，必要时重启 daemon。' },
  E_TIMEOUT: { retryable: true, hint: '命令超时：daemon 可能卡住，稍后重试或重启。' },
  E_BUSY: { retryable: true, hint: 'daemon 忙：等待当前任务结束后重试。' },
  E_DENIED: { retryable: false, hint: '命令不在白名单或被权限拒绝。' },
  E_UNAVAILABLE: { retryable: true, hint: '依赖/数据源不可用：检查 Python venv 与 mss 安装。' },
  E_STATE: { retryable: false, hint: '当前状态不允许该操作：先 stop 再 start。' },
  E_CAPTURE: { retryable: true, hint: '抓屏失败：可能是独占全屏、锁屏或 DRM 内容。' },
  E_CAPTURE_MINIMIZED: {
    retryable: false,
    hint: '目标窗口已最小化，没有可捕获的渲染帧；请先恢复窗口再截图。',
  },
  E_CAPTURE_BLACK: {
    retryable: false,
    hint: '目标窗口所有 GDI 后端都返回黑图/单色图；可能是 UWP/WinUI3 或 DirectComposition 窗口。',
  },
  E_CAPTURE_OCCLUDED: {
    retryable: true,
    hint: '目标窗口被其他窗口遮挡，当前只能得到屏幕区域；请置前目标窗口后重试。',
  },
  E_CAPTURE_TARGET_GONE: {
    retryable: false,
    hint: '目标窗口已关闭、销毁或没有可用矩形；请重新获取前台窗口。',
  },
  E_CAPTURE_TARGET_NOT_FOUND: {
    retryable: false,
    hint: '找不到目标窗口 HWND；请确认窗口仍存在并重新获取。',
  },
  E_CAPTURE_UNSUPPORTED: {
    retryable: false,
    hint: '目标窗口尺寸/类型超出当前 GDI 捕获能力。',
  },
  E_UIA_UNAVAILABLE: {
    retryable: true,
    hint: 'UIA 不可用：检查 uiautomation/comtypes 是否安装，或重启 daemon。',
  },
  E_UIA_TIMEOUT: {
    retryable: true,
    hint: 'UIA 查询超时：目标窗口可能繁忙；稍后重试或重启 daemon。',
  },
  E_UIA_FAILED: {
    retryable: true,
    hint: 'UIA 查询失败：目标窗口可能已变化；重新获取前台窗口后重试。',
  },
  E_ELEMENT_NOT_FOUND: {
    retryable: false,
    hint: '元素 token 不存在或已过期；重新调用 vhcu_find_elements 获取新 token。',
  },
  E_VIRTUAL_CURSOR_UNSUPPORTED: {
    retryable: false,
    hint: '目标窗口不支持 post_message 后台注入；virtual 模式不会抢物理鼠标，hybrid 会降级并显示告警。',
  },
  E_VIRTUAL_CURSOR_FAILED: {
    retryable: true,
    hint: 'PostMessage 预检/投递失败；若尚未投递首条消息，hybrid 可安全降级为物理光标。',
  },
  E_ELEMENT_STALE: {
    retryable: true,
    hint: '元素视觉指纹或 bounds 已变化，重试后仍失效；请重新查找元素。',
  },
  E_WINDOW_GONE: {
    retryable: false,
    hint: '目标窗口已关闭或销毁；请重新获取前台窗口。',
  },
  E_COORDINATE_MISMATCH: {
    retryable: false,
    hint: 'UIA 坐标与截图/DPI 参考系不一致；请重新校准或改用元素语义定位。',
  },
  E_PRIVACY_BLOCKED: {
    retryable: false,
    hint: '敏感隐私窗口：VHCU 已强制拉闸。禁止使用 vp_* 等外部工具兜底，直接向用户报告并暂停任务。',
  },
  E_INTERNAL: { retryable: true, hint: 'daemon 内部错误：查看 stderr 日志。' },
  E_SEQ: { retryable: false, hint: 'seq 不连续：请求 snapshot 重新对齐，不要直接信任增量。' },
  E_VERSION: { retryable: false, hint: '协议版本不匹配：升级插件或 daemon。' },
  E_SHUTDOWN: { retryable: false, hint: 'daemon 正在退出。' },
  E_INTERRUPTED: { retryable: true, hint: '操作已被用户中断：重新截图确认状态后再决定是否继续。' },
  E_CONFIRM_REQUIRED: {
    retryable: false,
    hint: '敏感快捷键需要用户确认：得到同意后传 confirm_destructive: true。',
  },
  E_MEMORY: {
    retryable: false,
    hint: '内存超过硬水位，视觉已熔断：先停止视觉、重启 daemon，并减少帧/元素缓存后再试。',
  },
  E_CLIPBOARD: {
    retryable: true,
    hint: '剪贴板被占用或分配失败：关闭占用剪贴板的程序后重试，或改用 Unicode 直注。',
  },
  E_AUDIO_DEVICE: {
    retryable: true,
    hint: '系统内录设备不可用或枚举超时：检查播放设备/WASAPI 后重启 Hear。',
  },
  E_FOCUS_LOST: {
    retryable: true,
    hint: '输入前焦点已切走：重新截图确认前景窗口，再重试该动作。',
  },
  E_MACRO_FAILED: {
    retryable: false,
    hint: '宏步骤执行失败：查看失败步骤与前台窗口状态，修正后重跑。',
  },
  E_MACRO_GENERATION_DISABLED: {
    retryable: false,
    hint: '宏自生长已关闭：请用户先在设置中开启「允许模型自动生成宏」，再保存宏。',
  },
  E_MACRO_NOT_REUSABLE: {
    retryable: false,
    hint: '宏不可复用：超过 70% 的步骤是绝对坐标点击（逐步骤判断，少量变量不能豁免）；请把裸点击参数化，或改用 UIA 语义定位。',
  },
  E_SCRIPT_WRITE_DENIED: {
    retryable: false,
    hint: '禁止在工作区生成 .ps1/.cmd/.bat/.py 临时脚本；请直接使用 VHCU 视觉工具观测图像。',
  },
});

const HERE = dirname(fileURLToPath(import.meta.url));
const SCHEMA_DIR = join(HERE, '..', 'schema', 'v1');

/** type -> schema $id */
const SCHEMA_BY_TYPE = Object.freeze({
  ready: 'vhcu:ready:v1',
  cmd: 'vhcu:cmd:v1',
  ack: 'vhcu:ack:v1',
  event: 'vhcu:event:v1',
  heartbeat: 'vhcu:heartbeat:v1',
  error: 'vhcu:error:v1',
  snapshot: 'vhcu:snapshot:v1',
  shutdown: 'vhcu:shutdown:v1',
});

let registryCache = null;

/** Load every schema under contract/schema/v1 into a $id -> schema map. */
export function loadSchemas() {
  if (registryCache) return registryCache;
  const registry = new Map();
  for (const file of readdirSync(SCHEMA_DIR).filter((f) => f.endsWith('.json')).sort()) {
    const schema = JSON.parse(readFileSync(join(SCHEMA_DIR, file), 'utf8'));
    if (!schema.$id) throw new Error(`schema ${file} is missing $id`);
    if (registry.has(schema.$id)) throw new Error(`duplicate schema $id ${schema.$id}`);
    registry.set(schema.$id, schema);
  }
  registryCache = registry;
  return registry;
}

export function schemaForType(type) {
  const id = SCHEMA_BY_TYPE[type];
  return id ? loadSchemas().get(id) : null;
}

/** Validate a frame against its envelope + type schema. */
export function validateFrame(frame, schemas = loadSchemas()) {
  if (!frame || typeof frame !== 'object' || Array.isArray(frame)) {
    return { ok: false, errors: ['$: frame must be an object'] };
  }
  const envelope = schemas.get('vhcu:envelope:v1');
  const errors = validate(envelope, frame, schemas).errors;
  const typeSchema = schemaForType(frame.type);
  if (!typeSchema) errors.push(`$.type: unknown type ${JSON.stringify(frame.type)}`);
  else errors.push(...validate(typeSchema, frame, schemas).errors);
  return { ok: errors.length === 0, errors };
}

export function makeEnvelope(type, seq, fields = {}, now = Date.now()) {
  return { v: PROTOCOL_VERSION, seq, type, ts: now, ...fields };
}

export function cmdFrame({ seq, id, op, args = {}, deadlineMs, now }) {
  if (!OPS.includes(op)) throw new Error(`op ${op} is not in the whitelist`);
  return makeEnvelope('cmd', seq, { id, op, args, ...(deadlineMs ? { deadlineMs } : {}) }, now);
}

export function ackFrame({ seq, cmdId, ok, result, error, now }) {
  return makeEnvelope('ack', seq, { cmdId, ok, ...(result ? { result } : {}), ...(error ? { error } : {}) }, now);
}

export function eventFrame({ seq, kind, fields = {}, now }) {
  return makeEnvelope('event', seq, { kind, ...fields }, now);
}

export function errorFrame({ seq, code, message, fatal = false, hint, now }) {
  if (!ERROR_CODES[code]) throw new Error(`unknown error code ${code}`);
  return makeEnvelope('error', seq, { code, message, fatal, ...(hint ? { hint } : {}) }, now);
}

export function shutdownFrame({ seq, reason = 'host-requested', now }) {
  return makeEnvelope('shutdown', seq, { reason }, now);
}

/**
 * Serialize one frame to a wire line. Throws instead of writing an oversized
 * frame: a truncated JSON line would desynchronize the reader forever.
 */
export function encodeFrame(frame) {
  const line = `${JSON.stringify(frame)}\n`;
  const bytes = Buffer.byteLength(line, 'utf8');
  if (bytes > MAX_LINE_BYTES) {
    const error = new Error(`frame is ${bytes}B > ${MAX_LINE_BYTES}B limit`);
    error.code = 'E_PROTO';
    throw error;
  }
  return line;
}

/** Parse + validate one wire line. Never throws. */
export function parseFrame(line) {
  const text = typeof line === 'string' ? line.replace(/\r$/, '') : '';
  if (!text) return { ok: false, errors: ['$: empty line'], code: 'E_PROTO' };
  if (Buffer.byteLength(text, 'utf8') > MAX_LINE_BYTES) {
    return { ok: false, errors: [`$: line exceeds ${MAX_LINE_BYTES}B`], code: 'E_PROTO' };
  }
  let frame;
  try {
    frame = JSON.parse(text);
  } catch (error) {
    return { ok: false, errors: [`$: invalid JSON (${error.message})`], code: 'E_PROTO' };
  }
  const result = validateFrame(frame);
  if (!result.ok) return { ok: false, errors: result.errors, code: 'E_PROTO', frame };
  if (frame.v !== PROTOCOL_VERSION) {
    return { ok: false, errors: [`$.v: unsupported protocol ${frame.v}`], code: 'E_VERSION', frame };
  }
  return { ok: true, frame, errors: [] };
}

/**
 * Monotonic sequence tracker. A gap is reported (E_SEQ) but the frame is still
 * accepted; a duplicate/rewind is reported and the old watermark kept.
 */
export class SeqTracker {
  constructor(name = 'peer') {
    this.name = name;
    this.last = 0;
    this.gaps = 0;
    this.duplicates = 0;
  }

  accept(seq) {
    if (!Number.isInteger(seq) || seq < 1) {
      return { ok: false, reason: 'invalid', expected: this.last + 1 };
    }
    if (seq === this.last + 1) {
      this.last = seq;
      return { ok: true, reason: 'next', expected: seq };
    }
    if (seq <= this.last) {
      this.duplicates += 1;
      return { ok: false, reason: 'duplicate', expected: this.last + 1 };
    }
    const gap = { expected: this.last + 1, got: seq, missing: seq - this.last - 1 };
    this.last = seq;
    this.gaps += 1;
    return { ok: false, reason: 'gap', gap, expected: seq };
  }

  stats() {
    return { name: this.name, last: this.last, gaps: this.gaps, duplicates: this.duplicates };
  }
}

export function errorMeta(code) {
  return ERROR_CODES[code] ?? { retryable: false, hint: '未知错误码。' };
}
