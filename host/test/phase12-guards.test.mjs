import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { Supervisor } from '../lib/supervisor.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (...parts) => readFileSync(join(ROOT, ...parts), 'utf8');

test('P12 CON-01 forceKill never taskkills a remembered PID', () => {
  const source = read('lib', 'index.js');
  assert.doesNotMatch(source, /taskkill['"],\s*\[[^\]]*'\/PID'/);
  assert.match(source, /expectedStartedAt/);
  const supervisor = new Supervisor({ python: { cmd: 'x' }, visionPath: ROOT });
  supervisor.childStartedAt = 10;
  return supervisor.stop('test', { expectedStartedAt: 20 }).then((result) => {
    assert.equal(result.reason, 'child-identity-mismatch');
  });
});

test('P12 SEC-01 frame route refuses while privacy is blocked', () => {
  const source = read('lib', 'index.js');
  const start = source.indexOf("path: '/vhcu/frame'");
  const end = source.indexOf("path: '/vhcu/control'", start);
  const body = source.slice(start, end);
  assert.match(body, /state\.privacy\?\.blocked/);
  assert.match(body, /sendJson\(res, 403/);
});

test('P12 UI-01 settings draft is dirty-protected', () => {
  const source = read('lib', 'client.js');
  assert.match(source, /const \[isDirty, setIsDirty\] = useState\(false\)/);
  assert.match(source, /if \(!isDirty\) setDraft\(settings\)/);
});

test('P12 SEC-03 input has a daemon-side privacy gate', () => {
  const input = read('vision', 'vhcu_daemon', 'input.py');
  assert.match(input, /def assert_privacy/);
  assert.match(input, /self\._privacy_check/);
  const cli = read('vision', 'vhcu_daemon', 'cli.py');
  assert.match(cli, /set_privacy_check\(self\._input_privacy_blocked\)/);
  assert.match(cli, /guard\(hwnd\)/);
});

test('B2.1 explicit privacy-off choice is propagated to Node and Python', () => {
  const host = read('lib', 'index.js');
  assert.match(host, /guardedArgs\.allowUnprotected = true/);
  assert.match(host, /allowUnprotected: Boolean\(settings\.privacyUnprotected\)/);
  const cli = read('vision', 'vhcu_daemon', 'cli.py');
  assert.match(
    cli,
    /def _assert_uia_privacy\(self, hwnd: int, allow_unprotected: bool = False\)/,
  );
  assert.match(cli, /if allow_unprotected:\s*\n\s*return/);
  assert.match(cli, /allow_unprotected=allow_unprotected/);
  const input = read('vision', 'vhcu_daemon', 'input.py');
  assert.match(
    input,
    /def assert_privacy\(self, hwnd: int = 0, allow_unprotected: bool = False\)/,
  );
});

test('P12 CON-03 async UIA acks use cmd_id, not loop frame', () => {
  const source = read('vision', 'vhcu_daemon', 'cli.py');
  assert.match(source, /_safe_ack_error\(cmd_id/);
  assert.doesNotMatch(
    source,
    /except UiaError as error:\s*\n\s*self\._safe_ack_error\(frame/,
  );
});

test('P12 CON-02 sources no longer own frame_seq', () => {
  const source = read('vision', 'vhcu_daemon', 'sources.py');
  assert.doesNotMatch(source, /self\._seq/);
  assert.match(source, /frame_seq=0/);
  const runner = read('vision', 'vhcu_daemon', 'runner.py');
  assert.match(runner, /with self\._lock:\s*\n\s*self\._capture_seq \+= 1/);
});

test('P12 RUN-02 capture loop closes the source on fatal errors', () => {
  const runner = read('vision', 'vhcu_daemon', 'runner.py');
  const start = runner.indexOf('if consecutive_errors >= 3');
  const body = runner.slice(start, start + 700);
  assert.match(body, /source = self\._source/);
  assert.match(body, /source\.close\(\)/);
});

test('P12 WIN-01 PostMessage failures release a queued button', () => {
  const source = read('vision', 'vhcu_daemon', 'virtual_cursor.py');
  assert.match(source, /down_sent/);
  assert.match(source, /PostMessageW\(int\(hwnd\), int\(active_up\)/);
  assert.match(source, /PostMessageW\(int\(hwnd\), int\(up_msg\)/);
});
