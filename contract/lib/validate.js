/**
 * @file validate.js — dependency-free JSON Schema validator (VHCU subset).
 *
 * Why not ajv: this repo keeps runtime deps at zero (same rule as dsh-island).
 * The keywords below are exactly the ones our schemas use; anything else is a
 * contract bug and fails loudly instead of being ignored silently.
 *
 * Supported: $ref (absolute $id / local #/$defs/...), allOf, oneOf, anyOf,
 * type (string or array), enum, const, required, properties,
 * additionalProperties (boolean), items, minItems, maxItems, uniqueItems,
 * minimum, maximum, minLength, maxLength, pattern.
 */

const SUPPORTED = new Set([
  '$schema',
  '$id',
  'title',
  'description',
  '$ref',
  'allOf',
  'oneOf',
  'anyOf',
  'type',
  'enum',
  'const',
  'required',
  'properties',
  'additionalProperties',
  'items',
  'minItems',
  'maxItems',
  'uniqueItems',
  'minimum',
  'maximum',
  'minLength',
  'maxLength',
  'pattern',
  'default',
]);

function typeOf(value) {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  if (Number.isInteger(value)) return 'integer';
  return typeof value;
}

function typeMatches(expected, value) {
  const actual = typeOf(value);
  if (expected === 'number') return actual === 'number' || actual === 'integer';
  if (expected === 'integer') return actual === 'integer';
  return actual === expected;
}

function resolveRef(ref, root, registry) {
  if (ref.startsWith('#/')) {
    return ref
      .slice(2)
      .split('/')
      .reduce((node, key) => (node == null ? node : node[key]), root);
  }
  return registry.get(ref);
}

/**
 * @param {object} schema
 * @param {unknown} value
 * @param {{registry?: Map<string, object>, root?: object, path?: string}} [ctx]
 * @returns {string[]} error list; empty means valid
 */
export function validateNode(schema, value, ctx = {}) {
  const registry = ctx.registry ?? new Map();
  const root = ctx.root ?? schema;
  const path = ctx.path ?? '$';
  const errors = [];

  if (schema === true || schema == null) return errors;
  if (schema === false) {
    errors.push(`${path}: schema is false`);
    return errors;
  }

  for (const key of Object.keys(schema)) {
    if (!SUPPORTED.has(key)) errors.push(`${path}: unsupported schema keyword "${key}"`);
  }

  if (schema.$ref) {
    const target = resolveRef(schema.$ref, root, registry);
    if (!target) {
      errors.push(`${path}: unresolved $ref "${schema.$ref}"`);
      return errors;
    }
    return validateNode(target, value, { ...ctx, root, path });
  }

  if (schema.const !== undefined && !deepEqual(schema.const, value)) {
    errors.push(`${path}: expected const ${JSON.stringify(schema.const)}, got ${JSON.stringify(value)}`);
  }
  if (schema.enum && !schema.enum.some((candidate) => deepEqual(candidate, value))) {
    errors.push(`${path}: ${JSON.stringify(value)} is not one of ${JSON.stringify(schema.enum)}`);
  }

  if (schema.type) {
    const expected = Array.isArray(schema.type) ? schema.type : [schema.type];
    if (!expected.some((t) => typeMatches(t, value))) {
      errors.push(`${path}: expected type ${expected.join('|')}, got ${typeOf(value)}`);
      return errors;
    }
  }

  if (Array.isArray(schema.allOf)) {
    for (const sub of schema.allOf) errors.push(...validateNode(sub, value, { ...ctx, root, path }));
  }
  if (Array.isArray(schema.anyOf)) {
    const results = schema.anyOf.map((sub) => validateNode(sub, value, { ...ctx, root, path }));
    if (!results.some((r) => r.length === 0)) errors.push(`${path}: does not match anyOf`);
  }
  if (Array.isArray(schema.oneOf)) {
    const results = schema.oneOf.map((sub) => validateNode(sub, value, { ...ctx, root, path }));
    const matches = results.filter((r) => r.length === 0).length;
    if (matches !== 1) errors.push(`${path}: expected exactly one oneOf match, got ${matches}`);
  }

  if (typeof value === 'string') {
    if (schema.minLength !== undefined && value.length < schema.minLength) {
      errors.push(`${path}: shorter than ${schema.minLength}`);
    }
    if (schema.maxLength !== undefined && value.length > schema.maxLength) {
      errors.push(`${path}: longer than ${schema.maxLength}`);
    }
    if (schema.pattern && !new RegExp(schema.pattern, 'u').test(value)) {
      errors.push(`${path}: does not match ${schema.pattern}`);
    }
  }

  if (typeof value === 'number') {
    if (schema.minimum !== undefined && value < schema.minimum) {
      errors.push(`${path}: less than ${schema.minimum}`);
    }
    if (schema.maximum !== undefined && value > schema.maximum) {
      errors.push(`${path}: greater than ${schema.maximum}`);
    }
  }

  if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems) {
      errors.push(`${path}: fewer than ${schema.minItems} items`);
    }
    if (schema.maxItems !== undefined && value.length > schema.maxItems) {
      errors.push(`${path}: more than ${schema.maxItems} items`);
    }
    if (schema.uniqueItems) {
      const seen = new Set(value.map((v) => JSON.stringify(v)));
      if (seen.size !== value.length) errors.push(`${path}: items are not unique`);
    }
    if (schema.items) {
      value.forEach((item, index) => {
        errors.push(...validateNode(schema.items, item, { ...ctx, root, path: `${path}[${index}]` }));
      });
    }
  }

  if (value && typeof value === 'object' && !Array.isArray(value)) {
    if (Array.isArray(schema.required)) {
      for (const key of schema.required) {
        if (!(key in value)) errors.push(`${path}: missing required "${key}"`);
      }
    }
    const props = schema.properties ?? {};
    for (const [key, sub] of Object.entries(props)) {
      if (key in value) {
        errors.push(...validateNode(sub, value[key], { ...ctx, root, path: `${path}.${key}` }));
      }
    }
    if (schema.additionalProperties === false) {
      for (const key of Object.keys(value)) {
        if (!(key in props)) errors.push(`${path}: unexpected property "${key}"`);
      }
    }
  }

  return errors;
}

/**
 * @param {object} schema
 * @param {unknown} value
 * @param {Map<string, object>} registry
 */
export function validate(schema, value, registry = new Map()) {
  const errors = validateNode(schema, value, { registry, root: schema });
  return { ok: errors.length === 0, errors };
}

export function deepEqual(a, b) {
  if (a === b) return true;
  if (typeof a !== typeof b) return false;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((v, i) => deepEqual(v, b[i]));
  }
  if (a && b && typeof a === 'object') {
    const ka = Object.keys(a).sort();
    const kb = Object.keys(b).sort();
    return deepEqual(ka, kb) && ka.every((k) => deepEqual(a[k], b[k]));
  }
  return false;
}
