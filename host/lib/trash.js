/**
 * @file trash.js — move files to the OS recycle bin. NEVER `unlink`.
 *
 * VHCU only ever trashes files it wrote itself (its own ledger), but a permanent
 * delete is still the wrong primitive: users must be able to restore, and a
 * plugin removing files under the user data directory looks like malware to
 * antivirus and to people. So this module has exactly one job:
 *
 *   1. Electron `shell.trashItem` (the host runs inside Electron).
 *   2. Windows fallback: .NET `FileSystem.DeleteFile(..., SendToRecycleBin)`.
 *   3. Otherwise fail loudly. Never delete permanently.
 *
 * Batching matters: a cold `powershell.exe` takes ~1s to load
 * `Microsoft.VisualBasic`, so one process per file turned a 7-file cleanup into
 * seven cold starts. `trashFiles` therefore tries Electron per file, then sends
 * the survivors through ONE PowerShell process per batch of at most 50 paths,
 * with a 3s per-batch timeout, a 10 ms pause between batches, and a total time
 * budget. Every batch is post-checked by the caller (`existsSync`), so a backend
 * that claims success without moving the file still counts as a failure.
 */

import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);

export const TRASH_UNAVAILABLE = 'E_TRASH_UNAVAILABLE';

/** One PowerShell process handles at most this many paths. */
export const TRASH_BATCH_SIZE = 50;
/** Per-batch deadline; the user asked for a hard 3s ceiling. */
export const TRASH_BATCH_TIMEOUT_MS = 3000;
/** Let the host event loop breathe between batches. */
export const TRASH_BATCH_PAUSE_MS = 10;
/** Whole cleanup budget; remaining files become failures instead of hanging. */
export const TRASH_TOTAL_BUDGET_MS = 30000;

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

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
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
 * One PowerShell invocation for a batch of paths. Each path is printed only when
 * the recycle-bin move itself returned without throwing; the caller still
 * verifies the file is gone.
 */
export function buildBatchScript(paths) {
  const list = paths.map((path) => `'${psQuote(path)}'`).join(', ');
  return [
    '$ErrorActionPreference = "Stop"',
    'Add-Type -AssemblyName Microsoft.VisualBasic',
    `$paths = @(${list})`,
    'foreach ($p in $paths) {',
    '  try {',
    '    if (Test-Path -LiteralPath $p) {',
    '      [System.IO.File]::SetAttributes($p, [System.IO.FileAttributes]::Normal)',
    "      [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($p, 'OnlyErrorDialogs', 'SendToRecycleBin')",
    '      Write-Output $p',
    '    }',
    '  } catch { }',
    '}',
  ].join('; ');
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
 *
 * Order: Electron per file (cheap, no child process), then one batched
 * PowerShell process per <=50 survivors. Verification is the caller's job.
 *
 * @returns {Promise<Array<{path: string, ok: boolean, via?: string, error?: string}>>}
 */
export async function trashFiles(paths = [], options = {}) {
  const list = (Array.isArray(paths) ? paths : []).map(String).filter(Boolean);
  if (list.length === 0) return [];

  const batchSize = Math.max(1, Math.min(TRASH_BATCH_SIZE, Number(options.batchSize) || TRASH_BATCH_SIZE));
  const timeoutMs = Number(options.timeoutMs) > 0 ? Number(options.timeoutMs) : TRASH_BATCH_TIMEOUT_MS;
  const pauseMs = Number.isFinite(Number(options.pauseMs))
    ? Math.max(0, Number(options.pauseMs))
    : TRASH_BATCH_PAUSE_MS;
  const totalBudgetMs = Number(options.totalBudgetMs) > 0
    ? Number(options.totalBudgetMs)
    : TRASH_TOTAL_BUDGET_MS;

  const started = Date.now();
  const results = [];
  const pending = [];
  const shell = electronShell();

  for (const path of list) {
    if (!shell) {
      pending.push(path);
      continue;
    }
    try {
      await shell.trashItem(path);
      results.push({ path, ok: true, via: 'electron' });
    } catch {
      // Fall back to the batched helper; the caller verifies the post-condition.
      pending.push(path);
    }
  }

  for (let index = 0; index < pending.length; index += batchSize) {
    if (Date.now() - started > totalBudgetMs) {
      for (const path of pending.slice(index)) {
        results.push({ path, ok: false, error: 'recycle-bin time budget exceeded' });
      }
      break;
    }
    const chunk = pending.slice(index, index + batchSize);
    if (process.platform !== 'win32') {
      for (const path of chunk) {
        results.push({ path, ok: false, error: 'no recycle-bin mechanism on this platform' });
      }
      continue;
    }
    try {
      const { stdout } = await runPowerShell(buildBatchScript(chunk), { timeoutMs });
      const confirmed = new Set(
        String(stdout)
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter(Boolean),
      );
      for (const path of chunk) {
        if (confirmed.has(path)) results.push({ path, ok: true, via: 'powershell' });
        else results.push({ path, ok: false, error: 'recycle-bin helper did not confirm this path' });
      }
    } catch (error) {
      for (const path of chunk) {
        results.push({ path, ok: false, error: error?.message ?? String(error) });
      }
    }
    if (index + batchSize < pending.length) await sleep(pauseMs);
  }

  return results;
}
