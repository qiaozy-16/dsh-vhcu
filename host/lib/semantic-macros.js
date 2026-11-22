/**
 * @file semantic-macros.js — generic semantic metadata for reusable macros.
 *
 * VHCU is a computer-use layer, not an MCP server and not an app-specific
 * integration. A semantic macro therefore stores:
 *   - what the function is (app/function/params) for naming and discovery;
 *   - a generic, parameterized action plan that the normal input layer executes.
 *
 * There is deliberately no per-app resolver or hidden API call here. Keeping
 * semantics as metadata prevents a Blender/Office/IDE adapter matrix from
 * becoming a second, unverified control plane.
 */

const MAX_APP = 40;
const MAX_FUNCTION = 80;
const MAX_PARAM_TEXT = 2000;
const MAX_PARAMS = 24;

function fail(code, message, hint) {
  throw Object.assign(new Error(message), { code, hint });
}

function cleanName(value, label, max) {
  const text = String(value ?? '').trim().toLowerCase();
  if (!/^[a-z0-9][a-z0-9_.-]*$/.test(text) || text.length > max) {
    fail('E_PROTO', `${label} must be a lowercase identifier up to ${max} chars`);
  }
  return text;
}

function cleanParams(raw) {
  if (raw === undefined || raw === null) return {};
  if (typeof raw !== 'object' || Array.isArray(raw)) {
    fail('E_PROTO', 'semantic.params must be an object');
  }
  const entries = Object.entries(raw);
  if (entries.length > MAX_PARAMS) fail('E_PROTO', `semantic.params exceeds ${MAX_PARAMS} keys`);
  const params = {};
  for (const [key, value] of entries) {
    if (!/^[a-zA-Z0-9_]{1,40}$/.test(key)) {
      fail('E_PROTO', `semantic.params key is invalid: ${key}`);
    }
    if (value === null || ['string', 'number', 'boolean'].includes(typeof value)) {
      if (typeof value === 'string' && value.length > MAX_PARAM_TEXT) {
        fail('E_PROTO', `semantic.params.${key} exceeds ${MAX_PARAM_TEXT} chars`);
      }
      params[key] = value;
      continue;
    }
    fail('E_PROTO', `semantic.params.${key} must be a string, number, or boolean`);
  }
  return params;
}

export function normalizeSemantic(raw) {
  if (raw === undefined || raw === null) return null;
  if (typeof raw !== 'object' || Array.isArray(raw)) {
    fail('E_PROTO', 'semantic must be an object');
  }
  return {
    app: cleanName(raw.app, 'semantic.app', MAX_APP),
    function: cleanName(raw.function, 'semantic.function', MAX_FUNCTION),
    params: cleanParams(raw.params),
  };
}

/**
 * Resolve a semantic macro to its generic action plan. The plan is authored by
 * the model and remains executable by the ordinary input layer; semantic
 * metadata never substitutes a hidden per-app call.
 */
export function resolveSemanticSteps(macro) {
  if (!macro?.semantic) return macro?.steps ?? [];
  if (!Array.isArray(macro.steps) || macro.steps.length === 0) {
    fail(
      'E_SEMANTIC_EMPTY',
      'semantic macro has no generic action plan',
      'Provide parameterized steps for the function; VHCU does not call per-app APIs.',
    );
  }
  return macro.steps;
}
