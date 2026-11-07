#!/usr/bin/env node
/**
 * @file cli.mjs — terminal proof that the VHCU bus works end to end.
 *
 * Flow: spawn Python -> READY -> ping/sys.info -> vision.start(fake) ->
 * collect diff events -> pull one PNG from the image plane -> status ->
 * stop vision (and prove it went quiet) -> shutdown -> no zombie.
 *
 * Options:
 *   --python <path>    interpreter (default: VHCU_PYTHON or auto-detect)
 *   --source fake|mss  capture backend (default fake)
 *   --fps <n>          capture rate (default 8)
 *   --events <n>       diff events to collect before proceeding (default 5)
 *   --kill-after       SIGKILL the daemon mid-run to prove auto-restart
 *   --no-frame         skip the image-plane check
 *   --json             print only the final RESULT line
 */

import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { Supervisor } from './lib/supervisor.js';
import { resolvePython } from './lib/python.js';
import { createLogger } from './lib/log.js';
import { fetchFrame, isPng } from './lib/frames.js';
import { validateFrame } from '../contract/lib/contract.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '..');
const VISION = join(ROOT, 'vision');
const ARTIFACTS = join(ROOT, '.artifacts');

function parseArgs(argv) {
  const args = { source: 'fake', fps: 8, events: 5, frame: true, json: false, killAfter: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--python') args.python = argv[++i];
    else if (arg === '--source') args.source = argv[++i];
    else if (arg === '--fps') args.fps = Number(argv[++i]);
    else if (arg === '--events') args.events = Number(argv[++i]);
    else if (arg === '--kill-after') args.killAfter = true;
    else if (arg === '--no-frame') args.frame = false;
    else if (arg === '--json') args.json = true;
    else if (arg === '--help' || arg === '-h') {
      console.log('usage: node host/cli.mjs [--python PATH] [--source fake|mss] [--fps N] [--events N] [--kill-after] [--no-frame] [--json]');
      process.exit(0);
    } else {
      throw new Error(`unknown argument ${arg}`);
    }
  }
  return args;
}

function onceEvent(emitter, name, predicate = () => true, timeoutMs = 5000) {
  return new Promise((resolvePromise, reject) => {
    const timer = setTimeout(() => {
      emitter.off(name, handler);
      reject(new Error(`timeout waiting for ${name}`));
    }, timeoutMs);
    function handler(payload) {
      if (!predicate(payload)) return;
      clearTimeout(timer);
      emitter.off(name, handler);
      resolvePromise(payload);
    }
    emitter.on(name, handler);
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const logger = createLogger({ level: args.json ? 'error' : 'info', prefix: 'vhcu-cli' });
  const startedAt = Date.now();

  const python = await resolvePython({ pythonPath: args.python });
  logger.info(`python ${python.version} (${python.executable}) source=${python.source}`);

  const supervisor = new Supervisor({
    python,
    visionPath: VISION,
    daemonArgs: ['-u', '-m', 'vhcu_daemon'],
    cwd: ROOT,
    logger,
  });
  supervisor.installExitHook();

  const timings = {};
  const diffs = [];
  supervisor.on('stderr', (line) => logger.debug('daemon', line));
  supervisor.on('protocol-error', (frame) => logger.warn(`protocol ${frame.code}`, frame.message));
  supervisor.on('failure', (failure) => logger.warn(`failure ${failure.kind}`, failure.message));
  supervisor.on('state', (state) => logger.debug(`state ${state}`));

  let exitCode = 0;
  try {
    const readyT0 = Date.now();
    let ready = await supervisor.start();
    timings.readyMs = Date.now() - readyT0;
    logger.info(`ready pid=${ready.pid} port=${ready.port} sources=${ready.capabilities.sources.join(',')}`, {
      python: ready.python?.version,
    });

    const pong = await supervisor.send('ping', {}, { timeoutMs: 2000 });
    logger.info(`ping pong=${pong.pong} pid=${pong.pid}`);

    const sysinfo = await supervisor.send('sys.info', {}, { timeoutMs: 3000 });
    logger.info(`sys.info python=${sysinfo.python} platform=${sysinfo.platform}`);

    if (args.killAfter) {
      logger.warn('kill-after: SIGKILL daemon to prove supervised restart');
      const pid = supervisor.pid;
      const exited = onceEvent(supervisor, 'exit', () => true, 5000);
      supervisor.child.kill('SIGKILL');
      await exited;
      const reReady = await onceEvent(supervisor, 'ready', (frame) => frame.pid !== pid, 8000);
      timings.restartMs = Date.now() - readyT0;
      logger.info(`restarted pid=${reReady.pid} restarts=${supervisor.restarts}`);
      // The image-plane port is per process: anything cached from the first
      // READY (port/token) is dead now.
      ready = reReady;
      await supervisor.send('ping', {}, { timeoutMs: 2000 });
    }

    supervisor.on('event', (frame) => {
      if (frame.kind === 'vision.diff') diffs.push(frame);
    });

    const startT0 = Date.now();
    // Register before sending: the daemon may emit vision.started in the same
    // tick as the ack, so waiting for the ack first would miss the event.
    const startedEvent = onceEvent(
      supervisor,
      'event',
      (frame) => frame.kind === 'vision.started',
      5000,
    );
    await supervisor.send('vision.start', { source: args.source, fps: args.fps }, { timeoutMs: 5000 });
    timings.visionStartMs = Date.now() - startT0;
    await startedEvent;

    const deadline = Date.now() + 8000;
    while (diffs.length < args.events && Date.now() < deadline) await sleep(50);
    if (diffs.length === 0) throw new Error('no vision.diff events received');
    timings.firstDiffMs = Date.now() - startT0;

    for (const frame of diffs.slice(0, 3)) {
      const check = validateFrame(frame);
      if (!check.ok) throw new Error(`diff frame failed schema: ${check.errors.join('; ')}`);
    }
    const diffMs = diffs.map((frame) => frame.stats.diffMs).sort((a, b) => a - b);
    const p50 = diffMs[Math.floor(diffMs.length / 2)];
    logger.info(
      `diffs=${diffs.length} rois=${diffs[diffs.length - 1].rois.length} p50DiffMs=${p50.toFixed(2)} ` +
        `screen=${diffs[0].screen.width}x${diffs[0].screen.height}`,
    );

    const status = await supervisor.send('vision.status', {}, { timeoutMs: 2000 });
    logger.info(`status frames=${status.frames} fps=${status.fps} captureMs=${status.captureMs}`);
    timings.captureMs = status.captureMs;

    if (args.frame) {
      const roi = diffs[diffs.length - 1].rois[0];
      const image = await fetchFrame({
        port: ready.port,
        token: ready.token,
        x: roi?.x,
        y: roi?.y,
        w: roi?.w,
        h: roi?.h,
        maxWidth: 320,
      });
      if (!isPng(image.buffer)) throw new Error('image plane did not return a PNG');
      mkdirSync(ARTIFACTS, { recursive: true });
      const file = join(ARTIFACTS, `vhcu-frame-${Date.now()}.png`);
      writeFileSync(file, image.buffer);
      logger.info(`frame ${image.bytes}B -> ${file}`);
      timings.frameBytes = image.bytes;
    }

    await supervisor.send('vision.snapshot', {}, { timeoutMs: 2000 });
    await supervisor.send('vision.stop', { reason: 'cli-check' }, { timeoutMs: 3000 });
    const before = diffs.length;
    await sleep(600);
    if (diffs.length !== before) throw new Error('diff events continued after vision.stop');
    logger.info(`vision stopped: events stayed at ${before} for 600ms`);

    const stopT0 = Date.now();
    await supervisor.stop('cli-done');
    timings.shutdownMs = Date.now() - stopT0;
    logger.info(`shutdown ${timings.shutdownMs}ms; child pid=${ready.pid} alive=${isAlive(ready.pid)}`);

    const result = {
      ok: true,
      python: python.version,
      readyMs: timings.readyMs,
      visionStartMs: timings.visionStartMs,
      firstDiffMs: timings.firstDiffMs,
      p50DiffMs: Number(p50.toFixed(2)),
      captureMs: Number(status.captureMs),
      diffs: diffs.length,
      frameBytes: timings.frameBytes ?? null,
      shutdownMs: timings.shutdownMs,
      restarted: args.killAfter ? supervisor.restarts > 0 : null,
      zombie: isAlive(ready.pid),
      elapsedMs: Date.now() - startedAt,
    };
    if (!args.json) logger.info('RESULT', result);
    process.stdout.write(`RESULT ${JSON.stringify(result)}\n`);
    if (result.zombie) exitCode = 3;
  } catch (error) {
    logger.error('cli failed', error.message);
    if (error.hint) logger.error(`hint: ${error.hint}`);
    if (!args.json) console.error(error.stack);
    process.stdout.write(`RESULT ${JSON.stringify({ ok: false, error: error.message })}\n`);
    exitCode = 1;
  } finally {
    supervisor.dispose();
    await supervisor.stop('final').catch(() => {});
  }
  process.exit(exitCode);
}

function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

main();
