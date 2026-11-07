/**
 * P19.2 macro recorder: convert normalized global input events into macro steps.
 * Pure JS (no native dependency) so it can be tested independently of uiohook.
 */

const DEFAULT_WAIT_GAP_MS = 500;
const DEFAULT_MAX_STEPS = 100;
const MAX_TEXT_CHARS = 2000;

const SPECIAL_KEYS = {
  Enter: 'enter',
  NumpadEnter: 'enter',
  Tab: 'tab',
  Escape: 'esc',
  Space: 'space',
  Backspace: 'backspace',
  Delete: 'delete',
  ArrowLeft: 'left',
  ArrowRight: 'right',
  ArrowUp: 'up',
  ArrowDown: 'down',
  Home: 'home',
  End: 'end',
  PageUp: 'pageup',
  PageDown: 'pagedown',
};

const MODIFIER_KEYS = new Set([
  'Ctrl',
  'CtrlRight',
  'Shift',
  'ShiftRight',
  'Alt',
  'AltRight',
  'Meta',
  'MetaRight',
  'CapsLock',
  'NumLock',
  'ScrollLock',
]);

const PUNCTUATION = {
  Semicolon: [';', ':'],
  Equal: ['=', '+'],
  Comma: [',', '<'],
  Minus: ['-', '_'],
  Period: ['.', '>'],
  Slash: ['/', '?'],
  Backquote: ['`', '~'],
  BracketLeft: ['[', '{'],
  Backslash: ['\\', '|'],
  BracketRight: [']', '}'],
  Quote: ["'", '"'],
};

const NUMPAD_CHARS = {
  Numpad0: '0',
  Numpad1: '1',
  Numpad2: '2',
  Numpad3: '3',
  Numpad4: '4',
  Numpad5: '5',
  Numpad6: '6',
  Numpad7: '7',
  Numpad8: '8',
  Numpad9: '9',
  NumpadAdd: '+',
  NumpadSubtract: '-',
  NumpadMultiply: '*',
  NumpadDivide: '/',
  NumpadDecimal: '.',
};

function charFor(key, shift) {
  if (/^[A-Za-z]$/.test(key)) return shift ? key.toUpperCase() : key.toLowerCase();
  if (/^[0-9]$/.test(key)) {
    if (!shift) return key;
    return ')!@#$%^&*('.charAt(Number(key));
  }
  if (key === 'Space') return ' ';
  if (NUMPAD_CHARS[key]) return NUMPAD_CHARS[key];
  if (PUNCTUATION[key]) return PUNCTUATION[key][shift ? 1 : 0];
  return '';
}

function stepKeyName(key) {
  if (/^[A-Za-z]$/.test(key)) return key.toLowerCase();
  if (/^[0-9]$/.test(key)) return key;
  if (/^F([1-9]|1[0-2])$/.test(key)) return key.toLowerCase();
  return SPECIAL_KEYS[key] ?? null;
}

function modsFor(event, { includeShift = false } = {}) {
  const keys = [];
  if (event.ctrlKey) keys.push('ctrl');
  if (event.altKey) keys.push('alt');
  if (event.metaKey) keys.push('win');
  if (includeShift && event.shiftKey) keys.push('shift');
  return keys;
}

export class MacroRecorder {
  constructor({ now = Date.now, waitGapMs = DEFAULT_WAIT_GAP_MS, maxSteps = DEFAULT_MAX_STEPS } = {}) {
    this.now = now;
    this.waitGapMs = waitGapMs;
    this.maxSteps = maxSteps;
    this.active = false;
    this.steps = [];
    this.pendingText = '';
    this.lastAt = 0;
    this.startedAt = 0;
    this.stoppedAt = 0;
    this.eventCount = 0;
    this.ignored = 0;
    this.truncated = false;
  }

  start() {
    this.active = true;
    this.steps = [];
    this.pendingText = '';
    this.startedAt = this.now();
    this.lastAt = this.startedAt;
    this.stoppedAt = 0;
    this.eventCount = 0;
    this.ignored = 0;
    this.truncated = false;
    return this;
  }

  stop() {
    this._flushText();
    this.stoppedAt = this.now();
    this.active = false;
    return this.summary();
  }

  summary() {
    this._flushText();
    return {
      steps: this.steps.map((step) => ({ ...step })),
      step_count: this.steps.length,
      event_count: this.eventCount,
      ignored: this.ignored,
      truncated: this.truncated,
      duration_ms: Math.max(0, (this.stoppedAt || (this.active ? this.now() : this.lastAt)) - this.startedAt),
    };
  }

  handle(event = {}) {
    if (!this.active) return false;
    const time = Number.isFinite(Number(event.time)) ? Number(event.time) : this.now();
    this.eventCount += 1;
    if (event.type === 'keydown') return this._onKey(event, time);
    if (event.type === 'mousedown') return this._onMouseDown(event, time);
    if (event.type === 'wheel') return this._onWheel(event, time);
    this.ignored += 1;
    return false;
  }

  _onKey(event, time) {
    const key = String(event.key ?? '');
    if (!key || MODIFIER_KEYS.has(key)) {
      this.ignored += 1;
      return false;
    }
    const shortcut = Boolean(event.ctrlKey || event.altKey || event.metaKey);
    const char = shortcut ? '' : charFor(key, Boolean(event.shiftKey));
    if (char) {
      this._recordChar(char, time);
      return true;
    }
    const name = stepKeyName(key);
    if (!name) {
      this.ignored += 1;
      return false;
    }
    const keys = [...modsFor(event, { includeShift: true }), name];
    this._prepareNonText(time);
    return this._pushStep({ op: 'key', keys });
  }

  _onMouseDown(event, time) {
    const button = Number(event.button);
    const x = Math.round(Number(event.x));
    const y = Math.round(Number(event.y));
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      this.ignored += 1;
      return false;
    }
    if (button !== 1 && button !== 2) {
      this.ignored += 1;
      return false;
    }
    this._prepareNonText(time);
    return this._pushStep(button === 2 ? { op: 'right_click', x, y } : { op: 'click', x, y });
  }

  _onWheel(event, time) {
    const direction = Number(event.direction);
    if (direction === 4) {
      this.ignored += 1;
      return false;
    }
    const magnitude = Math.max(1, Math.abs(Number(event.amount) || 1));
    const amount = Number(event.rotation) < 0 ? -magnitude : magnitude;
    this._prepareNonText(time);
    return this._pushStep({ op: 'scroll', amount });
  }

  _recordChar(char, time) {
    const gap = time - this.lastAt;
    if (this.pendingText) {
      if (gap > this.waitGapMs) {
        this._flushText();
        this._pushWait(gap);
      }
    } else if (this.steps.length > 0 && gap > this.waitGapMs) {
      this._pushWait(gap);
    }
    this.pendingText += char;
    if (this.pendingText.length >= MAX_TEXT_CHARS) this._flushText();
    this.lastAt = time;
  }

  _prepareNonText(time) {
    const gap = time - this.lastAt;
    this._flushText();
    if (this.steps.length > 0 && gap > this.waitGapMs) this._pushWait(gap);
    this.lastAt = time;
  }

  _flushText() {
    if (!this.pendingText) return;
    const text = this.pendingText;
    this.pendingText = '';
    this._pushStep({ op: 'type', text });
  }

  _pushWait(gapMs) {
    if (gapMs <= this.waitGapMs) return;
    this._pushStep({ op: 'wait', ms: Math.min(30000, Math.round(gapMs)) });
  }

  _pushStep(step) {
    if (this.steps.length >= this.maxSteps) {
      this.truncated = true;
      return false;
    }
    this.steps.push(step);
    return true;
  }
}
