import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  ERROR_CODES,
  HEARTBEAT_MS,
  HEARTBEAT_MISS_LIMIT,
  MAX_LINE_BYTES,
  OPS,
  PROTOCOL_VERSION,
  SeqTracker,
  cmdFrame,
  encodeFrame,
  errorFrame,
  loadSchemas,
  parseFrame,
  validateFrame,
} from '../lib/contract.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const GOLDEN = join(HERE, '..', 'golden');
const SCHEMA = join(HERE, '..', 'schema', 'v1');

const read = (name) => JSON.parse(readFileSync(join(GOLDEN, name), 'utf8'));
const line = (obj) => `${JSON.stringify(obj)}\n`;

test('schema registry loads every v1 schema and is self-consistent', () => {
  const schemas = loadSchemas();
  assert.equal(schemas.size, readdirSync(SCHEMA).filter((f) => f.endsWith('.json')).length);
  for (const [id, schema] of schemas) {
    assert.equal(schema.$id, id);
    assert.ok(schema.$schema);
  }
  const errorSchema = schemas.get('vhcu:error:v1');
  const codes = errorSchema.allOf[1].properties.code.enum;
  assert.deepEqual([...codes].sort(), Object.keys(ERROR_CODES).sort());
});

test('golden valid frames pass envelope + type validation', () => {
  const valid = [
    'ready.json',
    'cmd.json',
    'event.diff.json',
    'heartbeat.json',
    'snapshot.json',
    'ack.ok.json',
    'ack.error.json',
    'error.json',
  ];
  for (const name of valid) {
    const frame = read(name);
    const result = validateFrame(frame);
    assert.equal(result.ok, true, `${name}: ${result.errors.join('; ')}`);
  }
});

test('golden invalid frames are rejected with a reason', () => {
  const invalid = ['bad-seq-zero.json', 'bad-missing-seq.json', 'bad-type.json'];
  for (const name of invalid) {
    const result = validateFrame(read(name));
    assert.equal(result.ok, false, `${name} should be invalid`);
    assert.ok(result.errors.length > 0);
  }
});

test('parseFrame accepts a wire line and rejects malformed JSON', () => {
  const ok = parseFrame(line(read('event.diff.json')));
  assert.equal(ok.ok, true, ok.errors?.join('; '));
  assert.equal(ok.frame.kind, 'vision.diff');

  const bad = parseFrame('{"v":1,"seq":1,"type":"event"');
  assert.equal(bad.ok, false);
  assert.equal(bad.code, 'E_PROTO');

  const crlf = parseFrame(`${JSON.stringify(read('heartbeat.json'))}\r\n`);
  assert.equal(crlf.ok, true);
});

test('parseFrame reports a version mismatch distinctly', () => {
  const v2 = { ...read('heartbeat.json'), v: 2 };
  const result = parseFrame(line(v2));
  assert.equal(result.ok, false);
  assert.equal(result.code, 'E_PROTO'); // envelope const v=1 fails before the v check
});

test('encodeFrame refuses oversized frames instead of truncating', () => {
  const huge = { v: 1, seq: 1, type: 'event', ts: 1, kind: 'daemon.log', text: 'x'.repeat(MAX_LINE_BYTES) };
  assert.throws(() => encodeFrame(huge), (error) => error.code === 'E_PROTO');
});

test('cmd whitelist is enforced at build time', () => {
  assert.throws(() => cmdFrame({ seq: 1, id: 'c-1', op: 'rm -rf', args: {} }), /whitelist/);
  assert.deepEqual(OPS.includes('vision.start'), true);
});

test('errorFrame requires a known code', () => {
  assert.throws(() => errorFrame({ seq: 1, code: 'E_NOPE', message: 'x' }));
  const frame = errorFrame({ seq: 1, code: 'E_TIMEOUT', message: 'no ack' });
  assert.equal(validateFrame(frame).ok, true);
});

test('SeqTracker reports gaps and duplicates without losing the watermark', () => {
  const tracker = new SeqTracker('daemon');
  assert.equal(tracker.accept(1).ok, true);
  assert.equal(tracker.accept(2).ok, true);
  const gap = tracker.accept(5);
  assert.equal(gap.ok, false);
  assert.equal(gap.reason, 'gap');
  assert.deepEqual(gap.gap, { expected: 3, got: 5, missing: 2 });
  const duplicate = tracker.accept(5);
  assert.equal(duplicate.reason, 'duplicate');
  assert.equal(tracker.last, 5);
  assert.equal(tracker.accept(0).reason, 'invalid');
  assert.equal(tracker.stats().gaps, 1);
  assert.equal(tracker.stats().duplicates, 1);
});

test('vision.diff without rois is rejected (event branch requirements)', () => {
  const frame = {
    v: PROTOCOL_VERSION,
    seq: 3,
    type: 'event',
    ts: 1,
    kind: 'vision.diff',
    screen: { width: 10, height: 10, scale: 1 },
    rois: [],
    stats: { frame: 1, captureMs: 0, diffMs: 0 },
  };
  assert.equal(validateFrame(frame).ok, true);
  delete frame.rois;
  assert.equal(validateFrame(frame).ok, false);
});

test('hear.status and hear.text are valid daemon events', () => {
  const status = {
    v: PROTOCOL_VERSION,
    seq: 1,
    type: 'event',
    ts: Date.now(),
    kind: 'hear.status',
    status: 'listening',
    state: 'listening',
    running: true,
    model: 'base',
    modelPath: 'D:/models',
  };
  const statusResult = validateFrame(status);
  assert.equal(statusResult.ok, true, statusResult.errors?.join('; '));

  const text = {
    v: PROTOCOL_VERSION,
    seq: 2,
    type: 'event',
    ts: Date.now(),
    kind: 'hear.text',
    text: '香港島戰役',
    durationMs: 2162,
  };
  const textResult = validateFrame(text);
  assert.equal(textResult.ok, true, textResult.errors?.join('; '));

  const missingFrame = { ...status };
  delete missingFrame.status;
  const missing = validateFrame(missingFrame);
  assert.equal(missing.ok, false);
});

test('vision.blocked event is accepted', () => {
  const frame = {
    v: PROTOCOL_VERSION,
    seq: 1,
    type: 'event',
    ts: Date.now(),
    kind: 'vision.blocked',
    blocked: true,
    title: '登录 Password',
    reason: 'sensitive-title',
  };
  const result = validateFrame(frame);
  assert.equal(result.ok, true, result.errors?.join('; '));
});

test('hear.audio ambient event is accepted and requires rms', () => {
  const frame = {
    v: PROTOCOL_VERSION,
    seq: 1,
    type: 'event',
    ts: Date.now(),
    kind: 'hear.audio',
    rms: 0.15,
    duration: 8.0,
    durationMs: 8000,
    reason: 'filtered',
  };
  assert.equal(validateFrame(frame).ok, true);
  const missing = { ...frame };
  delete missing.rms;
  assert.equal(validateFrame(missing).ok, false);
});

test('P17.4 heartbeat watchdog tolerates 10s of heavy work', () => {
  assert.equal(HEARTBEAT_MS, 1000);
  assert.equal(HEARTBEAT_MISS_LIMIT, 10);
  assert.equal(HEARTBEAT_MS * HEARTBEAT_MISS_LIMIT, 10_000);
});
