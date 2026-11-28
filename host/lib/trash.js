/**
 * @file trash.js — move files to the OS recycle bin. NEVER `unlink`.
 *
 * VHCU only ever trashes files it wrote itself (its own ledger), but a permanent
 * delete is still the wrong primitive: users must be able to restore, and a
 * plugin removing files under the user data directory looks like malware to
 * antivirus and to people. So this module has exactly one job:
 *
 *   1. Electron `shell.trashItem` (the host runs inside Electron; `dsh-pet`
 *      already requires `electron` from a host plugin, so this is available in
 *      the normal desktop deployment).
 *   2. Windows fallback: .NET `FileSystem.DeleteFile(..., SendToRecycleBin)`.
 *   3. Otherwise fail loudly. Never delete permanently.
 */

import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);

export const TRASH_UNAVAILABLE = 'E_TRASH_UNAVAILABLE';

function electronShell() {
  try {
    const electron = require('electron');
    const shell = electron?.shell ?? electron?.default?.shell;
    if (shell && typeof shell.trashItem === 'function') return shell;
  } catch {
    // Not running inside Electron (dev harness, tests) — fall through.
  }
  return null;
}

function runPowerShell(script, { timeoutMs }) {
  return new Promise((resolve, reject) => {
    const child = spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString('utf8');
    });
    child.stderr.on('data', (chunk) => {
      stderr += chunk.toString('utf8');
    });
    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {
        // ignore
      }
      reject(Object.assign(new Error('recycle-bin helper timed out'), { code: 'E_TIMEOUT' }));
    }, timeoutMs);
    child.on('error', (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      if (code === 0) resolve({ stdout, stderr });
      else reject(new Error(`recycle-bin helper exited ${code}: ${stderr.trim().slice(0, 300)}`));
    });
  });
}

/** Escape one path for a single-quoted PowerShell string literal. */
function psQuote(value) {
  return String(value).replace(/'/g, "''");
}

/**
 * Move one file to the recycle bin.
 * @returns {Promise<{ok: true, via: 'electron'|'powershell'}>}
 * @throws when no recycle-bin mechanism is available; callers must NOT fall back
 *         to a permanent delete.
 */
export async function trashFile(filePath, { timeoutMs = 15000 } = {}) {
  const path = String(filePath ?? '');
  if (!path) throw Object.assign(new Error('trashFile requires a path'), { code: 'E_PROTO' });

  const shell = electronShell();
  if (shell) {
    try {
      await shell.trashItem(path);
      return { ok: true, via: 'electron' };
    } catch (error) {
      // Fall through to the shell helper; keep the original reason for the report.
      var electronError = error;
    }
  }

  if (process.platform === 'win32') {
    const script = [
      '$ErrorActionPreference = "Stop"',
      'Add-Type -AssemblyName Microsoft.VisualBasic',
      `[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile('${psQuote(path)}','OnlyErrorDialogs','SendToRecycleBin')`,
      'Write-Output "ok"',
    ].join('; ');
    try {
      await runPowerShell(script, { timeoutMs });
      return { ok: true, via: 'powershell' };
    } catch (error) {
      throw Object.assign(
        new Error(
          `cannot move to recycle bin: ${error?.message ?? error}` +
            (typeof electronError !== 'undefined' ? ` (electron: ${electronError?.message ?? electronError})` : ''),
        ),
        { code: TRASH_UNAVAILABLE, cause: error },
      );
    }
  }

  throw Object.assign(
    new Error('no recycle-bin mechanism on this platform; refusing to delete permanently'),
    { code: TRASH_UNAVAILABLE },
  );
}

/**
 * Trash a batch, isolating per-file failures.
 * @returns {Promise<Array<{path: string, ok: boolean, via?: string, error?: string}>>}
 */
export async function trashFiles(paths = [], options = {}) {
  const results = [];
  for (const path of paths) {
    try {
      const done = await trashFile(path, options);
      results.push({ path, ok: true, via: done.via });
    } catch (error) {
      results.push({ path, ok: false, error: error?.message ?? String(error) });
    }
  }
  return results;
}
