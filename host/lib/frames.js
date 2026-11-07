/**
 * @file frames.js — pull one PNG from the daemon's loopback image plane.
 *
 * The image plane is pull-only on purpose: previews are optional, cancellable
 * and must never back-pressure the JSON control plane.
 */

import http from 'node:http';

const MAX_BYTES = 8 * 1024 * 1024;

/**
 * @param {{
 *   port: number, token: string,
 *   x?: number, y?: number, w?: number, h?: number,
 *   maxWidth?: number, timeoutMs?: number,
 * }} options
 * @returns {Promise<{buffer: Buffer, contentType: string, status: number, bytes: number}>}
 */
export function fetchFrame(options) {
  const {
    port,
    token,
    x,
    y,
    w,
    h,
    maxWidth = 960,
    timeoutMs = 2000,
  } = options;

  if (!Number.isInteger(port) || port <= 0) {
    return Promise.reject(Object.assign(new Error('invalid image plane port'), { code: 'E_UNAVAILABLE' }));
  }
  const query = new URLSearchParams({ token: String(token ?? ''), maxw: String(maxWidth) });
  for (const [key, value] of Object.entries({ x, y, w, h })) {
    if (Number.isInteger(value)) query.set(key, String(value));
  }
  const path = `/frame?${query.toString()}`;

  return new Promise((resolve, reject) => {
    const request = http.get(
      { host: '127.0.0.1', port, path, timeout: timeoutMs, headers: { accept: 'image/png' } },
      (response) => {
        const chunks = [];
        let bytes = 0;
        response.on('data', (chunk) => {
          bytes += chunk.length;
          if (bytes > MAX_BYTES) {
            request.destroy(Object.assign(new Error('frame exceeds 8 MiB'), { code: 'E_PROTO' }));
            return;
          }
          chunks.push(chunk);
        });
        response.on('end', () => {
          const buffer = Buffer.concat(chunks);
          if (response.statusCode !== 200) {
            reject(
              Object.assign(new Error(`frame request failed: HTTP ${response.statusCode} ${buffer.toString('utf8').slice(0, 200)}`), {
                code: 'E_CAPTURE',
                status: response.statusCode,
              }),
            );
            return;
          }
          resolve({
            buffer,
            contentType: String(response.headers['content-type'] ?? ''),
            status: response.statusCode,
            bytes: buffer.length,
          });
        });
      },
    );
    request.on('timeout', () => request.destroy(Object.assign(new Error('frame request timeout'), { code: 'E_TIMEOUT' })));
    request.on('error', (error) => reject(error));
  });
}

export function isPng(buffer) {
  return (
    Buffer.isBuffer(buffer) &&
    buffer.length >= 8 &&
    buffer[0] === 0x89 &&
    buffer[1] === 0x50 &&
    buffer[2] === 0x4e &&
    buffer[3] === 0x47
  );
}
