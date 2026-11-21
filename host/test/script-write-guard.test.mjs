import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  SCRIPT_WRITE_DENIED,
  detectScriptWrite,
  deniedToolResult,
} from '../lib/script-write-guard.js';

test('P11 blocks only VHCU/captcha/crop script filenames', () => {
  const cases = [
    ['ops_bash', { command: "Set-Content -Path vhcu-captcha-1.ps1 -Value 'x'" }],
    ['pwsh', { command: 'echo pixel > captcha.bat' }],
    ['ops_bash', { command: 'curl -o crop.py https://example.test/x' }],
    ['vhcu_ops_bash', { command: 'New-Item -ItemType File vhcu-cluster.py' }],
  ];
  for (const [tool, args] of cases) {
    const detail = detectScriptWrite(tool, args);
    assert.ok(detail, `${tool} should be blocked: ${JSON.stringify(args)}`);
    assert.equal(detail.code, SCRIPT_WRITE_DENIED);
    assert.match(detail.message, /禁止在工作区生成脚本文件/);
  }
});

test('P11 blocks a frame-download script even with a normal filename', () => {
  const command =
    'Invoke-WebRequest -Uri "http://127.0.0.1:1234/vhcu/frame?sig=x" -OutFile native.png';
  const detail = detectScriptWrite('pwsh', { command });
  assert.ok(detail);
  assert.equal(detail.code, SCRIPT_WRITE_DENIED);
  assert.match(detail.match, /Invoke-WebRequest/);
});

test('P11 allows normal development scripts and unmarked downloads', () => {
  const allowed = [
    ['ops_bash', { command: 'python existing.py --flag' }],
    ['ops_bash', { command: 'echo result > report.py' }],
    ['pwsh', { command: 'Set-Content -Path notes.ps1 -Value "hello"' }],
    ['ops_bash', { command: "python -c \"open('cluster.py','w').write('x')\"" }],
    ['pwsh', { command: 'Get-Content vhcu-captcha-1.ps1' }],
    ['pwsh', { command: 'Invoke-WebRequest https://example.test/data -OutFile data.zip' }],
  ];
  for (const [tool, args] of allowed) {
    assert.equal(
      detectScriptWrite(tool, args),
      null,
      `${tool} should be allowed: ${JSON.stringify(args)}`,
    );
  }
});

test('P11 never blocks write/edit tools, even for suspicious filenames', () => {
  assert.equal(
    detectScriptWrite('write', { file_path: 'vhcu-captcha-1.ps1', content: 'x' }),
    null,
  );
  assert.equal(
    detectScriptWrite('edit', { file_path: 'crop.ps1', old_string: 'a', new_string: 'b' }),
    null,
  );
});

test('P11 denial result carries the machine-readable code', () => {
  const detail = detectScriptWrite('ops_bash', { command: 'echo x > vhcu-a.ps1' });
  assert.ok(detail);
  const result = deniedToolResult(detail);
  assert.equal(result.isError, true);
  assert.equal(result.error.info.code, SCRIPT_WRITE_DENIED);
  assert.match(result.content[0].text, /E_SCRIPT_WRITE_DENIED/);
});
