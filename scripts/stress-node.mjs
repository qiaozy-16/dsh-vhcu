#!/usr/bin/env node
/**
 * 1000-iteration Node buffer/action memory probe.
 *
 * Run with `node --expose-gc scripts/stress-node.mjs --iterations 1000`.
 * It allocates and drops screenshot-sized buffers plus action-sized event
 * objects, and reports process.memoryUsage() before/after/peak. It does not
 * run Python or capture the real screen; the Python stress script covers the
 * GDI/mss path.
 */
import { Buffer } from 'node:buffer';

const args = process.argv.slice(2);
const at = args.indexOf('--iterations');
const iterations = Math.max(1, Number(at >= 0 ? args[at + 1] : 1000) || 1000);
const frameBytes = 1920 * 1080 * 4;
const rss = () => process.memoryUsage().rss / 1048576;
const start = rss();
let peak = start;
for (let i = 0; i < iterations; i += 1) {
  const frame = Buffer.allocUnsafe(frameBytes);
  frame[0] = i & 0xff;
  frame[frame.length - 1] = (i * 7) & 0xff;
  const event = { id: i, kind: i % 3 ? 'vision' : 'action', ts: Date.now(), roi: [i, i, 64, 64] };
  if (event.id < 0) throw new Error('unreachable');
  frame.fill(0);
  if (i % 50 === 49) {
    if (typeof global.gc === 'function') global.gc();
    peak = Math.max(peak, rss());
  }
}
if (typeof global.gc === 'function') global.gc();
const end = rss();
console.log(JSON.stringify({
  iterations,
  rss_start_mb: Number(start.toFixed(1)),
  rss_end_mb: Number(end.toFixed(1)),
  rss_peak_mb: Number(peak.toFixed(1)),
  rss_delta_mb: Number((end - start).toFixed(1)),
}));
if (end - start > 256) process.exitCode = 2;
