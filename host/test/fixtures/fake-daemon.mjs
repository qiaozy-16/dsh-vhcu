#!/usr/bin/env node
/**
 * @file fake-daemon.mjs — protocol-complete stand-in for lifecycle tests.
 *
 * It exists so supervisor tests never depend on Python being installed or on
 * real capture hardware: crash loops, hangs and slow shutdowns are all modes.
 */

import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  ackFrame,
  encodeFrame,
  errorFrame,
  eventFrame,
  makeEnvelope,
} from '../../../contract/lib/contract.js';

const HERE = dirname(fileURLToPath(import.meta.url));
void HERE;

const MODE = process.env.FAKE_MODE ?? 'normal';
const HEARTBEAT_MS = Number(process.env.FAKE_HEARTBEAT_MS ?? 100);
let seq = 0;
let frameNo = 0;

function send(frame) {
  process.stdout.write(encodeFrame(frame));
}

function sendReady() {
  if (MODE === 'crash-on-start') process.exit(3);
  if (MODE === 'hang') return;
  send(
    makeEnvelope('ready', ++seq, {
      proto: 1,
      pid: process.pid,
      port: 1,
      token: 'a'.repeat(32),
      capabilities: { vision: true, sources: ['fake'], maxFps: 15 },
      startedAt: Date.now(),
    }),
  );
}

function heartbeat() {
  if (MODE === 'no-heartbeat') return;
  if (MODE === 'gap') seq += 3;
  send(
    makeEnvelope('heartbeat', ++seq, {
      uptimeMs: Math.round(process.uptime() * 1000),
      frames: frameNo,
      diffs: frameNo,
      dropped: 0,
      fps: 10,
      queueDepth: 0,
    }),
  );
}

let diffTimer = null;

function startDiffs() {
  if (diffTimer) return;
  diffTimer = setInterval(() => {
    frameNo += 1;
    send(
      eventFrame({
        seq: ++seq,
        kind: 'vision.diff',
        fields: {
          screen: { width: 64, height: 64, scale: 1 },
          rois: [{ id: 1, x: 4, y: 4, w: 16, h: 16, score: 0.25, hash: 'deadbeef' }],
          stats: { frame: frameNo, captureMs: 0.1, diffMs: 0.2 },
        },
      }),
    );
  }, 50);
}

function onCommand(frame) {
  const reply = (ok, result, error) =>
    send(ackFrame({ seq: ++seq, cmdId: frame.id, ok, result, error }));

  switch (frame.op) {
    case 'ping':
      if (MODE === 'crash-on-cmd') process.exit(4);
      if (MODE === 'hang-on-cmd') return;
      reply(true, { pong: true, pid: process.pid });
      return;
    case 'sys.info':
      reply(true, { mode: MODE, node: process.version });
      return;
    case 'vision.start':
      reply(true, { source: 'fake' });
      send(eventFrame({ seq: ++seq, kind: 'vision.started', fields: { source: 'fake', fps: 10 } }));
      startDiffs();
      return;
    case 'vision.stop':
      if (diffTimer) clearInterval(diffTimer);
      diffTimer = null;
      reply(true, { stopped: true });
      send(eventFrame({ seq: ++seq, kind: 'vision.stopped', fields: { reason: 'cmd' } }));
      return;
    case 'vision.status':
      reply(true, { frames: frameNo, state: diffTimer ? 'running' : 'stopped' });
      return;
    case 'vision.snapshot':
      reply(true, {});
      send(
        makeEnvelope('snapshot', ++seq, {
          asOfSeq: seq,
          values: { frames: frameNo, vision: diffTimer ? 'running' : 'stopped' },
        }),
      );
      return;
    case 'shutdown':
      if (MODE === 'ignore-shutdown') return;
      reply(true, { bye: true });
      setTimeout(() => process.exit(0), 10);
      return;
    default:
      send(errorFrame({ seq: ++seq, code: 'E_DENIED', message: `unknown op ${frame.op}` }));
  }
}

const decoder = (text) => {
  for (const line of text.split('\n')) {
    if (!line.trim()) continue;
    let frame;
    try {
      frame = JSON.parse(line);
    } catch {
      send(errorFrame({ seq: ++seq, code: 'E_PROTO', message: 'invalid json' }));
      continue;
    }
    if (frame.type === 'cmd') onCommand(frame);
    if (frame.type === 'shutdown') {
      if (MODE === 'ignore-shutdown') continue;
      process.exit(0);
    }
  }
};

let buffer = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buffer += chunk;
  let index;
  while ((index = buffer.indexOf('\n')) !== -1) {
    const line = buffer.slice(0, index);
    buffer = buffer.slice(index + 1);
    decoder(`${line}\n`);
  }
});

setTimeout(sendReady, 20);
setInterval(heartbeat, HEARTBEAT_MS);

process.on('SIGTERM', () => {
  if (MODE === 'ignore-sigterm') return;
  process.exit(0);
});
