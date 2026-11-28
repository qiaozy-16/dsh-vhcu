/**
 * @file phase13-tool-guard.test.mjs — the tools/execute script-write guard.
 *
 * Regression for the P1 defect: the host names parsed tool arguments
 * `exec.arguments`, not `exec.args`. Before the fix the middleware always saw
 * `undefined` and silently allowed every call. These cases drive the real
 * middleware through the mock context's `runTool` waterfall.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { apply } from '../../lib/index.js';
import { createMockCtx } from '../../dev/mock-ctx.mjs';

const suffix = `${process.pid}-${Date.now()}`;
process.env.VHCU_MACROS_PATH = join(tmpdir(), `vhcu-phase13-macros-${suffix}.json`);
process.env.VHCU_SETTINGS_PATH = join(tmpdir(), `vhcu-phase13-settings-${suffix}.json`);
process.env.VHCU_SESSION_STATE_PATH = join(tmpdir(), `vhcu-phase13-session-${suffix}.json`);
process.env.VHCU_DISABLE_USER_TAKEOVER = '1';

const silentLogger = { debug() {}, info() {}, warn() {}, error() {} };

function withPlugin(fn) {
  const mock = createMockCtx({ logger: silentLogger });
  apply(mock.ctx);
  return Promise.resolve()
    .then(() => fn(mock))
    .finally(() => {
      mock.dispose();
      for (const key of ['VHCU_MACROS_PATH', 'VHCU_SETTINGS_PATH', 'VHCU_SESSION_STATE_PATH']) {
        rmSync(process.env[key], { force: true });
      }
    });
}

test('phase13: shell call writing a vhcu helper script is denied via exec.arguments', async () => {
  await withPlugin(async ({ runTool }) => {
    const result = await runTool({
      name: 'ops_bash',
      arguments: { command: 'echo hello > vhcu-shot.py' },
    });
    assert.equal(result.isError, true);
    assert.equal(result.error?.info?.code, 'E_SCRIPT_WRITE_DENIED');
    assert.match(result.content?.[0]?.text ?? '', /E_SCRIPT_WRITE_DENIED/);
  });
});

test('phase13: ordinary script write is not narrowed by the guard', async () => {
  await withPlugin(async ({ runTool }) => {
    const result = await runTool({
      name: 'ops_bash',
      arguments: { command: 'echo hello > notes.py' },
    });
    assert.equal(result.isError, false);
    assert.equal(result.content?.[0]?.text, 'tool-allowed');
  });
});

test('phase13: non-shell tools are outside the guard', async () => {
  await withPlugin(async ({ runTool }) => {
    const result = await runTool({
      name: 'write',
      arguments: { path: 'vhcu-shot.py', content: 'print(1)' },
    });
    assert.equal(result.isError, false);
    assert.equal(result.content?.[0]?.text, 'tool-allowed');
  });
});

test('phase13: frame downloader via shell is denied', async () => {
  await withPlugin(async ({ runTool }) => {
    const result = await runTool({
      name: 'pwsh',
      arguments: {
        command: 'Invoke-WebRequest http://127.0.0.1:1/vhcu/frame?x=1 -OutFile shot.png',
      },
    });
    assert.equal(result.isError, true);
    assert.equal(result.error?.info?.code, 'E_SCRIPT_WRITE_DENIED');
  });
});

test('phase13: legacy exec.args shape still denies (backward-compatible fallback)', async () => {
  await withPlugin(async ({ runTool }) => {
    const result = await runTool({
      name: 'ops_bash',
      args: { command: 'echo hello > captcha.png.py' },
    });
    assert.equal(result.isError, true);
    assert.equal(result.error?.info?.code, 'E_SCRIPT_WRITE_DENIED');
  });
});

// The three Windows script extensions are interchangeable for the guard: a
// helper written as .ps1/.cmd/.bat is just as much "vision by script" as .py.
for (const ext of ['ps1', 'cmd', 'bat']) {
  test(`phase13: helper script write (.${ext}) is denied`, async () => {
    await withPlugin(async ({ runTool }) => {
      const result = await runTool({
        name: 'pwsh',
        arguments: { command: `Set-Content -Path vhcu-${ext}.${ext} -Value 'x'` },
      });
      assert.equal(result.isError, true);
      assert.equal(result.error?.info?.code, 'E_SCRIPT_WRITE_DENIED');
      assert.match(result.error?.info?.name ?? '', /ScriptWriteDenied/);
    });
  });
}

test('phase13: reading an existing vhcu script is not a write and passes', async () => {
  await withPlugin(async ({ runTool }) => {
    const result = await runTool({
      name: 'ops_bash',
      arguments: { command: 'Get-Content vhcu-old.ps1' },
    });
    assert.equal(result.isError, false);
    assert.equal(result.content?.[0]?.text, 'tool-allowed');
  });
});

test('phase13: nested argument objects are still scanned (collectText depth)', async () => {
  await withPlugin(async ({ runTool }) => {
    const result = await runTool({
      name: 'ops_bash',
      arguments: { opts: { nested: { command: 'python -c "x" > vhcu-deep.py' } } },
    });
    assert.equal(result.isError, true);
    assert.equal(result.error?.info?.code, 'E_SCRIPT_WRITE_DENIED');
  });
});
