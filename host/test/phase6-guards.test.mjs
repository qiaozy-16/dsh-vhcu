/**
 * @file phase6-guards.test.mjs — regression guards for the V1.1 final patches.
 *
 * P6.1 Web recovery buttons are clickable; P6.2 expired commands are refused;
 * P6.3 daemon PID records reclaim orphans left by a dead host.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const clientSource = readFileSync(join(ROOT, 'lib', 'client.js'), 'utf8');
const cliSource = readFileSync(join(ROOT, 'vision', 'vhcu_daemon', 'cli.py'), 'utf8');
const supervisorSource = readFileSync(join(ROOT, 'host', 'lib', 'supervisor.js'), 'utf8');

test('P6.1 Web recovery buttons are not disabled by IS_WEB', () => {
  assert.ok(!clientSource.includes('disabled: recoveryBusy || IS_WEB'));
  const recovery = clientSource.indexOf("resolveRecovery('resume')");
  const before = clientSource.slice(Math.max(0, recovery - 400), recovery);
  assert.match(before, /disabled: recoveryBusy,/);
  const discard = clientSource.indexOf("resolveRecovery('discard')");
  const beforeDiscard = clientSource.slice(Math.max(0, discard - 400), discard);
  assert.match(beforeDiscard, /disabled: recoveryBusy,/);
});

test('P6.2 deadline is checked before any command dispatch', () => {
  assert.match(cliSource, /def deadline_expired\(frame: dict, now_ms: int \| None = None\) -> bool:/);
  const dispatch = cliSource.indexOf('def _dispatch(self, frame: dict) -> bool:');
  const check = cliSource.indexOf('if deadline_expired(frame):', dispatch);
  const op = cliSource.indexOf('op = frame["op"]', dispatch);
  assert.ok(check > dispatch && op > check, 'deadline check must precede dispatch');
  assert.match(cliSource.slice(check, op), /E_TIMEOUT/);
  // The host must send an absolute epoch deadline, not a duration.
  assert.match(supervisorSource, /deadlineMs: Date\.now\(\) \+ timeoutMs/);
});

test('P6.3 daemon reclaims orphan PID records before binding', () => {
  const pidfile = join(ROOT, 'vision', 'vhcu_daemon', 'pidfile.py');
  assert.equal(existsSync(pidfile), true);
  assert.match(cliSource, /from \.pidfile import reclaim_orphans, remove_record, write_record/);
  const run = cliSource.indexOf('def run(self) -> int:');
  const reclaim = cliSource.indexOf('reclaim_orphans()', run);
  const write = cliSource.indexOf('write_record(', run);
  const bind = cliSource.indexOf('self.server.start()', run);
  assert.ok(reclaim > run && write > reclaim && bind > write, 'reclaim/write must precede bind');
  const shutdown = cliSource.indexOf('def shutdown(self, reason: str)');
  assert.ok(cliSource.indexOf('remove_record(', shutdown) > shutdown);
});
