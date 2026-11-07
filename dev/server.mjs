#!/usr/bin/env node
/**
 * @file server.mjs — dev harness that mounts the REAL host plugin outside DSH.
 *
 * It serves the same four /vhcu routes and a standalone dark panel at /dev, so
 * the SSE stream and start/stop controls can be verified in a browser without
 * restarting the DSH desktop app. It is NOT part of the shipped plugin.
 *
 * Usage: node dev/server.mjs [--port 49380]
 */

import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { apply } from '../lib/index.js';
import { createMockCtx } from './mock-ctx.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));

function parseArgs(argv) {
  const args = { port: Number(process.env.VHCU_DEV_PORT ?? 49380) };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--port') args.port = Number(argv[++i]);
  }
  return args;
}

const logger = {
  debug: () => {},
  info: (...args) => console.log('[vhcu]', ...args),
  warn: (...args) => console.warn('[vhcu]', ...args),
  error: (...args) => console.error('[vhcu]', ...args),
};

const args = parseArgs(process.argv.slice(2));
const { ctx, routes, dispose } = createMockCtx({ logger });
apply(ctx);

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', 'http://127.0.0.1');
  if (url.pathname === '/dev' || url.pathname === '/') {
    try {
      const html = await readFile(join(HERE, 'index.html'));
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' });
      res.end(html);
    } catch (error) {
      res.writeHead(500, { 'content-type': 'text/plain' });
      res.end(`cannot read dev/index.html: ${error.message}`);
    }
    return;
  }
  const route = routes.get(url.pathname);
  if (!route) {
    res.writeHead(404, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ ok: false, error: 'not found' }));
    return;
  }
  try {
    await route.handler(req, res);
  } catch (error) {
    if (!res.headersSent) {
      res.writeHead(500, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: error.message }));
    } else {
      res.destroy?.();
    }
  }
});

server.listen(args.port, '127.0.0.1', () => {
  // P4: absolute frame_urls need a real origin outside DSH.
  ctx.webServer.setOrigin?.(`http://127.0.0.1:${args.port}`);
  console.log(`[vhcu] dev harness: http://127.0.0.1:${args.port}/dev`);
  console.log(`[vhcu] routes: ${[...routes.keys()].join(', ')}`);
});

let closing = false;
const shutdown = () => {
  if (closing) return;
  closing = true;
  dispose();
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 2000).unref();
};

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
