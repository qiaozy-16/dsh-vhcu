/**
 * @file payload.js — image transport mode for vision tool results.
 *
 * Two tracks:
 *   - `url`        — embed the short-lived signed URL in Markdown (cheap).
 *   - `attachment` — store the PNG in the host attachment service and return an
 *                    image content block, so the model receives actual pixels
 *                    instead of text it cannot see.
 *
 * The previous `base64` track (inline `data:` URI inside the tool text) was
 * retired: some hosts treat the whole data URI as text, truncate it, and the
 * model still sees nothing while paying the token cost.
 *
 * Pure functions only; the attachment store lives in lib/index.js.
 */

/** Modes accepted by settings and by per-call `payload_mode_override`. */
export const VISION_PAYLOAD_MODES = Object.freeze(['url', 'attachment']);

/**
 * Coerce any stored/patched value to a known mode.
 *
 * Legacy migration: `base64` (retired text track) maps to `attachment`, which
 * is what the user was trying to achieve when they selected it.
 * Unknown values fall back instead of throwing: a corrupt settings file must
 * never make the plugin unusable.
 */
export function normalizeVisionPayloadMode(value, fallback = 'url') {
  const raw = String(value ?? '').trim().toLowerCase();
  if (raw === 'attachment' || raw === 'base64') return 'attachment';
  if (raw === 'url') return 'url';
  const resolvedFallback = String(fallback ?? '').trim().toLowerCase();
  if (resolvedFallback === 'attachment' || resolvedFallback === 'base64') return 'attachment';
  return 'url';
}

/**
 * Resolve the mode for one tool call.
 * A valid per-call override wins so the model can recover mid-task; an INVALID
 * override is ignored (it must not silently flip the user's setting).
 */
export function resolveVisionPayloadMode({ override, setting } = {}) {
  const overridden = String(override ?? '').trim().toLowerCase();
  if (overridden === 'url' || overridden === 'attachment' || overridden === 'base64') {
    return normalizeVisionPayloadMode(overridden);
  }
  return normalizeVisionPayloadMode(setting);
}

/** Whether this mode needs the host attachment service. */
export function needsAttachmentStore(mode) {
  return normalizeVisionPayloadMode(mode) === 'attachment';
}
