/**
 * @file protocol.js — NDJSON line decoder for the stdio control plane.
 *
 * The reader must never unbounded-buffer: a child that prints a 10 MB line
 * (a stray traceback, a broken library) must not take the host down with it.
 */

import { MAX_LINE_BYTES } from '../../contract/lib/contract.js';

export class LineDecoder {
  /**
   * @param {{maxLineBytes?: number, onLine: (line: string) => void,
   *          onError?: (error: Error & {code?: string}) => void}} options
   */
  constructor({ maxLineBytes = MAX_LINE_BYTES, onLine, onError } = {}) {
    if (typeof onLine !== 'function') throw new TypeError('LineDecoder requires onLine');
    this.maxLineBytes = maxLineBytes;
    this.onLine = onLine;
    this.onError = onError ?? (() => {});
    this.buffer = Buffer.alloc(0);
    this.droppedBytes = 0;
    this.lines = 0;
  }

  push(chunk) {
    if (!chunk || chunk.length === 0) return;
    this.buffer = this.buffer.length === 0 ? chunk : Buffer.concat([this.buffer, chunk]);
    for (;;) {
      const index = this.buffer.indexOf(0x0a);
      if (index === -1) break;
      const raw = this.buffer.subarray(0, index);
      this.buffer = this.buffer.subarray(index + 1);
      if (raw.length > this.maxLineBytes) {
        this.droppedBytes += raw.length;
        this.onError(Object.assign(new Error(`line exceeds ${this.maxLineBytes}B`), { code: 'E_PROTO' }));
        continue;
      }
      const text = raw.toString('utf8').replace(/\r$/, '');
      if (text.trim() === '') continue;
      this.lines += 1;
      this.onLine(text);
    }
    if (this.buffer.length > this.maxLineBytes) {
      // No newline in sight and already over budget: drop the tail until the
      // next newline so a broken writer cannot pin memory.
      this.droppedBytes += this.buffer.length;
      this.buffer = Buffer.alloc(0);
      this.onError(Object.assign(new Error('unterminated oversized line dropped'), { code: 'E_PROTO' }));
    }
  }

  reset() {
    this.buffer = Buffer.alloc(0);
  }
}
