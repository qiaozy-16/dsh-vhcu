/**
 * @file python.js — locate a usable Python 3 interpreter, loudly.
 *
 * D5 = "user-configured venv first". We never silently fall back to a broken
 * interpreter: a wrong python is the single most common "daemon won't start".
 */

import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);

const CANDIDATES = [
  { cmd: 'py', args: ['-3'] },
  { cmd: 'python', args: [] },
  { cmd: 'python3', args: [] },
];

/**
 * @param {{pythonPath?: string, timeoutMs?: number}} [options]
 * @returns {Promise<{cmd: string, args: string[], version: string,
 *                    executable: string, source: string}>}
 */
export async function resolvePython(options = {}) {
  const timeoutMs = options.timeoutMs ?? 4000;
  const explicit = options.pythonPath ?? process.env.VHCU_PYTHON ?? '';
  const candidates = explicit
    ? [{ cmd: explicit, args: [], source: 'explicit' }]
    : CANDIDATES.map((candidate) => ({ ...candidate, source: 'auto' }));

  const failures = [];
  for (const candidate of candidates) {
    try {
      const { stdout } = await execFileAsync(candidate.cmd, [...candidate.args, '--version'], {
        timeout: timeoutMs,
        windowsHide: true,
      });
      const version = String(stdout).trim();
      if (!/^Python 3\./.test(version)) {
        failures.push(`${candidate.cmd}: unexpected version "${version}"`);
        continue;
      }
      const probe = await execFileAsync(
        candidate.cmd,
        [...candidate.args, '-c', 'import sys;print(sys.executable)'],
        { timeout: timeoutMs, windowsHide: true },
      );
      return {
        cmd: candidate.cmd,
        args: candidate.args,
        version,
        executable: String(probe.stdout).trim(),
        source: candidate.source,
      };
    } catch (error) {
      failures.push(`${candidate.cmd}: ${error.code ?? error.message}`);
    }
  }

  const error = new Error(
    `no usable Python 3 found (tried ${candidates.map((c) => c.cmd).join(', ')}). ` +
      'Set VHCU_PYTHON to your venv python.exe. Details: ' +
      failures.join(' | '),
  );
  error.code = 'E_UNAVAILABLE';
  throw error;
}

/** Same, synchronously — only for CLI startup where blocking is acceptable. */
export function resolvePythonSync() {
  // Kept intentionally unused by the supervisor: async probing there avoids
  // blocking the host event loop while the plugin tree boots.
  throw new Error('not implemented; use resolvePython()');
}
