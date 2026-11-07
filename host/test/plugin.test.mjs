import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { apply } from '../../lib/index.js';
import { createMockCtx } from '../../dev/mock-ctx.mjs';

// Keep macro persistence out of the real %LOCALAPPDATA% during tests.
process.env.VHCU_MACROS_PATH = join(tmpdir(), `vhcu-plugin-macros-${process.pid}.json`);
// Deterministic tests: do not let real desktop mouse/keyboard pause AI actions.
process.env.VHCU_DISABLE_USER_TAKEOVER = '1';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function startServer(routes) {
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url ?? '/', 'http://127.0.0.1');
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
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      resolve({ server, base: `http://127.0.0.1:${port}` });
    });
  });
}

async function post(base, path, body, headers = {}) {
  const response = await fetch(`${base}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-vhcu': '1', ...headers },
    body: JSON.stringify(body),
  });
  return { status: response.status, body: await response.json().catch(() => null) };
}

async function getState(base) {
  const response = await fetch(`${base}/vhcu/state`);
  const payload = await response.json();
  return payload.state;
}

async function approvePendingApproval(base, decision = 'allow') {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const state = await getState(base);
    if (state?.approval?.id) {
      return post(base, '/vhcu/approval', { id: state.approval.id, decision });
    }
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error('timed out waiting for approval request');
}

async function readEvents(base) {
  const response = await fetch(`${base}/vhcu/state`);
  const payload = await response.json();
  return Array.isArray(payload.events) ? payload.events : [];
}

async function waitFor(predicate, timeoutMs, label) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (await predicate()) return true;
    await sleep(100);
  }
  throw new Error(`timeout waiting for ${label}`);
}

/** Collect raw SSE frames from a streaming fetch into an array of {event, data}. */
function collectSse(response) {
  const frames = [];
  const waiters = [];
  let buffer = '';
  (async () => {
    try {
      for await (const chunk of response.body) {
        buffer += Buffer.from(chunk).toString('utf8');
        let index;
        while ((index = buffer.indexOf('\n\n')) !== -1) {
          const raw = buffer.slice(0, index);
          buffer = buffer.slice(index + 2);
          const frame = { event: 'message', data: '' };
          for (const line of raw.split('\n')) {
            if (line.startsWith('event: ')) frame.event = line.slice(7).trim();
            else if (line.startsWith('data: ')) frame.data += line.slice(6);
          }
          if (frame.data) {
            try {
              frame.json = JSON.parse(frame.data);
            } catch {
              // keep raw
            }
          }
          frames.push(frame);
          for (const waiter of waiters.splice(0)) waiter();
        }
      }
    } catch {
      // aborted
    }
  })();
  return { frames, waiters };
}

test('plugin mounts nine routes, registers twenty-four tools, and reports idle state', { timeout: 20000 }, async () => {
  const { ctx, routes, tools, dispose } = createMockCtx({ logger: { info() {}, warn() {}, error() {}, debug() {} } });
  apply(ctx);
  assert.deepEqual(
    [...routes.keys()].sort(),
    [
      '/vhcu/approval',
      '/vhcu/control',
      '/vhcu/emergency-stop',
      '/vhcu/events',
      '/vhcu/frame',
      '/vhcu/macros',
      '/vhcu/recording',
      '/vhcu/settings',
      '/vhcu/state',
    ].sort(),
  );
  assert.deepEqual([...tools.keys()].sort(), [
    'vhcu_get_active_window',
    'vhcu_get_full_frame',
    'vhcu_get_hear_events',
    'vhcu_get_memory_clipboard',
    'vhcu_get_multimodal_events',
    'vhcu_get_recommended_macros',
    'vhcu_get_vision_events',
    'vhcu_key_press',
    'vhcu_macro_open_notepad_and_type',
    'vhcu_macro_save_file',
    'vhcu_mouse_click',
    'vhcu_mouse_drag',
    'vhcu_mouse_middle_drag',
    'vhcu_mouse_scroll',
    'vhcu_run_macro',
    'vhcu_save_macro',
    'vhcu_set_memory_clipboard',
    'vhcu_start_hear',
    'vhcu_start_recording',
    'vhcu_start_vision',
    'vhcu_stop_hear',
    'vhcu_stop_recording',
    'vhcu_stop_vision',
    'vhcu_type_text',
  ]);
  for (const tool of tools.values()) {
    assert.ok(tool.description.length > 30, `${tool.name} needs a useful description`);
    assert.equal(tool.parameters.type, 'object');
    assert.equal(typeof tool.output.render, 'function');
  }
  assert.match(tools.get('vhcu_type_text').description, /输入法/);
  assert.match(tools.get('vhcu_type_text').description, /Ctrl\+空格/);
  assert.match(
    tools.get('vhcu_macro_open_notepad_and_type').description,
    /优先调用此宏，比单步操作快得多/,
  );
  const { server, base } = await startServer(routes);
  try {
    const state = await getState(base);
    assert.equal(state.phase, 'idle');
    assert.equal(state.running, false);
    assert.equal(state.interaction.mode, 'running');
    assert.equal(state.approval, null);

    const initial = await (await fetch(`${base}/vhcu/settings`)).json();
    assert.equal(initial.ok, true);
    assert.equal(initial.settings.imeShortcut, 'ctrl+space');
    assert.equal(initial.settings.hearSensitivity, 'normal');
    assert.equal(initial.settings.hearRawTranscripts, false);
    assert.equal(initial.settings.privacyUnprotected, false);
    assert.equal(initial.settings.autoApproveDestructive, false);
    assert.equal(initial.settings.turboMode, false);
    assert.ok(initial.settings.privacyBlacklist.includes('密码'));
    const updated = await post(base, '/vhcu/settings', {
      settings: {
        debug: true,
        imeShortcut: 'shift',
        hearSensitivity: 'sensitive',
        hearRawTranscripts: true,
        privacyUnprotected: true,
        autoApproveDestructive: true,
        turboMode: true,
        macros: { save: false },
        privacyBlacklist: [' 密码 ', 'password', 'PASSWORD', '', ' 银行 '],
      },
    });
    assert.equal(updated.status, 200, JSON.stringify(updated.body));
    assert.equal(updated.body.settings.debug, true);
    assert.equal(updated.body.settings.imeShortcut, 'shift');
    assert.equal(updated.body.settings.hearSensitivity, 'sensitive');
    assert.equal(updated.body.settings.hearRawTranscripts, true);
    assert.equal(updated.body.settings.privacyUnprotected, true);
    assert.equal(updated.body.settings.autoApproveDestructive, true);
    assert.equal(updated.body.settings.turboMode, true);
    assert.equal(updated.body.settings.macros.save, false);
    assert.equal(updated.body.settings.macros.open_notepad, true);
    assert.deepEqual(updated.body.settings.privacyBlacklist, ['密码', 'password', '银行']);
    const after = await (await fetch(`${base}/vhcu/settings`)).json();
    assert.equal(after.settings.imeShortcut, 'shift');
    assert.equal(after.settings.hearSensitivity, 'sensitive');
    assert.equal(after.settings.hearRawTranscripts, true);
    assert.equal(after.settings.privacyUnprotected, true);
    assert.equal(after.settings.autoApproveDestructive, true);
    assert.deepEqual(after.settings.privacyBlacklist, ['密码', 'password', '银行']);

    // Persistent macro store: seeded defaults, save, toggle, duplicate conflict.
    const seeded = await (await fetch(`${base}/vhcu/macros`)).json();
    assert.equal(seeded.ok, true);
    assert.ok(seeded.macros.some((macro) => macro.name === 'macro_launch_app'));
    assert.ok(seeded.categories.some((category) => category.name === '系统基础宏' && category.builtin));
    assert.ok(seeded.categories.some((category) => category.name === '我的自定义宏'));
    const saved = await post(base, '/vhcu/macros', {
      action: 'save',
      macro: { name: 'p8_smoke', description: 'test', steps: [{ op: 'wait', ms: 10 }] },
    });
    assert.equal(saved.status, 200, JSON.stringify(saved.body));
    assert.equal(saved.body.macro.name, 'p8_smoke');
    const toggled = await post(base, '/vhcu/macros', { action: 'toggle', name: 'p8_smoke', enabled: false });
    assert.equal(toggled.status, 200);
    const disabled = toggled.body.macros.find((macro) => macro.name === 'p8_smoke');
    assert.equal(disabled.enabled, false);
    const duplicate = await post(base, '/vhcu/macros', {
      action: 'save',
      macro: { name: 'p8_smoke', steps: [{ op: 'wait', ms: 10 }] },
    });
    assert.equal(duplicate.status, 400);
    assert.equal(duplicate.body.code, 'E_CONFLICT');

    const edited = await post(base, '/vhcu/macros', {
      action: 'update',
      name: 'p8_smoke',
      macro: { category: 'explorer', description: 'edited', steps: [{ op: 'wait', ms: 20 }] },
    });
    assert.equal(edited.status, 200, JSON.stringify(edited.body));
    const editedMacro = edited.body.macros.find((macro) => macro.name === 'p8_smoke');
    assert.equal(editedMacro.category, 'explorer');
    assert.equal(editedMacro.description, 'edited');
    assert.deepEqual(editedMacro.steps, [{ op: 'wait', ms: 20 }]);

    const removed = await post(base, '/vhcu/macros', { action: 'delete', name: 'p8_smoke' });
    assert.equal(removed.status, 200);
    assert.equal(removed.body.macros.some((macro) => macro.name === 'p8_smoke'), false);
  } finally {
    server.close();
    dispose();
  }
});

test('control rejects cross-origin and missing marker header', { timeout: 20000 }, async () => {
  const { ctx, routes, dispose } = createMockCtx({ logger: { info() {}, warn() {}, error() {}, debug() {} } });
  apply(ctx);
  const { server, base } = await startServer(routes);
  try {
    const cross = await post(base, '/vhcu/control', { action: 'start' }, { origin: 'http://evil.example' });
    assert.equal(cross.status, 403);
    const missing = await post(base, '/vhcu/control', { action: 'start' }, { 'x-vhcu': '' });
    assert.equal(missing.status, 403);
    const denied = await post(base, '/vhcu/control', { action: 'nuke' });
    assert.equal(denied.status, 400);
  } finally {
    server.close();
    dispose();
  }
});

test('start streams vision.diff over SSE, stop leaves no child', { timeout: 35000 }, async () => {
  const logs = [];
  const { ctx, routes, dispose } = createMockCtx({
    logger: { info: (...a) => logs.push(a.join(' ')), warn() {}, error() {}, debug() {} },
  });
  apply(ctx);
  const { server, base } = await startServer(routes);
  const controller = new AbortController();
  try {
    const eventsResponse = await fetch(`${base}/vhcu/events`, { signal: controller.signal });
    assert.equal(eventsResponse.status, 200);
    assert.match(eventsResponse.headers.get('content-type'), /text\/event-stream/);
    const sse = collectSse(eventsResponse);

    const started = await post(base, '/vhcu/control', { action: 'start' });
    assert.equal(started.status, 200, JSON.stringify(started.body));
    assert.equal(started.body.ok, true);

    await waitFor(async () => (await getState(base)).phase === 'running', 20000, 'running');
    const running = await getState(base);
    assert.ok(running.pid > 0);
    assert.equal(running.running, true);
    assert.ok(['mss', 'fake'].includes(running.source));

    await waitFor(
      () => sse.frames.some((frame) => frame.event === 'event' && /检测到画面变化/.test(frame.json?.text ?? '')),
      15000,
      'vision.diff event',
    );

    const dataFrames = sse.frames.filter((frame) => frame.event === 'event');
    assert.ok(dataFrames.length > 0, 'expected at least one data event');
    for (const frame of dataFrames) {
      assert.ok(['vision', 'action', 'hear'].includes(frame.json?.kind), `unexpected kind ${frame.json?.kind}`);
      assert.equal(typeof frame.json?.ts, 'number');
    }

    const beforeStop = await getState(base);
    const stopped = await post(base, '/vhcu/control', { action: 'stop' });
    assert.equal(stopped.status, 200);
    await waitFor(async () => (await getState(base)).phase === 'idle', 10000, 'idle');
    await waitFor(
      () => {
        try {
          process.kill(beforeStop.pid, 0);
          return false;
        } catch {
          return true;
        }
      },
      8000,
      'python child exit',
    );
  } finally {
    controller.abort();
    await new Promise((resolve) => server.close(resolve));
    dispose();
  }
});

test('P4 tools: signed frame_url + markdown, limit/since_id paging, stop kills child', { timeout: 60000 }, async () => {
  const { ctx, routes, tools, dispose } = createMockCtx({
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  });
  apply(ctx);
  const { server, base } = await startServer(routes);
  ctx.webServer.setOrigin(base);

  const start = tools.get('vhcu_start_vision');
  const getEvents = tools.get('vhcu_get_vision_events');
  const stop = tools.get('vhcu_stop_vision');
  assert.ok(start && getEvents && stop);

  try {
    const started = await start.execute({}, {});
    assert.equal(started.ok, true, JSON.stringify(started));
    assert.equal(started.fps, 1, 'tool start must use the low-power 1fps rate');
    assert.ok(started.pid > 0);

    // Calling start twice is safe and must not spawn a second daemon.
    const again = await start.execute({}, {});
    assert.equal(again.ok, true);
    assert.equal(again.pid, started.pid);

    let listed = [];
    const deadline = Date.now() + 20000;
    while (Date.now() < deadline) {
      const value = await getEvents.execute({ limit: 20 }, {});
      assert.equal(value.ok, true);
      listed = value.events;
      if (listed.length >= 2) break;
      await sleep(250);
    }
    assert.ok(listed.length >= 1, 'expected at least one diff event at 1fps');
    assert.ok(listed.length <= 20);

    // Default limit is 5; explicit limit is honoured.
    const page = await getEvents.execute({ limit: 2 }, {});
    assert.equal(page.limit, 2);
    assert.ok(page.count <= 2);
    const event = page.events[0];
    assert.ok(event.rois.length > 0);
    assert.ok(typeof event.diffMs === 'number');
    assert.ok(typeof event.time === 'string');
    assert.match(event.text, /^<untrusted_screen_data>[\s\S]*<\/untrusted_screen_data>$/);
    assert.ok(event.frame_url.startsWith(`${base}/vhcu/frame?`), event.frame_url);
    assert.match(event.frame_url, /[?&]sig=[0-9a-f]{64}/);
    assert.match(event.frame_url, /[?&]exp=\d+/);
    assert.ok(event.rois[0].frame_url.startsWith(`${base}/vhcu/frame?`));
    const roi = event.rois[0];
    assert.equal(roi.image_width, Math.min(roi.w, 960));
    assert.ok(Math.abs(roi.scale_factor - roi.w / roi.image_width) < 1e-6);
    assert.equal(event.scale_factor, roi.scale_factor);
    const roiImage = await fetch(roi.frame_url);
    assert.equal(roiImage.status, 200);
    assert.equal(Buffer.from(await roiImage.arrayBuffer()).readUInt32BE(16), roi.image_width);

    // The signed link is a real capability: it must return a PNG.
    const image = await fetch(event.frame_url);
    if (image.status !== 200) assert.fail(`frame fetch failed: ${image.status} ${await image.text()}`);
    assert.match(image.headers.get('content-type') ?? '', /image\/png/);
    const bytes = new Uint8Array(await image.arrayBuffer());
    assert.ok(bytes.length > 50);
    assert.equal(bytes[0], 0x89);
    assert.equal(bytes[1], 0x50);

    // Render embeds markdown images so the model sees the link next to the event.
    const rendered = getEvents.output.render({}, page);
    assert.equal(rendered[0].type, 'text');
    assert.match(rendered[0].text, /!\[变化区域 1\]\(http:\/\//);
    assert.match(rendered[0].text, /next_since_id=/);

    // since_id pages forward; an id in the future yields nothing new.
    const firstPage = await getEvents.execute({ since_id: 0, limit: 1 }, {});
    assert.equal(firstPage.count, 1);
    assert.ok(firstPage.events[0].id > 0);
    const noNew = await getEvents.execute({ since_id: 1_000_000_000 }, {});
    assert.equal(noNew.count, 0, JSON.stringify(noNew).slice(0, 300));

    // A forged signature, or no signature at all, must be refused from any caller.
    const forged = await fetch(
      `${base}/vhcu/frame?x=0&y=0&w=32&h=32&maxw=64&exp=9999999999999&sig=${'0'.repeat(64)}`,
      { headers: { origin: 'http://evil.example' } },
    );
    assert.equal(forged.status, 403);
    const unsigned = await fetch(`${base}/vhcu/frame?x=0&y=0&w=32&h=32&maxw=64`);
    assert.equal(unsigned.status, 403);

    const stopped = await stop.execute({}, {});
    assert.equal(stopped.ok, true);
    await waitFor(
      () => {
        try {
          process.kill(started.pid, 0);
          return false;
        } catch {
          return true;
        }
      },
      8000,
      'python child exit after tool stop',
    );
    const stoppedAgain = await stop.execute({}, {});
    assert.equal(stoppedAgain.ok, true);
  } finally {
    await new Promise((resolve) => server.close(resolve));
    dispose();
  }
});

test('P5 input tools: eyes-before-hands gate, dry-run injection, key validation, rate limit', { timeout: 60000 }, async () => {
  const previous = {
    dry: process.env.VHCU_INPUT_DRY_RUN,
    max: process.env.VHCU_ACTION_MAX,
    window: process.env.VHCU_ACTION_WINDOW_MS,
  };
  // Dry run makes the Python daemon validate without injecting real input.
  process.env.VHCU_INPUT_DRY_RUN = '1';
  process.env.VHCU_ACTION_MAX = '3';
  process.env.VHCU_ACTION_WINDOW_MS = '1500';
  const { ctx, tools, dispose } = createMockCtx({
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  });
  apply(ctx);
  // The Node-side guard captures these at apply time; the daemon only needs dry-run.
  if (previous.max === undefined) delete process.env.VHCU_ACTION_MAX;
  else process.env.VHCU_ACTION_MAX = previous.max;
  if (previous.window === undefined) delete process.env.VHCU_ACTION_WINDOW_MS;
  else process.env.VHCU_ACTION_WINDOW_MS = previous.window;

  const start = tools.get('vhcu_start_vision');
  const stop = tools.get('vhcu_stop_vision');
  const click = tools.get('vhcu_mouse_click');
  const type = tools.get('vhcu_type_text');
  const hotkey = tools.get('vhcu_key_press');
  const getEvents = tools.get('vhcu_get_vision_events');
  const fullFrame = tools.get('vhcu_get_full_frame');

  try {
    // Hands without eyes are refused.
    const refused = await click.execute({ x: 100, y: 100 }, {});
    assert.equal(refused.ok, false);
    assert.equal(refused.code, 'E_STATE');

    const started = await start.execute({}, {});
    assert.equal(started.ok, true, JSON.stringify(started));
    assert.equal(typeof started.active_window, 'string');
    const active = await tools.get('vhcu_get_active_window').execute({}, {});
    assert.equal(active.ok, true, JSON.stringify(active));
    assert.equal(typeof active.title, 'string');
    assert.equal(active.active_window, active.title);
    const recommended = await tools.get('vhcu_get_recommended_macros').execute({}, {});
    assert.equal(recommended.ok, true, JSON.stringify(recommended));
    assert.ok(Array.isArray(recommended.macros));
    assert.ok(recommended.macros.some((macro) => macro.name === 'macro_launch_app'));

    const clicked = await click.execute({ x: 100, y: 100 }, {});
    assert.equal(clicked.ok, true, JSON.stringify(clicked));
    assert.equal(clicked.action, 'click');
    assert.equal(clicked.dry_run, true);
    assert.equal(clicked.verify.tool, 'vhcu_get_vision_events');

    // Slow lane: without a read-back, the next action is refused.
    const unverified = await type.execute({ text: '你好 vhcu' }, {});
    assert.equal(unverified.ok, false);
    assert.equal(unverified.code, 'E_STATE');
    assert.match(unverified.error, /回读/);

    // A read-back (events or full frame) unlocks the next action.
    const confirm = await getEvents.execute({ limit: 1 }, {});
    assert.equal(confirm.ok, true);
    const typed = await type.execute({ text: '你好 vhcu' }, {});
    assert.equal(typed.ok, true, JSON.stringify(typed));
    assert.equal(typed.dry_run, true);

    const confirm2 = await fullFrame.execute({}, {});
    assert.equal(confirm2.ok, true, JSON.stringify(confirm2));
    assert.match(confirm2.frame_url, /\/vhcu\/frame\?/);

    const combo = await hotkey.execute({ keys: ['ctrl', 's'], confirm_destructive: true }, {});
    assert.equal(combo.ok, true, JSON.stringify(combo));
    await getEvents.execute({ limit: 1 }, {});

    // Rate limit: 3 successful actions were spent in the 1.5s window.
    const throttled = await click.execute({ x: 101, y: 101 }, {});
    assert.equal(throttled.ok, false);
    assert.equal(throttled.code, 'E_BUSY');

    // Let the window drain before validation checks (they must reach Python).
    await sleep(1700);
    const badKey = await hotkey.execute({ keys: ['ctrl', 'nope'] }, {});
    assert.equal(badKey.ok, false);
    assert.equal(badKey.code, 'E_PROTO');

    const badPoint = await click.execute({ x: -5, y: 0 }, {});
    assert.equal(badPoint.ok, true, JSON.stringify(badPoint));
    assert.equal(badPoint.x, 0);
    assert.match(badPoint.warning, /坐标越界/);

    const longText = await type.execute({ text: 'x'.repeat(2001) }, {});
    assert.equal(longText.ok, false);
    assert.equal(longText.code, 'E_PROTO');
  } finally {
    await stop.execute({}, {}).catch(() => {});
    dispose();
    if (previous.dry === undefined) delete process.env.VHCU_INPUT_DRY_RUN;
    else process.env.VHCU_INPUT_DRY_RUN = previous.dry;
  }
});


test('P6 full frame + scroll/drag: fresh capture, signed PNG, markdown, read-back gate', { timeout: 60000 }, async () => {
  const previous = {
    dry: process.env.VHCU_INPUT_DRY_RUN,
    max: process.env.VHCU_ACTION_MAX,
    window: process.env.VHCU_ACTION_WINDOW_MS,
  };
  process.env.VHCU_INPUT_DRY_RUN = '1';
  process.env.VHCU_ACTION_MAX = '20';
  process.env.VHCU_ACTION_WINDOW_MS = '2000';
  const { ctx, routes, tools, dispose } = createMockCtx({
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  });
  apply(ctx);
  if (previous.max === undefined) delete process.env.VHCU_ACTION_MAX;
  else process.env.VHCU_ACTION_MAX = previous.max;
  if (previous.window === undefined) delete process.env.VHCU_ACTION_WINDOW_MS;
  else process.env.VHCU_ACTION_WINDOW_MS = previous.window;
  const { server, base } = await startServer(routes);
  ctx.webServer.setOrigin(base);

  const start = tools.get('vhcu_start_vision');
  const stop = tools.get('vhcu_stop_vision');
  const fullFrame = tools.get('vhcu_get_full_frame');
  const click = tools.get('vhcu_mouse_click');
  const scroll = tools.get('vhcu_mouse_scroll');
  const drag = tools.get('vhcu_mouse_drag');
  const middleDrag = tools.get('vhcu_mouse_middle_drag');
  const keyPress = tools.get('vhcu_key_press');
  const typeText = tools.get('vhcu_type_text');
  const getEvents = tools.get('vhcu_get_vision_events');

  try {
    const early = await fullFrame.execute({}, {});
    assert.equal(early.ok, false);
    assert.equal(early.code, 'E_STATE');

    const earlyRelative = await click.execute({ x: 10, y: 10, is_relative_to_image: true }, {});
    assert.equal(earlyRelative.ok, false);
    assert.equal(earlyRelative.code, 'E_STATE');

    const started = await start.execute({}, {});
    assert.equal(started.ok, true, JSON.stringify(started));

    const frame = await fullFrame.execute({ max_width: 800 }, {});
    assert.equal(frame.ok, true, JSON.stringify(frame));
    assert.equal(frame.source, 'mss', 'full frame must come from the real screen source');
    assert.ok(frame.width > 0 && frame.height > 0);
    assert.ok(frame.frame_url.startsWith(`${base}/vhcu/frame?`), frame.frame_url);
    const query = new URL(frame.frame_url).searchParams;
    assert.equal(Number(query.get('w')), frame.width);
    assert.equal(Number(query.get('h')), frame.height);
    assert.match(frame.frame_url, /[?&]sig=[0-9a-f]{64}/);
    const image = await fetch(frame.frame_url);
    if (image.status !== 200) assert.fail(`full frame fetch ${image.status}: ${await image.text()}`);
    assert.match(image.headers.get('content-type') ?? '', /image\/png/);
    const bytes = new Uint8Array(await image.arrayBuffer());
    assert.equal(bytes[0], 0x89);
    const png = Buffer.from(bytes);
    assert.equal(png.readUInt32BE(16), 800, 'max_width=800 must produce an 800px-wide PNG');
    assert.equal(png.readUInt32BE(20), 450, 'height must scale proportionally (800x450)');
    assert.equal(frame.image_width, 800);
    assert.equal(frame.image_height, 450);
    assert.ok(Number.isFinite(frame.frame_timestamp) && frame.frame_timestamp > 0);
    assert.equal(frame.scale_factor, Number((frame.width / 800).toFixed(6)));
    assert.ok(Math.abs(frame.scale_factor - frame.width / 800) < 1e-6);

    // Image-space click must be converted by the daemon using scale_factor.
    const imageClick = await click.execute({ x: 100, y: 50, is_relative_to_image: true }, {});
    assert.equal(imageClick.ok, true, JSON.stringify(imageClick));
    assert.equal(imageClick.is_relative_to_image, true);
    assert.equal(imageClick.requested[0], 100);
    assert.equal(imageClick.x, Math.round(100 * frame.scale_factor));
    assert.equal(imageClick.y, Math.round(50 * frame.scale_factor));
    await getEvents.execute({ limit: 1 }, {});
    const middle = await middleDrag.execute(
      { x1: 20, y1: 30, x2: 120, y2: 80, is_relative_to_image: true },
      {},
    );
    assert.equal(middle.ok, true, JSON.stringify(middle));
    assert.equal(middle.action, 'middle_drag');
    assert.equal(middle.is_relative_to_image, true);
    assert.deepEqual(middle.from, [
      Math.round(20 * frame.scale_factor),
      Math.round(30 * frame.scale_factor),
    ]);
    await getEvents.execute({ limit: 1 }, {});
    assert.match(fullFrame.output.render({}, frame)[0].text, /!\[全屏截图\]\(http:\/\//);

    const scrolled = await scroll.execute({ amount: 3 }, {});
    assert.equal(scrolled.ok, true, JSON.stringify(scrolled));
    assert.equal(scrolled.dry_run, true);

    // Pending read-back blocks the next action, including drag.
    const blocked = await drag.execute({ x1: 100, y1: 100, x2: 240, y2: 200 }, {});
    assert.equal(blocked.ok, false);
    assert.equal(blocked.code, 'E_STATE');

    await getEvents.execute({ limit: 1 }, {});
    const dragged = await drag.execute({ x1: 100, y1: 100, x2: 240, y2: 200 }, {});
    assert.equal(dragged.ok, true, JSON.stringify(dragged));
    assert.equal(dragged.dry_run, true);

    // Data plane: action events carry ms ts, action name, duration and status.
    const events = await readEvents(base);
    assert.ok(events.every((entry) => ['vision', 'action', 'hear'].includes(entry.kind)));
    assert.ok(events.every((entry) => typeof entry.ts === 'number'));
    const actionEvents = events.filter((entry) => entry.kind === 'action');
    const scrollEvent = actionEvents.find((entry) => entry.action === 'scroll');
    const dragEvent = actionEvents.find((entry) => entry.action === 'drag');
    assert.ok(scrollEvent, 'scroll action event missing');
    assert.ok(dragEvent, 'drag action event missing');
    assert.equal(scrollEvent.ok, true);
    assert.equal(dragEvent.ok, true);
    assert.ok(scrollEvent.durationMs >= 0);
    assert.equal(scrollEvent.params.amount, 3);
    assert.deepEqual(dragEvent.params.from, [100, 100]);

    const zero = await scroll.execute({ amount: 0 }, {});
    assert.equal(zero.ok, false);
    assert.equal(zero.code, 'E_PROTO');

    await getEvents.execute({ limit: 1 }, {});
    await post(base, '/vhcu/settings', { settings: { turboMode: true } });
    const turboKey = await keyPress.execute({ keys: ['a'], skip_readback: true }, {});
    assert.equal(turboKey.ok, true, JSON.stringify(turboKey));
    assert.equal(turboKey.skip_readback, true);
    const turboClick = await click.execute({ x: 5, y: 5, skip_readback: true }, {});
    assert.equal(turboClick.ok, true, JSON.stringify(turboClick));
    assert.equal(turboClick.skip_readback, true);

    await post(base, '/vhcu/settings', { settings: { turboMode: false } });
    const normalClick = await click.execute({ x: 6, y: 6, skip_readback: true }, {});
    assert.equal(normalClick.ok, true, JSON.stringify(normalClick));
    assert.equal(normalClick.skip_readback, false, 'Turbo off must ignore skip_readback');
    const stillBlocked = await keyPress.execute({ keys: ['b'] }, {});
    assert.equal(stillBlocked.ok, false);
    assert.equal(stillBlocked.code, 'E_STATE');
    await getEvents.execute({ limit: 1 }, {});

    const typed = await typeText.execute({ text: 'Blender', use_clipboard_fallback: true }, {});
    assert.equal(typed.ok, true, JSON.stringify(typed));
    assert.equal(typed.clipboard_fallback, true);
    await getEvents.execute({ limit: 1 }, {});
    const keyed = await keyPress.execute({ keys: ['shift', 'a'], delay_ms: 35 }, {});
    assert.equal(keyed.ok, true, JSON.stringify(keyed));
    assert.equal(keyed.delay_ms, 35);
    await getEvents.execute({ limit: 1 }, {});
  } finally {
    await stop.execute({}, {}).catch(() => {});
    await new Promise((resolve) => server.close(resolve));
    dispose();
    if (previous.dry === undefined) delete process.env.VHCU_INPUT_DRY_RUN;
    else process.env.VHCU_INPUT_DRY_RUN = previous.dry;
  }
});

test('P6 regression: fake 320x180 capture must not be signed as a full frame', { timeout: 40000 }, async () => {
  const previousForce = process.env.VHCU_FORCE_FAKE;
  const previousDry = process.env.VHCU_INPUT_DRY_RUN;
  process.env.VHCU_FORCE_FAKE = '1';
  process.env.VHCU_INPUT_DRY_RUN = '1';
  const { ctx, tools, dispose } = createMockCtx({
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  });
  apply(ctx);
  const start = tools.get('vhcu_start_vision');
  const fullFrame = tools.get('vhcu_get_full_frame');
  const stop = tools.get('vhcu_stop_vision');
  try {
    const started = await start.execute({}, {});
    assert.equal(started.ok, true, JSON.stringify(started));
    assert.equal(started.source, 'fake');

    const frame = await fullFrame.execute({}, {});
    assert.equal(frame.ok, false, JSON.stringify(frame));
    assert.equal(frame.code, 'E_UNAVAILABLE');
    assert.match(frame.error, /320x180/);
    assert.equal(frame.frame_url, undefined);
  } finally {
    await stop.execute({}, {}).catch(() => {});
    dispose();
    if (previousForce === undefined) delete process.env.VHCU_FORCE_FAKE;
    else process.env.VHCU_FORCE_FAKE = previousForce;
    if (previousDry === undefined) delete process.env.VHCU_INPUT_DRY_RUN;
    else process.env.VHCU_INPUT_DRY_RUN = previousDry;
  }
});

test('P7 macros: open notepad + type, save, auto full-frame verify, read-back gate', { timeout: 60000 }, async () => {
  const previous = {
    dry: process.env.VHCU_INPUT_DRY_RUN,
    max: process.env.VHCU_ACTION_MAX,
    window: process.env.VHCU_ACTION_WINDOW_MS,
  };
  process.env.VHCU_INPUT_DRY_RUN = '1';
  process.env.VHCU_ACTION_MAX = '20';
  process.env.VHCU_ACTION_WINDOW_MS = '3000';
  const { ctx, tools, dispose } = createMockCtx({
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  });
  apply(ctx);
  if (previous.max === undefined) delete process.env.VHCU_ACTION_MAX;
  else process.env.VHCU_ACTION_MAX = previous.max;
  if (previous.window === undefined) delete process.env.VHCU_ACTION_WINDOW_MS;
  else process.env.VHCU_ACTION_WINDOW_MS = previous.window;

  const start = tools.get('vhcu_start_vision');
  const stop = tools.get('vhcu_stop_vision');
  const openMacro = tools.get('vhcu_macro_open_notepad_and_type');
  const saveMacro = tools.get('vhcu_macro_save_file');
  const type = tools.get('vhcu_type_text');
  const getEvents = tools.get('vhcu_get_vision_events');

  try {
    const early = await openMacro.execute({ text: 'x' }, {});
    assert.equal(early.ok, false);
    assert.equal(early.code, 'E_STATE');

    const started = await start.execute({}, {});
    assert.equal(started.ok, true, JSON.stringify(started));

    const opened = await openMacro.execute({ text: 'VHCU P7 宏测试' }, {});
    assert.equal(opened.ok, true, JSON.stringify(opened));
    assert.equal(opened.macro, 'macro_launch_app');
    assert.equal(opened.dry_run, true);
    assert.ok(Array.isArray(opened.steps) && opened.steps.includes('key'));
    assert.deepEqual(opened.steps_executed, opened.steps);
    assert.deepEqual(opened.variables, ['app_name', 'text']);
    assert.ok(opened.texts.includes('notepad'));
    assert.ok(opened.texts.includes('VHCU P7 宏测试'));
    assert.ok(typeof opened.elapsed_ms === 'number');
    assert.ok(opened.verify && typeof opened.verify.ok === 'boolean');
    assert.ok(opened.frame_url === null || typeof opened.frame_url === 'string');

    // Renderer must surface macro telemetry (regression: frame_url branch dropped them).
    const renderedMacro = openMacro.output.render({}, opened);
    assert.match(renderedMacro[0].text, /耗时：\d+ ms/);
    assert.match(renderedMacro[0].text, /步骤：/);

    // The macro auto-captured a frame, so pending read-back is already cleared.
    const typed = await type.execute({ text: 'ok' }, {});
    assert.equal(typed.ok, true, JSON.stringify(typed));

    // type left pending: save must be refused until a read-back.
    const blocked = await saveMacro.execute({}, {});
    assert.equal(blocked.ok, false);
    assert.equal(blocked.code, 'E_STATE');

    await getEvents.execute({ limit: 1 }, {});
    const saved = await saveMacro.execute({}, {});
    assert.equal(saved.ok, true, JSON.stringify(saved));
    assert.equal(saved.macro, 'save_file');
    assert.deepEqual(saved.steps, ['key']);
  } finally {
    await stop.execute({}, {}).catch(() => {});
    dispose();
    if (previous.dry === undefined) delete process.env.VHCU_INPUT_DRY_RUN;
    else process.env.VHCU_INPUT_DRY_RUN = previous.dry;
  }
});

test('P9 macros: save/run dynamic macros, persistence file, disabled E_DENIED', { timeout: 60000 }, async () => {
  const previousDry = process.env.VHCU_INPUT_DRY_RUN;
  process.env.VHCU_INPUT_DRY_RUN = '1';
  const { ctx, routes, tools, dispose } = createMockCtx({
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  });
  apply(ctx);
  const { server, base } = await startServer(routes);
  const start = tools.get('vhcu_start_vision');
  const stop = tools.get('vhcu_stop_vision');
  const save = tools.get('vhcu_save_macro');
  const run = tools.get('vhcu_run_macro');

  try {
    const started = await start.execute({}, {});
    assert.equal(started.ok, true, JSON.stringify(started));

    const created = await save.execute(
      {
        name: 'p9_flow',
        description: 'ctrl+s then wait',
        category: 'explorer',
        steps: [{ op: 'key', keys: ['ctrl', 's'] }, { op: 'wait', ms: 10 }],
      },
      {},
    );
    assert.equal(created.ok, true, JSON.stringify(created));
    assert.equal(created.macro.name, 'p9_flow');
    assert.equal(created.macro.category, 'explorer');
    assert.equal(created.macro.enabled, true);

    const duplicate = await save.execute({ name: 'p9_flow', steps: [{ op: 'wait', ms: 10 }] }, {});
    assert.equal(duplicate.ok, false);
    assert.equal(duplicate.code, 'E_CONFLICT');

    const ran = await run.execute({ name: 'p9_flow' }, {});
    assert.equal(ran.ok, true, JSON.stringify(ran));
    assert.equal(ran.dry_run, true);
    assert.deepEqual(ran.steps_executed, ['key', 'wait']);

    const missing = await run.execute({ name: 'no_such_macro' }, {});
    assert.equal(missing.ok, false);
    assert.equal(missing.code, 'E_NOT_FOUND');

    const toggled = await post(base, '/vhcu/macros', { action: 'toggle', name: 'p9_flow', enabled: false });
    assert.equal(toggled.status, 200, JSON.stringify(toggled.body));

    const blocked = await run.execute({ name: 'p9_flow' }, {});
    assert.equal(blocked.ok, false);
    assert.equal(blocked.code, 'E_DENIED');
  } finally {
    await stop.execute({}, {}).catch(() => {});
    await new Promise((resolve) => server.close(resolve));
    dispose();
    if (previousDry === undefined) delete process.env.VHCU_INPUT_DRY_RUN;
    else process.env.VHCU_INPUT_DRY_RUN = previousDry;
  }
});

test('P11.8 built-in groups + parameterized macros', { timeout: 60000 }, async () => {
  const previousDry = process.env.VHCU_INPUT_DRY_RUN;
  process.env.VHCU_INPUT_DRY_RUN = '1';
  const { ctx, routes, tools, dispose } = createMockCtx({
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  });
  apply(ctx);
  const { server, base } = await startServer(routes);
  const start = tools.get('vhcu_start_vision');
  const stop = tools.get('vhcu_stop_vision');
  const run = tools.get('vhcu_run_macro');

  try {
    const started = await start.execute({}, {});
    assert.equal(started.ok, true, JSON.stringify(started));

    const ran = await run.execute(
      { name: 'macro_create_folder', variables: { path: 'D:\\', folder_name: 'VHCU_P118' } },
      {},
    );
    assert.equal(ran.ok, true, JSON.stringify(ran));
    assert.equal(ran.dry_run, true);
    assert.deepEqual(ran.variables, ['path', 'folder_name']);
    assert.ok(ran.texts.includes('D:\\'));
    assert.ok(ran.texts.includes('VHCU_P118'));

    const missing = await run.execute({ name: 'macro_create_folder', variables: { path: 'D:\\' } }, {});
    assert.equal(missing.ok, false);
    assert.equal(missing.code, 'E_PROTO');

    const queue = await run.execute(
      { macros_queue: [{ name: 'macro_copy' }, { name: 'macro_paste' }] },
      {},
    );
    assert.equal(queue.ok, true, JSON.stringify(queue));
    assert.equal(queue.action, 'macro_queue');
    assert.equal(queue.count, 2);
    assert.deepEqual(queue.results.map((item) => item.macro), ['macro_copy', 'macro_paste']);
    assert.ok(queue.results.every((item) => item.verify?.ok === true));
    assert.ok(typeof queue.queue_ms === 'number');

    const queueStrings = await run.execute({ macros_queue: ['macro_undo', 'macro_select_all'] }, {});
    assert.equal(queueStrings.ok, true, JSON.stringify(queueStrings));
    assert.equal(queueStrings.count, 2);

    const queueMissing = await run.execute(
      { macros_queue: [{ name: 'macro_copy' }, { name: 'no_such_macro_xyz' }] },
      {},
    );
    assert.equal(queueMissing.ok, false);
    assert.equal(queueMissing.code, 'E_NOT_FOUND');
    assert.equal(queueMissing.failed_at, 1);
    assert.equal(queueMissing.results.length, 1);

    const builtinDelete = await post(base, '/vhcu/macros', { action: 'deleteCategory', name: '系统基础宏' });
    assert.equal(builtinDelete.status, 400);
    assert.equal(builtinDelete.body.code, 'E_DENIED');

    const builtinUpdate = await post(base, '/vhcu/macros', {
      action: 'update',
      name: 'macro_launch_app',
      macro: { description: 'hack' },
    });
    assert.equal(builtinUpdate.status, 400);
    assert.equal(builtinUpdate.body.code, 'E_DENIED');

    const added = await post(base, '/vhcu/macros', { action: 'addCategory', name: 'p118_group' });
    assert.equal(added.status, 200, JSON.stringify(added.body));
    assert.ok(added.body.categories.some((category) => category.name === 'p118_group'));
    const removed = await post(base, '/vhcu/macros', { action: 'deleteCategory', name: 'p118_group' });
    assert.equal(removed.status, 200);
    assert.equal(removed.body.categories.some((category) => category.name === 'p118_group'), false);
  } finally {
    await stop.execute({}, {}).catch(() => {});
    await new Promise((resolve) => server.close(resolve));
    dispose();
    if (previousDry === undefined) delete process.env.VHCU_INPUT_DRY_RUN;
    else process.env.VHCU_INPUT_DRY_RUN = previousDry;
  }
});

test('P11.9 mouse macros + variable mapping render', { timeout: 60000 }, async () => {
  const previousDry = process.env.VHCU_INPUT_DRY_RUN;
  process.env.VHCU_INPUT_DRY_RUN = '1';
  const { ctx, routes, tools, dispose } = createMockCtx({
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  });
  apply(ctx);
  const { server, base } = await startServer(routes);
  ctx.webServer.setOrigin(base);
  const start = tools.get('vhcu_start_vision');
  const stop = tools.get('vhcu_stop_vision');
  const fullFrame = tools.get('vhcu_get_full_frame');
  const run = tools.get('vhcu_run_macro');

  try {
    assert.equal((await start.execute({}, {})).ok, true);

    const early = await run.execute(
      { name: 'macro_mouse_double_click', variables: { x: 10, y: 10 }, is_relative_to_image: true },
      {},
    );
    assert.equal(early.ok, false);
    assert.equal(early.code, 'E_STATE');

    const frame = await fullFrame.execute({ max_width: 800 }, {});
    assert.equal(frame.ok, true, JSON.stringify(frame));

    const doubleClick = await run.execute(
      { name: 'macro_mouse_double_click', variables: { x: 100, y: 50 }, is_relative_to_image: true },
      {},
    );
    assert.equal(doubleClick.ok, true, JSON.stringify(doubleClick));
    assert.equal(doubleClick.scale_factor, frame.scale_factor);
    assert.deepEqual(doubleClick.variable_map, { x: '100', y: '50' });
    assert.deepEqual(doubleClick.coords, [
      [Math.round(100 * frame.scale_factor), Math.round(50 * frame.scale_factor)],
    ]);
    const rendered = run.output.render({}, doubleClick)[0].text;
    assert.match(rendered, /变量映射：/);
    assert.match(rendered, /x = 100/);
    assert.match(rendered, /坐标：/);

    const rightClick = await run.execute(
      { name: 'macro_mouse_right_click', variables: { x: 10, y: 20 } },
      {},
    );
    assert.equal(rightClick.ok, true, JSON.stringify(rightClick));
    assert.deepEqual(rightClick.coords, [[10, 20]]);

    const drag = await run.execute(
      { name: 'macro_window_drag', variables: { x1: 1, y1: 2, x2: 30, y2: 40 } },
      {},
    );
    assert.equal(drag.ok, true, JSON.stringify(drag));
    assert.deepEqual(drag.coords, [[1, 2, 30, 40]]);

    const folder = await run.execute(
      { name: 'macro_create_folder', variables: { path: 'D:/VHCU', folder_name: 'P119' } },
      {},
    );
    assert.equal(folder.ok, true, JSON.stringify(folder));
    const folderRender = run.output.render({}, folder)[0].text;
    assert.match(folderRender, /变量映射：/);
    assert.match(folderRender, /path = D:\/VHCU/);
    assert.match(folderRender, /展开文本：/);

    await post(base, '/vhcu/settings', { settings: { turboMode: true } });
    const turboMacro = await run.execute(
      {
        name: 'macro_create_folder',
        variables: { path: 'D:/VHCU', folder_name: 'TURBO' },
        skip_readback: true,
      },
      {},
    );
    assert.equal(turboMacro.ok, true, JSON.stringify(turboMacro));
    assert.equal(turboMacro.turbo, true);
    assert.equal(turboMacro.frame_url, null);
    assert.equal(turboMacro.verify.skipped, true);

    const tooMany = await run.execute(
      { macros_queue: Array.from({ length: 21 }, () => ({ name: 'macro_launch_app' })) },
      {},
    );
    assert.equal(tooMany.ok, false);
    assert.equal(tooMany.code, 'E_PROTO');
    assert.match(tooMany.error, /1\.\.20/);
  } finally {
    await stop.execute({}, {}).catch(() => {});
    await new Promise((resolve) => server.close(resolve));
    dispose();
    if (previousDry === undefined) delete process.env.VHCU_INPUT_DRY_RUN;
    else process.env.VHCU_INPUT_DRY_RUN = previousDry;
  }
});

test('P11.10 window snap + cleanup macros and cleanup rule', { timeout: 60000 }, async () => {
  const previousDry = process.env.VHCU_INPUT_DRY_RUN;
  process.env.VHCU_INPUT_DRY_RUN = '1';
  const { ctx, routes, tools, dispose } = createMockCtx({
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  });
  apply(ctx);
  const { server, base } = await startServer(routes);
  const start = tools.get('vhcu_start_vision');
  const stop = tools.get('vhcu_stop_vision');
  const run = tools.get('vhcu_run_macro');

  try {
    const rules = run.description;
    assert.match(rules, /行为准则/);
    assert.match(rules, /macro_close_active_window/);
    assert.match(tools.get('vhcu_macro_open_notepad_and_type').description, /macro_close_active_window/);

    assert.equal((await start.execute({}, {})).ok, true);
    for (const name of ['macro_snap_window_left', 'macro_snap_window_right', 'macro_close_active_window']) {
      let result;
      if (name === 'macro_close_active_window') {
        const pending = run.execute({ name }, {});
        const approval = await approvePendingApproval(base, 'allow');
        assert.equal(approval.status, 200, JSON.stringify(approval.body));
        result = await pending;
      } else {
        result = await run.execute({ name }, {});
      }
      assert.equal(result.ok, true, `${name}: ${JSON.stringify(result)}`);
      assert.equal(result.dry_run, true);
      assert.deepEqual(result.steps, ['key']);
      assert.deepEqual(result.variables, []);
    }

    const deniedPromise = run.execute({ name: 'macro_close_active_window' }, {});
    const deniedApproval = await approvePendingApproval(base, 'deny');
    assert.equal(deniedApproval.status, 200, JSON.stringify(deniedApproval.body));
    const denied = await deniedPromise;
    assert.equal(denied.ok, false, JSON.stringify(denied));
    assert.equal(denied.code, 'E_DENIED');
    assert.match(denied.error, /用户拒绝/);

    const enabled = await post(base, '/vhcu/settings', {
      settings: { autoApproveDestructive: true },
    });
    assert.equal(enabled.status, 200, JSON.stringify(enabled.body));
    const autoApproved = await Promise.race([
      run.execute({ name: 'macro_close_active_window' }, {}),
      new Promise((_, reject) => setTimeout(() => reject(new Error('auto-approve macro hung')), 5000)),
    ]);
    assert.equal(autoApproved.ok, true, JSON.stringify(autoApproved));
    assert.equal((await getState(base)).approval, null);
  } finally {
    await stop.execute({}, {}).catch(() => {});
    await new Promise((resolve) => server.close(resolve));
    dispose();
    if (previousDry === undefined) delete process.env.VHCU_INPUT_DRY_RUN;
    else process.env.VHCU_INPUT_DRY_RUN = previousDry;
  }
});

test('P12 hear tools: registration, empty events, daemon gate', { timeout: 20000 }, async () => {
  const { ctx, routes, tools, dispose } = createMockCtx({ logger: { info() {}, warn() {}, error() {}, debug() {} } });
  apply(ctx);
  const { server, base } = await startServer(routes);
  try {
    const startHear = tools.get('vhcu_start_hear');
    const stopHear = tools.get('vhcu_stop_hear');
    const getHear = tools.get('vhcu_get_hear_events');
    assert.match(startHear.description, /WASAPI Loopback/);
    assert.match(startHear.description, /VHCU_MODEL_PATH/);
    assert.match(startHear.description, /E_UNAVAILABLE/);
    assert.match(startHear.description, /独立于视觉/);
    assert.doesNotMatch(startHear.description, /需要先 vhcu_start_vision/);

    const empty = await getHear.execute({ limit: 5 }, {});
    assert.equal(empty.ok, true, JSON.stringify(empty));
    assert.deepEqual(empty.events, []);
    assert.equal(empty.count, 0);
    const rendered = getHear.output.render({}, empty)[0].text;
    assert.match(rendered, /没有系统内录转写/);

    const stopped = await stopHear.execute({}, {});
    assert.equal(stopped.ok, true);
    assert.equal(stopped.running, false);

    // /vhcu/control accepts hear.stop while idle (vision and daemon stay off).
    const controlStop = await post(base, '/vhcu/control', { action: 'hear.stop' });
    assert.equal(controlStop.status, 200, JSON.stringify(controlStop.body));
    assert.equal(controlStop.body.state.hear.state, 'idle');
    assert.equal(controlStop.body.state.daemonRunning, false);
  } finally {
    await new Promise((resolve) => server.close(resolve));
    dispose();
  }
});

test('P14 destructive hotkeys require confirm_destructive', async () => {
  const { ctx, tools, dispose } = createMockCtx({
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  });
  apply(ctx);
  try {
    const keyPress = tools.get('vhcu_key_press');
    for (const keys of [['alt', 'f4'], ['ctrl', 's'], ['delete']]) {
      const blocked = await keyPress.execute({ keys }, {});
      assert.equal(blocked.ok, false, keys.join('+'));
      assert.equal(blocked.code, 'E_CONFIRM_REQUIRED');
      assert.match(blocked.hint, /confirm_destructive/);
    }
    const confirmed = await keyPress.execute({ keys: ['ctrl', 's'], confirm_destructive: true }, {});
    assert.notEqual(confirmed.code, 'E_CONFIRM_REQUIRED');
    const safe = await keyPress.execute({ keys: ['ctrl', 'a'] }, {});
    assert.notEqual(safe.code, 'E_CONFIRM_REQUIRED');
  } finally {
    dispose();
  }
});

test('P15 multimodal tool returns an empty fused timeline with metadata', async () => {
  const { ctx, tools, dispose } = createMockCtx({
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  });
  apply(ctx);
  try {
    const tool = tools.get('vhcu_get_multimodal_events');
    assert.ok(tool);
    const empty = await tool.execute({ limit: 5 }, {});
    assert.equal(empty.ok, true, JSON.stringify(empty));
    assert.deepEqual(empty.events, []);
    assert.equal(empty.window_ms, 1500);
    const rendered = tool.output.render({}, empty)[0].text;
    assert.match(rendered, /没有融合事件/);
  } finally {
    dispose();
  }
});

test('P16.7 settings hot-updates hear sensitivity while daemon runs', { timeout: 30000 }, async () => {
  const { ctx, routes, tools, dispose } = createMockCtx({
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  });
  apply(ctx);
  const { server, base } = await startServer(routes);
  try {
    const started = await tools.get('vhcu_start_vision').execute({}, {});
    assert.equal(started.ok, true, JSON.stringify(started));
    const response = await post(base, '/vhcu/settings', {
      settings: { hearSensitivity: 'strict' },
    });
    assert.equal(response.status, 200, JSON.stringify(response.body));
    assert.equal(response.body.settings.hearSensitivity, 'strict');
    const state = await getState(base);
    assert.equal(state.hear.sensitivity, 'strict');
  } finally {
    await tools.get('vhcu_stop_vision').execute({}, {});
    await new Promise((resolve) => server.close(resolve));
    dispose();
  }
});

test('P18 safety: untrusted notices and emergency stop endpoint', { timeout: 30000 }, async () => {
  const { ctx, routes, tools, dispose } = createMockCtx({
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  });
  apply(ctx);
  for (const name of ['vhcu_get_vision_events', 'vhcu_get_hear_events', 'vhcu_get_multimodal_events']) {
    const tool = tools.get(name);
    assert.match(tool.description, /<untrusted_screen_data>/);
    assert.match(tool.description, /不可信/);
    assert.match(tool.description, /严禁/);
  }
  const { server, base } = await startServer(routes);
  try {
    const started = await tools.get('vhcu_start_vision').execute({}, {});
    assert.equal(started.ok, true, JSON.stringify(started));
    const missing = await fetch(`${base}/vhcu/emergency-stop`, { method: 'POST' });
    assert.equal(missing.status, 403);
    const stopped = await post(base, '/vhcu/emergency-stop', {});
    assert.equal(stopped.status, 200, JSON.stringify(stopped.body));
    assert.equal(stopped.body.ok, true);
    assert.equal(stopped.body.state.phase, 'idle');
    assert.equal(stopped.body.state.running, false);
    assert.equal(stopped.body.state.daemonRunning, false);
  } finally {
    await new Promise((resolve) => server.close(resolve));
    dispose();
  }
});

test('P18.2 memory clipboard is isolated, bounded and logged', { timeout: 20000 }, async () => {
  const { ctx, routes, tools, dispose } = createMockCtx({
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  });
  apply(ctx);
  const { server, base } = await startServer(routes);
  const controller = new AbortController();
  try {
    const set = tools.get('vhcu_set_memory_clipboard');
    const get = tools.get('vhcu_get_memory_clipboard');
    assert.ok(set && get);
    assert.match(set.description, /完全隔离/);
    assert.match(set.description, /OpenClipboard/);
    const sse = collectSse(await fetch(`${base}/vhcu/events`, { signal: controller.signal }));
    const first = await set.execute({ text: '内存中的秘密 123' }, {});
    assert.equal(first.ok, true, JSON.stringify(first));
    assert.equal(first.length, '内存中的秘密 123'.length);
    assert.equal(first.isolated, true);
    const read = await get.execute({}, {});
    assert.equal(read.ok, true);
    assert.equal(read.text, '内存中的秘密 123');
    assert.equal(read.empty, false);
    await waitFor(
      () => sse.frames.some((frame) => frame.json?.kind === 'action' && /📋 写入内存剪贴板/.test(frame.json?.text ?? '')),
      3000,
      'clipboard set action log',
    );
    const tooLong = await set.execute({ text: 'x'.repeat(5001) }, {});
    assert.equal(tooLong.ok, false);
    assert.equal(tooLong.code, 'E_PROTO');
    assert.equal((await get.execute({}, {})).text, '内存中的秘密 123');
    await set.execute({ text: '' }, {});
    const empty = await get.execute({}, {});
    assert.equal(empty.text, '');
    assert.equal(empty.empty, true);
  } finally {
    controller.abort();
    await new Promise((resolve) => server.close(resolve));
    dispose();
  }
});

test('P19.2 recording API starts/stops the global hook', { timeout: 20000 }, async () => {
  const { ctx, routes, dispose } = createMockCtx({
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  });
  apply(ctx);
  const { server, base } = await startServer(routes);
  const controller = new AbortController();
  try {
    const sse = collectSse(await fetch(`${base}/vhcu/events`, { signal: controller.signal }));
    const missing = await fetch(`${base}/vhcu/recording`, { method: 'POST' });
    assert.equal(missing.status, 403);
    const early = await post(base, '/vhcu/recording', { action: 'stop' });
    assert.equal(early.status, 409);
    const started = await post(base, '/vhcu/recording', { action: 'start' });
    assert.equal(started.status, 200, JSON.stringify(started.body));
    assert.equal(started.body.ok, true);
    assert.equal(started.body.recording.active, true);
    await waitFor(
      () => sse.frames.some((frame) => /开始录制宏/.test(frame.json?.text ?? '')),
      3000,
      'recording start action log',
    );
    const stopped = await post(base, '/vhcu/recording', { action: 'stop' });
    assert.equal(stopped.status, 200, JSON.stringify(stopped.body));
    assert.equal(stopped.body.ok, true);
    assert.equal(stopped.body.recording.active, false);
    assert.ok(Array.isArray(stopped.body.steps));
    await waitFor(
      () => sse.frames.some((frame) => /停止录制/.test(frame.json?.text ?? '')),
      3000,
      'recording stop action log',
    );
    const again = await post(base, '/vhcu/recording', { action: 'stop' });
    assert.equal(again.status, 409);
  } finally {
    controller.abort();
    await post(base, '/vhcu/recording', { action: 'stop' }).catch(() => {});
    await new Promise((resolve) => server.close(resolve));
    dispose();
  }
});

test('P21 every VHCU tool advertises priority over official vp_* tools', async () => {
  const { ctx, tools, dispose } = createMockCtx({
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  });
  apply(ctx);
  try {
    assert.equal(tools.size, 24);
    for (const tool of tools.values()) {
      assert.match(tool.description, /^\[⭐ 首选推荐\]/, `${tool.name} is missing the priority prefix`);
      assert.match(tool.description, /vp_\*/, `${tool.name} is missing the vp_* comparison`);
      assert.match(tool.description, /绝对优先/, `${tool.name} is missing the priority instruction`);
      assert.match(tool.description, /【兜底规则】/, `${tool.name} is missing the fallback rule`);
      assert.match(tool.description, /E_STATE, E_TIMEOUT, 守护进程崩溃/, `${tool.name} is missing fallback errors`);
      assert.match(tool.description, /降级使用官方 vp_\* 工具/, `${tool.name} is missing fallback permission`);
      assert.doesNotMatch(tool.description, /不要先试 vp_\*/, `${tool.name} forbids official fallback`);
      assert.match(tool.description, /【隐私绝对红线】/, `${tool.name} is missing the privacy red line`);
      assert.match(tool.description, /E_PRIVACY_BLOCKED/, `${tool.name} is missing E_PRIVACY_BLOCKED`);
      assert.match(tool.description, /绝对禁止使用 vp_\* 等任何外部工具进行兜底/, `${tool.name} allows external fallback on privacy block`);
      assert.match(tool.description, /直接向用户报告并暂停任务/, `${tool.name} is missing the privacy pause instruction`);
      assert.match(tool.description, /不依赖任何本地记忆/, `${tool.name} is not self-contained`);
    }
    const vision = tools.get('vhcu_start_vision').description;
    assert.match(vision, /不要混用官方的 vp_screenshot 或 vp_click/);
    assert.match(vision, /坐标映射冲突/);
    assert.match(
      vision,
      /如果 VHCU 视觉引擎启动失败（且不是 E_PRIVACY_BLOCKED），请立刻回退，使用官方的 vp_screenshot 作为备用感知方案/,
    );
    assert.match(vision, /vhcu_get_vision_events/);
    assert.match(vision, /坐标只使用 VHCU 返回的 frame_url\/ROI 坐标/);
    assert.match(vision, /如果对当前画面拿不准/);
    assert.match(vision, /必须先重新调用 vhcu_get_full_frame 确认画面/);
    assert.match(tools.get('vhcu_mouse_click').description, /如果对当前画面拿不准/);
    assert.match(tools.get('vhcu_run_macro').description, /必须先重新调用 vhcu_get_full_frame/);
  } finally {
    dispose();
  }
});

test('P22.2 click rejects stale frame_timestamp before any input', async () => {
  const { ctx, tools, dispose } = createMockCtx({
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  });
  apply(ctx);
  try {
    const click = tools.get('vhcu_mouse_click');
    const stale = await click.execute({ x: 10, y: 20, frame_timestamp: Date.now() - 5000 }, {});
    assert.equal(stale.ok, false);
    assert.equal(stale.code, 'E_STALE_FRAME');
    assert.match(stale.hint, /vhcu_get_full_frame/);
    const fresh = await click.execute({ x: 10, y: 20, frame_timestamp: Date.now() }, {});
    assert.notEqual(fresh.code, 'E_STALE_FRAME');
    const invalid = await click.execute({ x: 10, y: 20, frame_timestamp: -1 }, {});
    assert.equal(invalid.code, 'E_PROTO');
  } finally {
    dispose();
  }
});


test('P22.3 emergency explicitly unloads uiohook and AI injection is filtered', () => {
  const HERE = dirname(fileURLToPath(import.meta.url));
  const source = readFileSync(join(HERE, '..', '..', 'lib', 'index.js'), 'utf8');
  assert.match(source, /function unloadUiohook\(\)/);
  assert.match(source, /unloadUiohook\(\);\s*await stopMacroRecording/);
  assert.match(source, /isAiInjecting = true/);
  assert.match(source, /shouldCaptureInput\(isAiInjecting, state\.recording\.active\)/);
});

test('P23 soft pause state machine and HITL approval are wired', () => {
  const HERE = dirname(fileURLToPath(import.meta.url));
  const source = readFileSync(join(HERE, '..', '..', 'lib', 'index.js'), 'utf8');
  assert.match(source, /paused_by_user/);
  assert.match(source, /用户已停止操作，请重新截图确认状态/);
  assert.match(source, /E_PAUSED_BY_USER/);
  assert.match(source, /requestMacroApproval/);
  assert.match(source, /resolveMacroApproval/);
  assert.match(source, /path: '\/vhcu\/approval'/);
  assert.match(source, /is_destructive/);
});
