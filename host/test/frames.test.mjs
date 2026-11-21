/**
 * @file frames.test.mjs — image-plane client contract.
 *
 * P1 8.7: both the GET and HEAD paths must forward `seq`, otherwise a signed
 * full-frame URL silently falls back to whatever frame is latest.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';

import { fetchFrame, fetchFrameMeta } from '../lib/frames.js';

const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
);

test('P1 8.7 frame clients forward seq to the image plane', async () => {
  const seen = [];
  const server = http.createServer((req, res) => {
    seen.push({ method: req.method, url: req.url ?? '' });
    if (req.method === 'HEAD') {
      res.writeHead(200, {
        'x-frame-size': '10x10',
        'x-frame-origin': '0,0',
        'content-length': '0',
      });
      res.end();
      return;
    }
    res.writeHead(200, { 'content-type': 'image/png', 'content-length': String(PNG.length) });
    res.end(PNG);
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const { port } = server.address();
    const image = await fetchFrame({ port, token: 'tok', x: 0, y: 0, w: 10, h: 10, maxWidth: 10, seq: 42 });
    assert.equal(image.status, 200);
    const meta = await fetchFrameMeta({ port, token: 'tok', x: 0, y: 0, w: 10, h: 10, maxWidth: 10, seq: 7 });
    assert.equal(meta.size, '10x10');

    assert.equal(seen.length, 2);
    assert.equal(seen[0].method, 'GET');
    assert.match(seen[0].url, /(?:[?&])seq=42(?:&|$)/);
    assert.equal(seen[1].method, 'HEAD');
    assert.match(seen[1].url, /(?:[?&])seq=7(?:&|$)/);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});
