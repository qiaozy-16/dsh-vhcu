/**
 * @file paths.js — absolute runtime resource paths shared with Python.
 *
 * The Node host is the only component that knows the install directory and the
 * user's data directory. It resolves every path once and injects the absolute
 * result into the daemon environment; Python never resolves a relative path
 * against the process cwd (important for PyInstaller and future app bundles).
 */

import { homedir } from 'node:os';
import { isAbsolute, join, resolve } from 'node:path';

function absolute(value, name) {
  const text = String(value ?? '').trim();
  if (!text) throw new Error(`${name} must not be empty`);
  if (!isAbsolute(text)) {
    throw Object.assign(new Error(`${name} must be absolute, got ${text}`), {
      code: 'E_CONFIG',
      hint: 'VHCU resource paths are resolved by the host; relative values are rejected.',
    });
  }
  return resolve(text);
}

function dataRoot(env) {
  return env.LOCALAPPDATA || env.XDG_DATA_HOME || join(homedir(), '.local', 'share');
}

/**
 * Resolve the three absolute resource paths passed to the Python daemon.
 * Defaults preserve the pre-P1 Windows locations exactly.
 *
 * @param {{root: string, env?: Record<string,string|undefined>, data?: string}} options
 * @returns {{models: string, macros: string, logs: string}}
 */
export function resolveRuntimePaths({ root, env = process.env, data } = {}) {
  if (!root) throw new TypeError('resolveRuntimePaths requires root');
  const base = resolve(root);
  const dataDir = data ? resolve(data) : resolve(dataRoot(env));
  return Object.freeze({
    models: env.VHCU_MODEL_PATH
      ? absolute(env.VHCU_MODEL_PATH, 'VHCU_MODEL_PATH')
      : join(base, 'models'),
    macros: env.VHCU_MACROS_PATH
      ? absolute(env.VHCU_MACROS_PATH, 'VHCU_MACROS_PATH')
      : join(dataDir, 'dsh-vhcu', 'macros.json'),
    logs: env.VHCU_LOG_DIR
      ? absolute(env.VHCU_LOG_DIR, 'VHCU_LOG_DIR')
      : join(dataDir, 'dsh-vhcu', 'logs'),
  });
}

export const RESOURCE_ENV = Object.freeze({
  models: 'VHCU_MODEL_PATH',
  macros: 'VHCU_MACROS_PATH',
  logs: 'VHCU_LOG_DIR',
});

export function resourceEnv(paths) {
  return {
    VHCU_MODEL_PATH: paths.models,
    VHCU_MACROS_PATH: paths.macros,
    VHCU_LOG_DIR: paths.logs,
  };
}

export default resolveRuntimePaths;
