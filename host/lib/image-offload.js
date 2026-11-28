/**
 * @file image-offload.js — best-effort probe for the host's `image/offload` facility.
 *
 * DSH 0.2.2 knows `image/offload` (`{targets: ImageOffloadTarget[]}`) and marks
 * image blocks `offloaded: true`, which is the only supported way to keep a
 * message while dropping its image payload from the model request. If a plugin
 * could emit that event, "offload then reclaim" would be a lossless cleanup.
 *
 * We do NOT know a public plugin-facing API for it, and guessing at inbound
 * session mutation would be reckless. So this module only *probes*: it looks for
 * an exposed service that offers an offload method, and reports what it found.
 * Nothing is mutated; when the probe fails, cleanup stays on the orphan-only
 * path and says so.
 */

const SERVICE_CANDIDATES = Object.freeze(['sessions', 'session', 'sessionStore', 'agents']);
const METHOD_CANDIDATES = Object.freeze([
  'offloadImages',
  'offloadSessionImages',
  'appendImageOffload',
  'offloadMessageImages',
]);

/**
 * @param {{get?: (name: string) => unknown}} ctx
 * @returns {{supported: boolean, service?: string, method?: string, reason?: string}}
 */
export function probeImageOffload(ctx) {
  if (!ctx || typeof ctx.get !== 'function') {
    return { supported: false, reason: 'host context exposes no service accessor' };
  }
  const seen = [];
  for (const name of SERVICE_CANDIDATES) {
    let service;
    try {
      service = ctx.get(name);
    } catch {
      // Cordis accessors throw for absent services; an absent service is not an error.
      continue;
    }
    if (!service || typeof service !== 'object') continue;
    seen.push(name);
    for (const method of METHOD_CANDIDATES) {
      if (typeof service[method] === 'function') {
        return { supported: true, service: name, method };
      }
    }
  }
  return {
    supported: false,
    reason:
      seen.length > 0
        ? `exposed session services (${seen.join(', ')}) have no image-offload method`
        : 'no session service is exposed to host plugins',
  };
}

/**
 * Attempt to offload selected images, and give up immediately when the host does
 * not permit it. Never throws: the caller keeps the orphan-only cleanup path.
 *
 * @param {unknown} ctx
 * @param {Array<{seq: number, imageIndexes: number[]}>} targets
 * @returns {Promise<{ok: boolean, offloaded?: number, reason?: string}>}
 */
export async function tryOffloadImages(ctx, targets) {
  if (!Array.isArray(targets) || targets.length === 0) {
    return { ok: false, reason: 'no-offload-targets' };
  }
  const probe = probeImageOffload(ctx);
  if (!probe.supported) return { ok: false, reason: probe.reason };
  try {
    const service = ctx.get(probe.service);
    await service[probe.method](targets);
    return { ok: true, offloaded: targets.length };
  } catch (error) {
    return { ok: false, reason: error?.message ?? String(error) };
  }
}
