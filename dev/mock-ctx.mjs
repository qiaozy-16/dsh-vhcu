/**
 * @file mock-ctx.js — the smallest cordis-shaped context that can host the
 * dsh-vhcu plugin outside DSH (dev server + unit tests).
 *
 * It mirrors only the faces the plugin touches: webServer.register, effect,
 * on('dispose'), logger. Anything else stays undefined on purpose: if the
 * plugin grows a dependency on a real host service, the dev harness should
 * fail loudly instead of silently faking it.
 */

const noop = () => {};

export function createMockCtx({ logger = console, label = 'vhcu-mock' } = {}) {
  const routes = new Map();
  const tools = new Map();
  const disposeHandlers = [];
  const disposers = [];
  let origin = '';

  const ctx = {
    logger: {
      debug: (...args) => logger.debug?.(label, ...args),
      info: (...args) => logger.info?.(label, ...args),
      warn: (...args) => logger.warn?.(label, ...args),
      error: (...args) => logger.error?.(label, ...args),
    },
    tools: {
      register(definition) {
        if (!definition?.name || typeof definition.execute !== 'function') {
          throw new TypeError('tool requires name + execute');
        }
        if (tools.has(definition.name)) throw new Error(`duplicate tool ${definition.name}`);
        tools.set(definition.name, definition);
        return () => tools.delete(definition.name);
      },
    },
    webServer: {
      register(route) {
        if (!route?.path || typeof route.handler !== 'function') {
          throw new TypeError('route requires path + handler');
        }
        if (routes.has(route.path)) throw new Error(`duplicate route ${route.path}`);
        routes.set(route.path, route);
        return () => routes.delete(route.path);
      },
      registerUpgrade() {
        return noop;
      },
      /** Mirrors the real WebServer.getOrigin(); tests/dev server set it after listen. */
      getOrigin() {
        return origin;
      },
      setOrigin(value) {
        origin = String(value ?? '').replace(/\/+$/, '');
      },
    },
    effect(body, name = 'effect') {
      if (typeof body !== 'function') throw new TypeError('effect body must be a function');
      const disposer = body();
      if (typeof disposer === 'function') disposers.push({ name, disposer });
      return () => {
        const index = disposers.findIndex((item) => item.name === name);
        if (index >= 0) disposers.splice(index, 1)[0].disposer();
      };
    },
    on(event, handler) {
      if (event === 'dispose') disposeHandlers.push(handler);
    },
  };

  const dispose = () => {
    for (const handler of disposeHandlers.splice(0)) {
      try {
        handler();
      } catch {
        // mock teardown must not mask the real assertion
      }
    }
    for (const { disposer } of disposers.splice(0)) {
      try {
        disposer();
      } catch {
        // ignore
      }
    }
    routes.clear();
    tools.clear();
  };

  return { ctx, routes, tools, dispose };
}
