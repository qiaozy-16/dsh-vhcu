#!/usr/bin/env node
/**
 * @file install.mjs — mount dsh-vhcu into a DSH profile, atomically.
 *
 * What it does (and nothing else):
 *   1. node_modules/dsh-vhcu -> this repo           (directory junction)
 *   2. profile package.json dependencies["dsh-vhcu"] = file:<repo>
 *   3. profile dsh.profile.bundles += "dsh-vhcu"
 *
 * Safety:
 *   - package.json is backed up once (package.json.bak, overwritten each run)
 *   - write is temp-file + rename, then re-parsed as a self-check
 *   - --dry-run prints the plan without touching anything
 *   - --uninstall reverses all three steps
 *
 * DSH reads profile bundles at startup only, so a restart is required.
 */

import {
  copyFileSync,
  existsSync,
  lstatSync,
  readFileSync,
  renameSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, '..');
const PACKAGE = 'dsh-vhcu';

function parseArgs(argv) {
  const args = { profile: 'desktop', dryRun: false, uninstall: false, force: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--profile') args.profile = argv[++i];
    else if (arg === '--dry-run') args.dryRun = true;
    else if (arg === '--uninstall') args.uninstall = true;
    else if (arg === '--force') args.force = true;
    else if (arg === '--help' || arg === '-h') {
      console.log('usage: node scripts/install.mjs [--profile desktop] [--dry-run] [--uninstall] [--force]');
      process.exit(0);
    } else {
      throw new Error(`unknown argument ${arg}`);
    }
  }
  return args;
}

function fail(message) {
  console.error(`[vhcu-install] ERROR ${message}`);
  process.exit(1);
}

function readJson(path) {
  return JSON.parse(readFileSync(path, 'utf8'));
}

function writeJsonAtomic(path, value) {
  const backup = `${path}.bak`;
  copyFileSync(path, backup);
  const temp = `${path}.tmp-${process.pid}`;
  writeFileSync(temp, `${JSON.stringify(value, null, 2)}\n`, 'utf8');
  renameSync(temp, path);
  const check = readJson(path);
  if (JSON.stringify(check) !== JSON.stringify(value)) {
    fail('post-write self-check failed; profile package.json is unchanged? (backup kept)');
  }
  return backup;
}

function isLinkTo(path, target) {
  try {
    const stat = lstatSync(path);
    if (!stat.isSymbolicLink()) return false;
    return realpathSync(path).toLowerCase() === realpathSync(target).toLowerCase();
  } catch {
    return false;
  }
}

function createJunction(link, target) {
  // Node creates a real NTFS junction with type 'junction'; it needs no admin
  // rights and no shell (pwsh is not guaranteed on PATH for child processes).
  symlinkSync(target, link, 'junction');
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const dshHome = process.env.DSH_HOME || join(homedir(), '.dsh');
  const profileDir = join(dshHome, 'profiles', args.profile);
  const pkgPath = join(profileDir, 'package.json');
  const linkPath = join(profileDir, 'node_modules', PACKAGE);

  if (!existsSync(pkgPath)) fail(`profile not found: ${pkgPath}`);
  const pkg = readJson(pkgPath);
  const bundles = pkg?.dsh?.profile?.bundles;
  if (!Array.isArray(bundles)) fail(`profile has no dsh.profile.bundles array: ${pkgPath}`);

  const hasBundle = bundles.includes(PACKAGE);
  const hasDep = Boolean(pkg.dependencies?.[PACKAGE]);
  const linked = isLinkTo(linkPath, REPO);
  const linkExists = existsSync(linkPath);

  console.log(`[vhcu-install] profile : ${profileDir}`);
  console.log(`[vhcu-install] repo    : ${REPO}`);
  console.log(`[vhcu-install] actions :`);
  if (args.uninstall) {
    console.log(`  - remove bundle entry      ${hasBundle ? 'yes' : 'already absent'}`);
    console.log(`  - remove dependency entry  ${hasDep ? 'yes' : 'already absent'}`);
    console.log(`  - remove junction          ${linked ? 'yes' : linkExists ? 'exists but not ours (kept)' : 'already absent'}`);
  } else {
    console.log(`  - junction ${linkPath} -> ${REPO}  ${linked ? '(already linked)' : linkExists ? '(EXISTS, need --force)' : ''}`);
    console.log(`  - dependency ${PACKAGE} = file:${REPO}  ${hasDep ? '(already present)' : ''}`);
    console.log(`  - bundle entry ${PACKAGE}  ${hasBundle ? '(already present)' : ''}`);
  }

  if (args.dryRun) {
    console.log('[vhcu-install] dry-run: nothing written');
    return;
  }

  if (args.uninstall) {
    if (linked) rmSync(linkPath, { recursive: true, force: true });
    else if (linkExists) console.warn('[vhcu-install] junction exists but points elsewhere; left untouched');
    if (hasBundle) pkg.dsh.profile.bundles = bundles.filter((entry) => entry !== PACKAGE);
    if (hasDep) delete pkg.dependencies[PACKAGE];
    if (hasBundle || hasDep) {
      const backup = writeJsonAtomic(pkgPath, pkg);
      console.log(`[vhcu-install] profile updated (backup: ${backup})`);
    }
    console.log('[vhcu-install] unmounted. Restart DSH to take effect.');
    return;
  }

  if (linkExists && !linked) {
    if (!args.force) fail(`junction exists and points elsewhere: ${linkPath} (use --force to replace)`);
    rmSync(linkPath, { recursive: true, force: true });
  }
  if (!linked) {
    createJunction(linkPath, REPO);
    console.log('[vhcu-install] junction created');
  }

  pkg.dependencies = pkg.dependencies ?? {};
  pkg.dependencies[PACKAGE] = `file:${REPO.replace(/\\/g, '/')}`;
  if (!hasBundle) pkg.dsh.profile.bundles.push(PACKAGE);
  const backup = writeJsonAtomic(pkgPath, pkg);
  console.log(`[vhcu-install] profile updated (backup: ${backup})`);
  console.log('[vhcu-install] done.');
  console.log('[vhcu-install] NEXT: fully restart the DSH desktop app, then open the sidebar + menu → "VHCU 视觉".');
  console.log('[vhcu-install] verify host logs for: dsh-vhcu: mounted (routes /vhcu/state|events|control|frame)');
  console.log(`[vhcu-install] rollback: node scripts/install.mjs --uninstall`);
}

main();
