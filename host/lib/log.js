/**
 * @file log.js — tiny leveled logger for the host half.
 *
 * Output goes to stderr only: stdout of the CLI is reserved for the demo's
 * machine-readable summary line.
 */

const LEVELS = { debug: 10, info: 20, warn: 30, error: 40 };

export function createLogger({ level = 'info', sink = process.stderr, prefix = 'vhcu' } = {}) {
  const threshold = LEVELS[level] ?? LEVELS.info;
  const write = (levelName, message, extra) => {
    if (LEVELS[levelName] < threshold) return;
    const stamp = new Date().toISOString().slice(11, 23);
    const tail = extra === undefined ? '' : ` ${format(extra)}`;
    sink.write(`[${stamp}] ${prefix}/${levelName} ${message}${tail}\n`);
  };
  return {
    debug: (message, extra) => write('debug', message, extra),
    info: (message, extra) => write('info', message, extra),
    warn: (message, extra) => write('warn', message, extra),
    error: (message, extra) => write('error', message, extra),
    child: (name) => createLogger({ level, sink, prefix: `${prefix}:${name}` }),
  };
}

function format(value) {
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
