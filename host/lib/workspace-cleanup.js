/**
 * @file workspace-cleanup.js — P9: keep VHCU frames out of the user workspace.
 *
 * VHCU serves frames from daemon memory over a signed loopback URL; it never
 * writes screenshots to disk. A model can still download a frame with shell
 * tools, so every served PNG carries a tEXt marker and startup/shutdown can
 * delete marked images plus the crop scripts that reference /vhcu/frame.
 */

import { closeSync, openSync, readFileSync, readSync, readdirSync, rmSync, statSync } from 'node:fs';
import { join } from 'node:path';

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const MARKER_KEYWORD = 'VHCU-Frame';
const MARKER_TEXT = `${MARKER_KEYWORD}\0memory-cache-ttl=300s`;
const MAX_PNG_BYTES = 32 * 1024 * 1024;
const MAX_SCRIPT_BYTES = 256 * 1024;

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) crc = CRC_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

/**
 * Insert a tEXt chunk so a downloaded copy is identifiable as a VHCU frame.
 * The PNG pixel payload is untouched; viewers ignore the ancillary chunk.
 */
export function markVhcuPng(buffer) {
  if (!Buffer.isBuffer(buffer) || buffer.length < 33 || !buffer.subarray(0, 8).equals(PNG_SIGNATURE)) {
    return buffer;
  }
  let offset = 8;
  while (offset + 12 <= buffer.length) {
    const length = buffer.readUInt32BE(offset);
    const type = buffer.subarray(offset + 4, offset + 8);
    const next = offset + 12 + length;
    if (next > buffer.length) return buffer;
    if (type.toString('latin1') === 'IEND') {
      const data = Buffer.from(MARKER_TEXT, 'latin1');
      const typeBuf = Buffer.from('tEXt', 'latin1');
      const chunk = Buffer.alloc(12 + data.length);
      chunk.writeUInt32BE(data.length, 0);
      typeBuf.copy(chunk, 4);
      data.copy(chunk, 8);
      chunk.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])), 8 + data.length);
      return Buffer.concat([buffer.subarray(0, offset), chunk, buffer.subarray(offset)]);
    }
    offset = next;
  }
  return buffer;
}

/** True when a PNG was served by VHCU (carries the frame tEXt marker). */
export function isVhcuFramePng(buffer) {
  if (!Buffer.isBuffer(buffer) || buffer.length < 33 || !buffer.subarray(0, 8).equals(PNG_SIGNATURE)) {
    return false;
  }
  return buffer.includes(Buffer.from(MARKER_TEXT, 'latin1'));
}

function readTail(path, size) {
  const fd = openSync(path, 'r');
  try {
    const length = Math.min(size, 64 * 1024);
    const buffer = Buffer.alloc(length);
    const read = readSync(fd, buffer, 0, length, Math.max(0, size - length));
    return buffer.subarray(0, read);
  } finally {
    closeSync(fd);
  }
}

function isVhcuScript(name, path, maxAgeMs, now) {
  if (!/\.(ps1|cmd|bat|sh|py)$/i.test(name)) return false;
  const explicitlyNamed = /^vhcu[-_][^/]*$/i.test(name);
  const cropScript = /^crop[^/]*$/i.test(name);
  if (!explicitlyNamed && !cropScript) return false;
  const info = statSync(path);
  if (maxAgeMs > 0 && now - info.mtimeMs < maxAgeMs) return false;
  if (info.size > MAX_SCRIPT_BYTES) return false;
  // vhcu-* is our own temp-script namespace (e.g. vhcu-captcha-*.ps1).
  if (explicitlyNamed) return true;
  const text = readFileSync(path, 'utf8');
  return text.includes('/vhcu/frame');
}

/**
 * Remove VHCU-derived workspace debris. Only files with the frame marker or
 * crop scripts that fetch /vhcu/frame are touched; nothing else is deleted.
 *
 * @returns {{removed: string[], kept: number, errors: string[]}}
 */
export function cleanupWorkspaceArtifacts({
  root = process.cwd(),
  minAgeMs = 60_000,
  maxEntries = 500,
  now = Date.now(),
} = {}) {
  const removed = [];
  const errors = [];
  let kept = 0;
  let entries;
  try {
    entries = readdirSync(root, { withFileTypes: true });
  } catch (error) {
    return { removed, kept, errors: [`readdir: ${error?.message ?? String(error)}`] };
  }
  let inspected = 0;
  for (const entry of entries) {
    if (inspected >= maxEntries) break;
    if (!entry.isFile()) continue;
    inspected += 1;
    const name = entry.name;
    const path = join(root, name);
    try {
      if (/\.png$/i.test(name)) {
        const info = statSync(path);
        if (info.size > MAX_PNG_BYTES) continue;
        if (minAgeMs > 0 && now - info.mtimeMs < minAgeMs) continue;
        if (isVhcuFramePng(readTail(path, info.size))) {
          rmSync(path, { force: true });
          removed.push(name);
        } else {
          kept += 1;
        }
        continue;
      }
      if (isVhcuScript(name, path, minAgeMs, now)) {
        rmSync(path, { force: true });
        removed.push(name);
        continue;
      }
      kept += 1;
    } catch (error) {
      errors.push(`${name}: ${error?.message ?? String(error)}`);
    }
  }
  return { removed, kept, errors };
}
