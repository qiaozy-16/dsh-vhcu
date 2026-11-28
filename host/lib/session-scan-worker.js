/**
 * @file session-scan-worker.js — decode session logs off the host event loop.
 *
 * Scanning 54 session logs (~137 MB compressed, ~12 s first run) inside the host
 * main thread would freeze the UI and the model stream. The main thread keeps the
 * per-file cache and only ships the changed files here; this worker does the
 * zstd decoding and returns attachment ids per file.
 *
 * Never validates or mutates the host; read-only.
 */

import { readFileSync } from 'node:fs';
import { parentPort, workerData } from 'node:worker_threads';

import { decodeZstdFrames, extractAttachmentIds, looksLikeJsonl } from './session-refs.js';

const { files = [], maxFileBytes = 128 * 1024 * 1024 } = workerData ?? {};

const perFile = [];
const failed = [];
let bytes = 0;

for (const file of files) {
  if (!file || typeof file.path !== 'string') continue;
  if (Number(file.size) > maxFileBytes) {
    failed.push(file.path);
    continue;
  }
  let buffer;
  try {
    buffer = readFileSync(file.path);
  } catch {
    failed.push(file.path);
    continue;
  }
  const decoded = decodeZstdFrames(buffer);
  if (decoded.failedFrames > 0 || !looksLikeJsonl(decoded.text)) {
    failed.push(file.path);
    continue;
  }
  perFile.push({ path: file.path, ids: [...extractAttachmentIds(decoded.text)] });
  bytes += Number(file.size) || 0;
}

parentPort?.postMessage({ perFile, failed, bytes });
