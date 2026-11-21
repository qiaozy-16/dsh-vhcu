/**
 * @file script-write-guard.js — P11: narrowly block VHCU-specific helper scripts.
 *
 * P10 blocked every .ps1/.cmd/.bat/.py write, which also blocked legitimate
 * user automation. P11 only denies when:
 *   - the written filename contains `vhcu-`, `captcha`, or `crop`, or
 *   - the command/content is a VHCU frame downloader
 *     (`Invoke-WebRequest` + `/vhcu/frame` + `-OutFile`).
 * Everything else — including ordinary `.py`/`.ps1` writes and `write`/`edit`
 * tools — passes through untouched.
 */

export const SCRIPT_WRITE_DENIED = 'E_SCRIPT_WRITE_DENIED';
export const SCRIPT_WRITE_MESSAGE =
  '禁止在工作区生成脚本文件，请直接使用 VHCU 视觉工具观测图像。';

const SHELL_TOOL = /^(?:vhcu_)?(?:ops_bash|bash|pwsh|powershell|shell|terminal_send|terminal_exec|run_command)$/i;
const SCRIPT_FILE = /[^\s"'<>|;&]+\.(?:ps1|cmd|bat|py)\b/gi;
const SUSPICIOUS_NAME = /(?:vhcu-|captcha|crop)/i;

// A write operation immediately before the filename. This keeps read-only
// references (`Get-Content vhcu-old.ps1`) and unrelated outputs out of scope.
const WRITE_BEFORE = new RegExp(
  [
    String.raw`>{1,2}\s*$`,
    String.raw`(?:set|add)-content\b[\s\S]*$`,
    String.raw`out-file\b[\s\S]*$`,
    String.raw`new-item\b[\s\S]*$`,
    String.raw`(?:copy-item|move-item|rename-item)\b[\s\S]*$`,
    String.raw`write(?:file|filesync|_text|text)\b[\s\S]*$`,
    String.raw`open\s*\([^)]*$`,
    String.raw`(?:^|\s)(?:-outfile|-o|-O)\s*$`,
    String.raw`\b(?:cp|copy|mv|move|ren|rename)\s+[^|;&]*$`,
    String.raw`tee-object\b[\s\S]*$`,
  ].join('|'),
  'i',
);

const IWR_FRAME_DOWNLOAD =
  /invoke-webrequest\b[\s\S]*?\/vhcu\/frame\b[\s\S]*?-?outfile\b/i;
const IWR_FRAME_OTHER_ORDER =
  /invoke-webrequest\b[\s\S]*?-?outfile\b[\s\S]*?\/vhcu\/frame\b/i;

function collectText(value, depth = 0) {
  if (depth > 4 || value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map((item) => collectText(item, depth + 1)).join('\n');
  if (typeof value === 'object') {
    return Object.values(value).map((item) => collectText(item, depth + 1)).join('\n');
  }
  return '';
}

function suspiciousWrittenFile(text) {
  for (const match of text.matchAll(SCRIPT_FILE)) {
    const token = match[0];
    const base = token.split(/[\\/]/).pop() ?? token;
    if (!SUSPICIOUS_NAME.test(base)) continue;
    const index = match.index ?? 0;
    const before = text.slice(Math.max(0, index - 120), index);
    if (WRITE_BEFORE.test(before)) return token;
  }
  return null;
}

/**
 * @returns {{code: string, message: string, tool: string, match: string}|null}
 */
export function detectScriptWrite(toolName, args) {
  const name = String(toolName ?? '');
  if (!SHELL_TOOL.test(name)) return null;
  const text = collectText(args);
  if (!text) return null;

  const file = suspiciousWrittenFile(text);
  if (file) {
    return {
      code: SCRIPT_WRITE_DENIED,
      message: SCRIPT_WRITE_MESSAGE,
      tool: name,
      match: file,
    };
  }

  if (IWR_FRAME_DOWNLOAD.test(text) || IWR_FRAME_OTHER_ORDER.test(text)) {
    return {
      code: SCRIPT_WRITE_DENIED,
      message: SCRIPT_WRITE_MESSAGE,
      tool: name,
      match: 'Invoke-WebRequest /vhcu/frame -OutFile',
    };
  }

  return null;
}

/** Build the denied tool result without invoking the shell body. */
export function deniedToolResult(detail) {
  return {
    content: [{ type: 'text', text: `Error: ${detail.message}（${detail.code}）` }],
    isError: true,
    error: {
      message: detail.message,
      info: { name: 'ScriptWriteDenied', code: detail.code },
    },
  };
}
