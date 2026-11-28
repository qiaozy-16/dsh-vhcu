/**
 * @file payload.js — URL / Base64 dual-track assembly for vision payloads.
 *
 * Background: a signed `frame_url` only works when the host can turn that URL
 * into an image for the model. Some DSH builds cannot, which makes the model
 * blind exactly when it needs to look. The base64 track inlines the pixels as
 * a `data:` URI instead, at the cost of a much heavier tool result.
 *
 * This module is intentionally pure: mode resolution, width clamping and
 * data-URI encoding are unit-testable without a daemon, a screen or a host.
 * The caller owns the HTTP fetch (see lib/index.js `inlineFrameData`).
 */

/** Modes accepted by settings and by the per-call `payload_mode_override`. */
export const VISION_PAYLOAD_MODES = Object.freeze(['url', 'base64']);

/**
 * Hard width ceiling for `base64 + precision_mode`.
 * A 100% physical-pixel desktop screenshot is several megabytes of PNG; even
 * base64-encoded it would dominate the context, so the precision request is
 * capped rather than refused (the model still gets usable pixels).
 */
export const BASE64_MAX_WIDTH = 1280;

/** A data URI larger than this is dropped back to the URL track. */
export const BASE64_MAX_DATA_URL_BYTES = 6 * 1024 * 1024;

/** Mirrors the markdown renderer: at most 3 ROI images per diff event. */
export const BASE64_EVENT_IMAGE_LIMIT = 3;

/** Total pixel budget for inlined ROI images in one events response. */
export const BASE64_EVENT_BUDGET_BYTES = 12 * 1024 * 1024;

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47]);
const JPEG_SIGNATURE = Buffer.from([0xff, 0xd8, 0xff]);

/**
 * Coerce any stored/patched value to a known mode.
 * Unknown values fall back instead of throwing: a corrupt settings file must
 * never make the plugin unusable.
 */
export function normalizeVisionPayloadMode(value, fallback = 'url') {
  const raw = String(value ?? '').trim().toLowerCase();
  if (raw === 'url' || raw === 'base64') return raw;
  return VISION_PAYLOAD_MODES.includes(fallback) ? fallback : 'url';
}

/**
 * Resolve the mode for one tool call.
 * A valid per-call override wins so the model can recover mid-task when it
 * notices images are not arriving; an INVALID override is ignored (it must not
 * silently flip the user's setting).
 */
export function resolveVisionPayloadMode({ override, setting } = {}) {
  const overridden = String(override ?? '').trim().toLowerCase();
  if (overridden === 'url' || overridden === 'base64') return overridden;
  return normalizeVisionPayloadMode(setting);
}

/**
 * Decide the PNG width for this capture.
 *
 * @param {{mode?: string, precision?: boolean, maxWidth?: number, cap?: number}} input
 * @returns {{maxWidth: number, clamped: boolean, hint: string|null}}
 */
export function base64WidthPlan({ mode, precision, maxWidth, cap = BASE64_MAX_WIDTH } = {}) {
  const requested = Math.max(1, Math.trunc(Number(maxWidth) || 0));
  const isBase64 = normalizeVisionPayloadMode(mode) === 'base64';
  if (!isBase64 || !precision || requested <= cap) {
    return { maxWidth: requested || cap, clamped: false, hint: null };
  }
  return {
    maxWidth: cap,
    clamped: true,
    hint:
      `Base64 + 高精度：为防止 data URI 撑爆上下文，图片最大宽度已被强制限制为 ${cap}px` +
      `（本次请求 ${requested}px）。若需要 100% 物理像素，请改用 payload_mode_override:"url"` +
      ' 或关闭 precision_mode 后重试。',
  };
}

/** Sniff the real container; the daemon currently emits PNG only. */
export function imageMimeFromBuffer(buffer) {
  if (!Buffer.isBuffer(buffer) || buffer.length < 8) return null;
  if (buffer.subarray(0, 4).equals(PNG_SIGNATURE)) return 'image/png';
  if (buffer.subarray(0, 3).equals(JPEG_SIGNATURE)) return 'image/jpeg';
  return null;
}

/**
 * Encode one frame as a `data:` URI.
 * Returns null (instead of throwing) when the bytes are not a known image or
 * the encoded payload would be too large — callers then keep the URL track.
 */
export function toImageDataUrl(buffer, { maxBytes = BASE64_MAX_DATA_URL_BYTES } = {}) {
  const mime = imageMimeFromBuffer(buffer);
  if (!mime) return null;
  const bytes = buffer.length;
  if (bytes > maxBytes) return null;
  return {
    dataUrl: `data:${mime};base64,${buffer.toString('base64')}`,
    mime,
    bytes,
    estimatedLength: Math.ceil(bytes / 3) * 4,
  };
}

/** Image source the renderer should embed: inline pixels first, URL second. */
export function pickImageSource(value) {
  const inline = typeof value?.frame_data_url === 'string' ? value.frame_data_url : '';
  if (inline) return inline;
  const url = typeof value?.frame_url === 'string' ? value.frame_url : '';
  return url || null;
}

/** Same choice for one ROI inside a diff event. */
export function pickRoiImageSource(roi) {
  const inline = typeof roi?.frame_data_url === 'string' ? roi.frame_data_url : '';
  if (inline) return inline;
  const url = typeof roi?.frame_url === 'string' ? roi.frame_url : '';
  return url || null;
}

/**
 * Human-readable summary for the tool result, so the model knows which track
 * delivered the pixels and whether anything was downgraded.
 */
export function payloadNotice({ mode, bytes = 0, downgraded = false } = {}) {
  const resolved = normalizeVisionPayloadMode(mode);
  if (resolved !== 'base64') return null;
  if (downgraded) {
    return 'Base64 传输失败或图片过大，本次已回退为签名 URL（若宿主读不到图片，请检查视觉守护进程）。';
  }
  return `Base64 内联传输：${bytes} 字节的图片已作为 data URI 返回（不依赖宿主能否访问 frame_url）。`;
}
