/**
 * @file supervisor.js — owns exactly one Python daemon child process.
 *
 * Contract (P0):
 *  A. Spawn with ELECTRON_RUN_AS_NODE deleted, detached:false, bounded stdio.
 *  B. READY gate (15s) before any command; commands are request/response with
 *     per-command deadlines.
 *  C. Heartbeat watchdog: a hung daemon never exits, so "no heartbeat" must be
 *     treated as death, not waited on.
 *  D. Stop is idempotent and three-stage: shutdown frame -> SIGTERM -> kill tree.
 *  E. Restart backoff + breaker: a crash loop must not become a fork bomb and
 *     must never take the host down. The restart timer is a real pending task,
 *     so it is deliberately NOT unref'd (see dsh-island P1-03 lesson).
 *  F. We only ever signal the ChildProcess we own and only while
 *     exitCode === null — never a pid we merely remember.
 */

import { spawn as nodeSpawn, spawnSync } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { delimiter } from 'node:path';

import * as C from '../../contract/lib/contract.js';
import { LineDecoder } from './protocol.js';

const noopLogger = { debug() {}, info() {}, warn() {}, error() {}, child() { return noopLogger; } };

/**
 * Windows reports a killed child with exitCode === null and signalCode set.
 * Checking only exitCode would treat a dead child as alive and hang every
 * subsequent wait — the exact failure this helper prevents.
 */
function hasExited(child) {
  return !child || child.exitCode !== null || child.signalCode !== null;
}

export class Supervisor extends EventEmitter {
  /**
   * @param {{
   *   python: {cmd: string, args: string[], version?: string},
   *   visionPath: string,
   *   daemonArgs?: string[],
   *   cwd?: string,
   *   env?: Record<string,string>,
   *   logger?: object,
   *   spawnImpl?: Function,
   *   readyTimeoutMs?: number,
   *   heartbeatMs?: number,
   *   heartbeatMissLimit?: number,
   *   restartBackoffMinMs?: number,
   *   restartBackoffMaxMs?: number,
   *   breakerMaxFailures?: number,
   *   breakerWindowMs?: number,
   *   shutdownGraceMs?: number,
   *   termGraceMs?: number,
   * }} options
   */
  constructor(options) {
    super();
    if (!options?.python?.cmd) throw new TypeError('Supervisor requires options.python');
    if (!options?.visionPath) throw new TypeError('Supervisor requires options.visionPath');

    this.logger = options.logger ?? noopLogger;
    this.python = options.python;
    this.visionPath = options.visionPath;
    this.daemonArgs = options.daemonArgs ?? ['-u', '-m', 'vhcu_daemon'];
    this.cwd = options.cwd ?? process.cwd();
    this.env = options.env ?? {};
    this.resourcePaths = options.resourcePaths ?? null;
    this.spawnImpl = options.spawnImpl ?? nodeSpawn;

    this.readyTimeoutMs = options.readyTimeoutMs ?? C.READY_TIMEOUT_MS;
    this.heartbeatMs = options.heartbeatMs ?? C.HEARTBEAT_MS;
    this.heartbeatMissLimit = options.heartbeatMissLimit ?? C.HEARTBEAT_MISS_LIMIT;
    this.restartBackoffMinMs = options.restartBackoffMinMs ?? C.RESTART_BACKOFF_MIN_MS;
    this.restartBackoffMaxMs = options.restartBackoffMaxMs ?? C.RESTART_BACKOFF_MAX_MS;
    this.breakerMaxFailures = options.breakerMaxFailures ?? C.BREAKER_MAX_FAILURES;
    this.breakerWindowMs = options.breakerWindowMs ?? C.BREAKER_WINDOW_MS;
    this.shutdownGraceMs = options.shutdownGraceMs ?? C.SHUTDOWN_GRACE_MS;
    this.termGraceMs = options.termGraceMs ?? C.TERM_GRACE_MS;

    /** @type {'idle'|'starting'|'ready'|'restarting'|'stopping'|'stopped'|'circuit-open'} */
    this.state = 'idle';
    this.child = null;
    this.pid = null;
    this.readyInfo = null;
    this.exitInfo = null;
    this.lastError = null;

    this.hostSeq = 1;
    this.daemonSeq = new C.SeqTracker('daemon');
    this.cmdCounter = 0;
    /** @type {Map<string, {resolve: Function, reject: Function, timer: NodeJS.Timeout, op: string}>} */
    this.pending = new Map();

    this.spawns = 0;
    this.restarts = 0;
    this.restartAttempt = 0;
    this.failures = [];
    this.lastHeartbeatAt = 0;
    this.lastHeartbeat = null;

    this.stderrRing = [];
    this.stderrLines = 0;
    this.maxStderrLines = 200;
    this.maxStderrChars = 500;

    this.timers = { ready: null, watchdog: null, restart: null };
    this.decoder = null;
    /** True when the next child exit is caused by our own failure cleanup. */
    this.expectingExit = false;
    this.stopping = false;
    this.stopPromise = null;
    this.startWaiter = null;
    this.exitWaiters = new Set();
    this._exitHook = null;
  }

  // ────────────────────────────── public ──────────────────────────────

  /** Start the daemon and resolve on READY. Rejects on timeout/breaker. */
  async start() {
    if (this.state === 'ready') return this.readyInfo;
    if (this.state === 'starting') return this._waitForReady();
    if (this.state === 'circuit-open') {
      const error = new Error(
        'supervisor is circuit-open; call restart() to reset the breaker before start()',
      );
      error.code = 'E_UNAVAILABLE';
      throw error;
    }
    this.stopping = false;
    this.stopPromise = null;
    this.state = 'starting';
    this.emit('state', this.state);
    const waiter = this._waitForReady();
    this._spawn();
    return waiter;
  }

  /**
   * Send one command. Resolves with ack.result; rejects with a classified
   * error (E_TIMEOUT / E_STATE / E_SHUTDOWN / daemon error code).
   */
  send(op, args = {}, { timeoutMs = C.DEFAULT_CMD_TIMEOUT_MS, id } = {}) {
    if (!C.OPS.includes(op)) {
      return Promise.reject(Object.assign(new Error(`op ${op} is not whitelisted`), { code: 'E_DENIED' }));
    }
    if (this.state !== 'ready' || hasExited(this.child)) {
      return Promise.reject(
        Object.assign(new Error(`cannot send ${op} in state ${this.state}`), { code: 'E_STATE' }),
      );
    }
    const cmdId = id ?? `c-${++this.cmdCounter}`;
    const frame = C.cmdFrame({ seq: this.hostSeq++, id: cmdId, op, args, deadlineMs: timeoutMs });
    const promise = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(cmdId);
        reject(
          Object.assign(new Error(`command ${op} timed out after ${timeoutMs}ms`), {
            code: 'E_TIMEOUT',
            cmdId,
          }),
        );
      }, timeoutMs);
      timer.unref?.();
      this.pending.set(cmdId, { resolve, reject, timer, op });
    });
    try {
      this._write(frame);
    } catch (error) {
      const entry = this.pending.get(cmdId);
      if (entry) {
        clearTimeout(entry.timer);
        this.pending.delete(cmdId);
      }
      return Promise.reject(error);
    }
    return promise;
  }

  /** Idempotent stop. Safe to call from a 'dispose' hook. */
  async stop(reason = 'host-stop') {
    if (this.stopPromise) return this.stopPromise;
    this.stopPromise = this._stop(reason);
    return this.stopPromise;
  }

  /** Manual recovery: reset the breaker, stop what is left, start again. */
  async restart(reason = 'manual-restart') {
    this.logger.warn('supervisor: manual restart', { reason });
    this.failures = [];
    this.restartAttempt = 0;
    if (this.state === 'circuit-open') {
      this.state = 'stopped';
      this.emit('state', this.state);
    }
    await this.stop(reason);
    this.restarts += 1;
    return this.start();
  }

  status() {
    return {
      state: this.state,
      pid: this.pid,
      spawns: this.spawns,
      restarts: this.restarts,
      restartAttempt: this.restartAttempt,
      failures: this.failures.length,
      breakerOpen: this.state === 'circuit-open',
      pendingCommands: this.pending.size,
      lastHeartbeatAt: this.lastHeartbeatAt,
      lastHeartbeat: this.lastHeartbeat,
      stderrLines: this.stderrLines,
      daemonSeq: this.daemonSeq.stats(),
      hostSeq: this.hostSeq - 1,
      ready: this.readyInfo,
      exit: this.exitInfo,
      lastError: this.lastError,
    };
  }

  /**
   * Send a high-priority interrupt to the loopback HTTP plane. This deliberately
   * bypasses the stdio command loop, which may be blocked in a macro right now.
   */
  async interrupt(reason = 'host-interrupt') {
    return this._interruptRequest(reason, false);
  }

  /** Clear a previously raised soft interrupt after the user resumes. */
  async clearInterrupt(reason = 'host-resume') {
    return this._interruptRequest(reason, true);
  }

  async _interruptRequest(reason, clear) {
    if (this.state !== 'ready' || hasExited(this.child)) return false;
    const port = Number(this.readyInfo?.port);
    const token = this.readyInfo?.token;
    if (!port || !token) return false;
    const query = new URLSearchParams({ token, reason: String(reason), ...(clear ? { clear: '1' } : {}) });
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 1500);
    timer.unref?.();
    try {
      const response = await fetch(`http://127.0.0.1:${port}/interrupt?${query.toString()}`, {
        method: 'POST',
        signal: controller.signal,
      });
      return response.ok;
    } catch (error) {
      this.logger.debug('supervisor: interrupt request failed', error?.message ?? String(error));
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  /** Recent stderr, oldest first (bounded). */
  stderrTail(limit = 20) {
    return this.stderrRing.slice(-limit);
  }

  /** Remove the process-exit hook installed by installExitHook(). */
  dispose() {
    if (this._exitHook) {
      process.removeListener('exit', this._exitHook);
      this._exitHook = null;
    }
    this.stop('dispose').catch(() => {});
  }

  /**
   * Best-effort synchronous kill on host exit. Deliberately spawnSync: an async
   * kill cannot run after the event loop is gone.
   */
  installExitHook() {
    if (this._exitHook) return;
    this._exitHook = () => {
      const child = this.child;
      if (hasExited(child)) return;
      try {
        if (process.platform === 'win32') {
          spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], {
            windowsHide: true,
            timeout: 2000,
          });
        } else {
          child.kill('SIGKILL');
        }
      } catch {
        // Nothing sensible left to do while the host is exiting.
      }
    };
    process.once('exit', this._exitHook);
  }

  // ────────────────────────────── internals ──────────────────────────────

  _waitForReady() {
    if (this.startWaiter) return this.startWaiter.promise;
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => {
      resolve = res;
      reject = rej;
    });
    this.startWaiter = { promise, resolve, reject };
    return promise;
  }

  _settleStart(error, info) {
    const waiter = this.startWaiter;
    if (!waiter) return;
    this.startWaiter = null;
    if (error) waiter.reject(error);
    else waiter.resolve(info);
  }

  _spawn() {
    this.spawns += 1;
    const args = [
      ...this.python.args,
      ...this.daemonArgs,
      '--parent-pid',
      String(process.pid),
      '--protocol',
      String(C.PROTOCOL_VERSION),
    ];
    this.logger.info('supervisor: spawn', { cmd: this.python.cmd, args: args.join(' ') });

    let child;
    try {
      child = this.spawnImpl(this.python.cmd, args, {
        cwd: this.cwd,
        env: this._buildEnv(),
        stdio: ['pipe', 'pipe', 'pipe'],
        windowsHide: true,
        detached: false,
      });
    } catch (error) {
      this._fail('spawn-sync', error);
      return;
    }

    this.child = child;
    this.pid = child.pid;
    this.lastHeartbeatAt = 0;
    this.lastHeartbeat = null;
    // Sequence numbers are per-process: a restarted daemon legitimately starts
    // again at 1, so the tracker must be recreated with the new child.
    this.daemonSeq = new C.SeqTracker('daemon');

    this.decoder = new LineDecoder({
      onLine: (line) => this._onLine(line, child),
      onError: (error) => this._protocolError('E_PROTO', error.message, false),
    });
    child.stdout.on('data', (chunk) => this.decoder.push(chunk));
    child.stderr.on('data', (chunk) => this._onStderr(chunk));
    // A dead child makes stdin emit EPIPE; without this handler the host would
    // crash on its next command instead of reporting a clean restart.
    child.stdin.on('error', (error) => this.logger.debug('supervisor: stdin error', error.message));

    child.on('error', (error) => {
      if (this.child !== child) return;
      this._fail('spawn-error', error);
    });
    child.on('exit', (code, signal) => this._onExit(child, code, signal));

    this._clearTimer('ready');
    this.timers.ready = setTimeout(() => {
      if (this.state !== 'starting') return;
      const error = Object.assign(new Error(`daemon did not send READY in ${this.readyTimeoutMs}ms`), {
        code: 'E_TIMEOUT',
      });
      this._fail('ready-timeout', error);
    }, this.readyTimeoutMs);
    this.timers.ready.unref?.();
  }

  _buildEnv() {
    const env = { ...process.env, ...this.env };
    // Hard delete: setting undefined would stringify to "undefined" in some
    // spawn paths and the child would still inherit the Electron flag.
    delete env.ELECTRON_RUN_AS_NODE;
    delete env.NODE_OPTIONS;
    env.PYTHONUNBUFFERED = '1';
    env.PYTHONIOENCODING = 'utf-8';
    env.PYTHONUTF8 = '1';
    env.PYTHONPATH = [this.visionPath, env.PYTHONPATH].filter(Boolean).join(delimiter);
    if (this.resourcePaths) {
      env.VHCU_MODEL_PATH = this.resourcePaths.models;
      env.VHCU_MACROS_PATH = this.resourcePaths.macros;
      env.VHCU_LOG_DIR = this.resourcePaths.logs;
    }
    return env;
  }

  _write(frame) {
    const child = this.child;
    if (hasExited(child)) {
      throw Object.assign(new Error('daemon is not running'), { code: 'E_SHUTDOWN' });
    }
    if (child.stdin.destroyed || child.stdin.writableEnded) {
      throw Object.assign(new Error('daemon stdin is closed'), { code: 'E_SHUTDOWN' });
    }
    child.stdin.write(C.encodeFrame(frame));
  }

  _onLine(line, child = this.child) {
    // A restarted supervisor may still receive buffered output from the old
    // child; never let it mutate state that belongs to the new one.
    if (child !== this.child) return;
    const parsed = C.parseFrame(line);
    if (!parsed.ok) {
      this._protocolError(parsed.code ?? 'E_PROTO', parsed.errors.join('; '), false);
      return;
    }
    const frame = parsed.frame;
    const accepted = this.daemonSeq.accept(frame.seq);
    if (accepted.reason === 'gap') {
      this._protocolError(
        'E_SEQ',
        `sequence gap: expected ${accepted.gap.expected}, got ${accepted.gap.got} (${accepted.gap.missing} missing)`,
        false,
      );
    } else if (accepted.reason === 'duplicate') {
      this._protocolError('E_SEQ', `duplicate seq ${frame.seq} (last ${this.daemonSeq.last})`, false);
      return;
    }
    this._dispatch(frame);
  }

  _dispatch(frame) {
    switch (frame.type) {
      case 'ready':
        this._onReady(frame);
        return;
      case 'ack':
        this._onAck(frame);
        return;
      case 'event':
        this.emit('event', frame);
        return;
      case 'heartbeat':
        this.lastHeartbeatAt = Date.now();
        this.lastHeartbeat = frame;
        this.emit('heartbeat', frame);
        return;
      case 'error':
        this.emit('protocol-error', frame);
        this.logger.warn('supervisor: daemon error frame', { code: frame.code, message: frame.message });
        if (frame.fatal) this._fail('daemon-fatal', Object.assign(new Error(frame.message), { code: frame.code }));
        return;
      case 'snapshot':
        this.emit('snapshot', frame);
        return;
      default:
        this._protocolError('E_PROTO', `unhandled frame type ${frame.type}`, false);
    }
  }

  _onReady(frame) {
    if (frame.proto !== C.PROTOCOL_VERSION) {
      const error = Object.assign(new Error(`daemon protocol ${frame.proto} != ${C.PROTOCOL_VERSION}`), {
        code: 'E_VERSION',
      });
      this._fail('version-mismatch', error);
      return;
    }
    this._clearTimer('ready');
    this.readyInfo = frame;
    // Start the silence window at READY: otherwise a daemon that never sends a
    // single heartbeat would look "fresh" forever (lastHeartbeatAt === 0).
    this.lastHeartbeatAt = Date.now();
    this.state = 'ready';
    this.restartAttempt = 0;
    this.emit('state', this.state);
    this.emit('ready', frame);
    this.logger.info('supervisor: ready', {
      pid: frame.pid,
      port: frame.port,
      sources: frame.capabilities?.sources,
    });
    this._startWatchdog();
    this._settleStart(null, frame);
  }

  _onAck(frame) {
    const entry = this.pending.get(frame.cmdId);
    if (!entry) {
      this.logger.warn('supervisor: ack for unknown command', { cmdId: frame.cmdId });
      return;
    }
    clearTimeout(entry.timer);
    this.pending.delete(frame.cmdId);
    if (frame.ok) {
      entry.resolve(frame.result ?? {});
      return;
    }
    const detail = frame.error ?? { code: 'E_INTERNAL', message: 'daemon reported failure' };
    entry.reject(
      Object.assign(new Error(detail.message), {
        code: detail.code ?? 'E_INTERNAL',
        hint: detail.hint,
        fatal: Boolean(detail.fatal),
        cmdId: frame.cmdId,
        op: entry.op,
      }),
    );
  }

  _startWatchdog() {
    this._clearTimer('watchdog');
    const interval = Math.max(100, Math.floor(this.heartbeatMs / 4));
    const limit = this.heartbeatMs * this.heartbeatMissLimit;
    this.timers.watchdog = setInterval(() => {
      if (this.state !== 'ready' || !this.lastHeartbeatAt) return;
      const silent = Date.now() - this.lastHeartbeatAt;
      if (silent > limit) {
        this.logger.error('supervisor: heartbeat lost', { silent, limit });
        this._fail(
          'heartbeat-lost',
          Object.assign(new Error(`no heartbeat for ${silent}ms (limit ${limit}ms)`), { code: 'E_TIMEOUT' }),
        );
      }
    }, interval);
    this.timers.watchdog.unref?.();
  }

  _onStderr(chunk) {
    const text = chunk.toString('utf8');
    for (const raw of text.split(/\r?\n/)) {
      const line = raw.length > this.maxStderrChars ? `${raw.slice(0, this.maxStderrChars)}…` : raw;
      if (!line) continue;
      this.stderrLines += 1;
      this.stderrRing.push(line);
      if (this.stderrRing.length > this.maxStderrLines) this.stderrRing.shift();
      this.emit('stderr', line);
      this.logger.debug('daemon/stderr', line);
    }
  }

  _protocolError(code, message, fatal) {
    const payload = { code, message, fatal: Boolean(fatal) };
    this.logger.warn('supervisor: protocol error', payload);
    this.emit('protocol-error', payload);
    if (fatal) this._fail('protocol-fatal', Object.assign(new Error(message), { code }));
  }

  _fail(kind, error) {
    if (this.stopping) return;
    this.lastError = { kind, message: error?.message ?? String(error), code: error?.code, at: Date.now() };
    this.logger.error('supervisor: failure', { kind, message: this.lastError.message });
    this.emit('failure', this.lastError);

    this._clearTimer('ready');
    this._clearTimer('watchdog');

    if (this.state === 'circuit-open') return;

    const now = Date.now();
    this.failures = this.failures.filter((at) => now - at <= this.breakerWindowMs);
    this.failures.push(now);

    // A hung child is still a live process: kill it before scheduling a restart.
    // The exit that follows is ours, so mark it expected — otherwise it would
    // be counted as a second failure and the breaker would trip early.
    const child = this.child;
    if (child && child.exitCode === null) {
      this.expectingExit = true;
      this._killTree(child, `fail:${kind}`);
    }

    if (this.failures.length >= this.breakerMaxFailures) {
      this._clearTimer('restart');
      this.state = 'circuit-open';
      this.emit('state', this.state);
      const breakerError = Object.assign(
        new Error(
          `circuit-open after ${this.failures.length} failures in ${this.breakerWindowMs}ms; ` +
            'vision is disabled until restart() is called',
        ),
        { code: 'E_UNAVAILABLE' },
      );
      this.emit('breaker', breakerError);
      this._settleStart(breakerError);
      this.logger.error('supervisor: breaker open', { failures: this.failures.length });
      return;
    }

    this.state = 'restarting';
    this.emit('state', this.state);
    this._settleStart(
      Object.assign(new Error(`daemon failed before READY: ${this.lastError.message}`), {
        code: error?.code ?? 'E_UNAVAILABLE',
      }),
    );
    this._scheduleRestart();
  }

  _scheduleRestart() {
    this._clearTimer('restart');
    this.restartAttempt += 1;
    const backoff = Math.min(
      this.restartBackoffMinMs * 2 ** (this.restartAttempt - 1),
      this.restartBackoffMaxMs,
    );
    this.logger.warn('supervisor: scheduling restart', { attempt: this.restartAttempt, backoff });
    this.timers.restart = setTimeout(() => {
      this.timers.restart = null;
      if (this.stopping || this.state === 'circuit-open') return;
      this.restarts += 1;
      this.state = 'starting';
      this.emit('state', this.state);
      this._spawn();
    }, backoff);
    // Intentionally NOT unref'd: "waiting to restart" is real pending work.
  }

  async _stop(reason) {
    this.stopping = true;
    this._clearTimer('ready');
    this._clearTimer('watchdog');
    this._clearTimer('restart');
    const child = this.child;
    if (hasExited(child)) {
      this._rejectPending('E_SHUTDOWN', 'daemon stopped');
      this.state = 'stopped';
      this.emit('state', this.state);
      this.stopping = false;
      return;
    }

    this.state = 'stopping';
    this.emit('state', this.state);
    try {
      this._write(C.shutdownFrame({ seq: this.hostSeq++, reason }));
    } catch {
      // Already gone; the kill path below still cleans up.
    }

    let exited = await this._waitForExit(child, this.shutdownGraceMs);
    if (!exited) {
      this.logger.warn('supervisor: shutdown grace expired, terminating', { pid: child.pid });
      await this._killTree(child, reason);
      exited = await this._waitForExit(child, this.termGraceMs);
    }
    if (!exited) {
      this.logger.error('supervisor: child survived kill', { pid: child.pid });
      const error = new Error(`daemon ${child.pid} survived SIGTERM/taskkill`);
      // 'error' without a listener would throw inside an async teardown and
      // skip the remaining cleanup; only emit when somebody is listening.
      if (this.listenerCount('error') > 0) this.emit('error', error);
      else this.emit('fatal', error);
    }

    this._rejectPending('E_SHUTDOWN', 'daemon stopped');
    this.child = null;
    this.pid = null;
    this.readyInfo = null;
    this.state = 'stopped';
    this.emit('state', this.state);
    this.stopping = false;
  }

  _rejectPending(code, message) {
    for (const [cmdId, entry] of this.pending) {
      clearTimeout(entry.timer);
      entry.reject(Object.assign(new Error(message), { code, cmdId, op: entry.op }));
    }
    this.pending.clear();
  }

  _waitForExit(child, timeoutMs) {
    if (hasExited(child)) return Promise.resolve(true);
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.exitWaiters.delete(onExit);
        resolve(false);
      }, timeoutMs);
      timer.unref?.();
      const onExit = () => {
        clearTimeout(timer);
        this.exitWaiters.delete(onExit);
        resolve(true);
      };
      this.exitWaiters.add(onExit);
      child.once('exit', onExit);
    });
  }

  async _killTree(child, reason) {
    if (hasExited(child)) return;
    try {
      child.kill('SIGTERM');
    } catch {
      // fall through to the OS-level kill
    }
    const exited = await this._waitForExit(child, this.termGraceMs);
    if (exited || hasExited(child)) return;
    this.logger.warn('supervisor: forcing kill tree', { pid: child.pid, reason });
    try {
      if (process.platform === 'win32') {
        spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], {
          windowsHide: true,
          timeout: 5000,
        });
      } else {
        child.kill('SIGKILL');
      }
    } catch (error) {
      this.logger.error('supervisor: kill tree failed', error.message);
    }
    await this._waitForExit(child, 1000);
  }

  _onExit(child, code, signal) {
    // Stale exit from a child we already replaced: ignore it completely.
    if (this.child !== child) {
      this.logger.debug('supervisor: ignoring stale exit', { pid: child.pid, code, signal });
      return;
    }
    const wasStopping = this.stopping;
    const wasExpected = this.expectingExit;
    this.expectingExit = false;
    this.child = null;
    this.pid = null;
    this.readyInfo = null;
    this._clearTimer('ready');
    this._clearTimer('watchdog');
    this.exitInfo = { code, signal, at: Date.now() };
    for (const waiter of this.exitWaiters) waiter();
    this.exitWaiters.clear();
    this._rejectPending('E_SHUTDOWN', `daemon exited (code=${code} signal=${signal})`);
    this.emit('exit', this.exitInfo);
    this.logger.warn('supervisor: child exited', { code, signal, stopping: wasStopping, expected: wasExpected });

    if (wasStopping) {
      this.state = 'stopped';
      this.emit('state', this.state);
      return;
    }
    if (this.state === 'circuit-open') return;
    // The exit we are looking at was caused by our own failure cleanup; a
    // restart is already scheduled and must not be counted twice.
    if (wasExpected) return;
    this._fail(
      'exit',
      Object.assign(new Error(`daemon exited (code=${code} signal=${signal})`), { code: 'E_UNAVAILABLE' }),
    );
  }

  _clearTimer(name) {
    const timer = this.timers[name];
    if (timer) {
      clearTimeout(timer);
      clearInterval(timer);
      this.timers[name] = null;
    }
  }
}
