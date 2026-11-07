/**
 * @file fusion.js — P15 multimodal fusion.
 *
 * Groups vision ROI changes and hear transcripts that happen within one
 * 1.5-second window. Pure/buffer logic only: timers and event emission belong
 * to index.js, so this is deterministic in tests.
 */

export const MULTIMODAL_WINDOW_MS = 1500;

export class FusionBuffer {
  constructor({ windowMs = MULTIMODAL_WINDOW_MS, onFlush = () => {}, now = () => Date.now() } = {}) {
    this.windowMs = windowMs;
    this.onFlush = onFlush;
    this.now = now;
    this.window = null;
  }

  get pendingStart() {
    return this.window ? this.window.start : null;
  }

  _ensure(ts) {
    if (!this.window) {
      this.window = {
        start: ts,
        end: ts,
        visionCount: 0,
        rois: [],
        visionSummary: '',
        hearTexts: [],
        audioRms: [],
        audioMax: [],
        audioMin: [],
        audioSpikes: [],
      };
    }
    this.window.end = Math.max(this.window.end, ts);
  }

  pushVision({ ts = this.now(), rois = [], summary = '' } = {}) {
    const expired = this.window && ts - this.window.start >= this.windowMs;
    if (expired) this.flush(this.window.end, 'window');
    this._ensure(ts);
    this.window.visionCount += 1;
    if (summary) this.window.visionSummary = summary;
    if (Array.isArray(rois)) {
      this.window.rois.push(...rois);
      if (this.window.rois.length > 12) this.window.rois = this.window.rois.slice(-12);
    }
    return this.window;
  }

  pushHear({ ts = this.now(), text = '' } = {}) {
    const expired = this.window && ts - this.window.start >= this.windowMs;
    if (expired) this.flush(this.window.end, 'window');
    this._ensure(ts);
    const value = String(text ?? '').trim();
    if (value) this.window.hearTexts.push(value);
    return this.window;
  }

  /** P16.2: ambient audio (hear.audio) is also a Hear-dimension event. */
  pushAudio({ ts = this.now(), rms = 0, rmsMax = null, rmsMin = null, hasSpike = false } = {}) {
    const expired = this.window && ts - this.window.start >= this.windowMs;
    if (expired) this.flush(this.window.end, 'window');
    this._ensure(ts);
    const value = Number(rms);
    if (Number.isFinite(value)) {
      this.window.audioRms.push(value);
      this.window.audioMax.push(Number.isFinite(Number(rmsMax)) ? Number(rmsMax) : value);
      this.window.audioMin.push(Number.isFinite(Number(rmsMin)) ? Number(rmsMin) : value);
      this.window.audioSpikes.push(Boolean(hasSpike));
    }
    return this.window;
  }

  /** Flush if the current window has reached its deadline. */
  flushIfExpired(ts = this.now()) {
    if (!this.window) return null;
    if (ts - this.window.start < this.windowMs) return null;
    return this.flush(this.window.end, 'timer');
  }

  flush(at = null, reason = 'timer') {
    if (!this.window) return null;
    const win = this.window;
    this.window = null;
    const end = at ?? win.end;
    const audioCount = win.audioRms.length;
    const audioRms = audioCount
      ? win.audioRms.reduce((sum, value) => sum + value, 0) / audioCount
      : null;
    const audioRmsMax = audioCount ? Math.max(...win.audioMax) : null;
    const audioRmsMin = audioCount ? Math.min(...win.audioMin) : null;
    const hasSpike = audioCount ? win.audioSpikes.some(Boolean) : false;
    const audioText = audioCount
      ? `[RMS:${audioRms.toFixed(2)} | 峰值:${audioRmsMax.toFixed(2)}${hasSpike ? ' 💥 巨响/音效' : ''}]`
      : '';
    const hearText = [...win.hearTexts, audioText].filter(Boolean).join('\n');
    const hasHear = Boolean(hearText);
    const vision = win.visionCount
      ? `画面变化 ×${win.visionCount} · ${win.rois.length} 个区域`
      : '';
    const modalities = win.visionCount > 0 ? (hasHear ? 'both' : 'vision') : hasHear ? 'hear' : 'unknown';
    const event = {
      ts: win.start,
      time: new Date(win.start).toTimeString().slice(0, 8),
      window_start: win.start,
      window_end: end,
      window_ms: Math.max(0, end - win.start),
      reason,
      modalities,
      vision: vision || null,
      vision_rois: win.visionCount ? win.rois : [],
      vision_count: win.visionCount,
      hear: hearText || null,
      hear_text: hearText || null,
      hear_count: win.hearTexts.length,
      audio_count: audioCount,
      audio_rms: audioRms === null ? null : Number(audioRms.toFixed(4)),
      audio_rms_max: audioRmsMax === null ? null : Number(audioRmsMax.toFixed(4)),
      audio_rms_min: audioRmsMin === null ? null : Number(audioRmsMin.toFixed(4)),
      has_spike: hasSpike,
      text:
        [vision ? `👁 ${vision}` : '', hearText ? `👂 ${hearText.replace(/\n/g, ' / ')}` : '']
          .filter(Boolean)
          .join('  |  ') || '多模态事件',
    };
    this.onFlush(event);
    return event;
  }

  reset() {
    this.window = null;
  }
}
