// Helpers for the permission catalogue (app/permissions.py) and pages that act on it.

// A save the API accepted, but some fields of which it left out for lack of permission
export const warnIgnoredFields = (response, showWarning, t) => {
  const fields = response?.data?.ignoredFields;
  if (fields?.length) {
    showWarning(t('admin.permissions.ignoredFields', { fields: fields.join(', ') }));
  }
};

// Translation key part of a catalogue function: its dots would nest
export const functionKey = (fn) => fn.replace(/\./g, '__');

export const moduleLabel = (t, module) => t(`permissions.modules.${module}`);
export const functionLabel = (t, fn) => t(`permissions.functions.${functionKey(fn)}.label`);
export const functionDescription = (t, fn) => t(`permissions.functions.${functionKey(fn)}.description`);

// Single keys that only show something, so they sit in the View column
const VIEW_ACTIONS = new Set(['radios.claim_code', 'announcements.email_details']);

// The permission of a function row in the View column and the one in the Change column (either may be null)
export const rowPermissions = (fn) => {
  let view = null;
  let edit = null;
  fn.permissions.forEach((p) => {
    if (p.level === 'view' || (p.level === 'action' && VIEW_ACTIONS.has(p.key))) view = p;
    else edit = p;
  });
  return { view, edit };
};

export const LEVELS = ['none', 'view', 'partial', 'full'];

// None, View, Partial or Full for a module, from the keys a role holds (administrator-only keys do not count).
// A key the read-only role also holds (programming a radio, a claim code) counts as viewing, not changing.
export const moduleLevel = (module, held) => {
  let any = false;
  let anyEdit = false;
  let full = true;
  module.functions.forEach((fn) => {
    const { view, edit } = rowPermissions(fn);
    [[view, false], [edit, true]].forEach(([p, isEdit]) => {
      if (!p || p.administratorOnly) return;
      if (held.has(p.key)) {
        any = true;
        if (isEdit && !p.readonlyDefault) anyEdit = true;
      } else {
        full = false;
      }
    });
  });
  if (!any) return 'none';
  if (full) return 'full';
  return anyEdit ? 'partial' : 'view';
};

// key -> permission of the catalogue
export const indexCatalog = (modules) => {
  const index = new Map();
  modules.forEach((m) => m.functions.forEach((fn) => fn.permissions.forEach((p) => index.set(p.key, p))));
  return index;
};

// The keys plus everything they need, transitively
export const withRequirements = (keys, index) => {
  const result = new Set(keys);
  const todo = [...result];
  while (todo.length) {
    const key = todo.pop();
    (index.get(key)?.requires || []).forEach((required) => {
      if (!result.has(required)) {
        result.add(required);
        todo.push(required);
      }
    });
  }
  return result;
};

// The keys without these, and without what needs them, transitively. Other keys whose requirement is
// missing already (stored but not counting) are left alone.
export const withoutDependants = (keys, removed, index) => {
  const result = new Set(keys);
  const gone = new Set(removed);
  gone.forEach((key) => result.delete(key));
  let changed = true;
  while (changed) {
    changed = false;
    [...result].forEach((key) => {
      if ((index.get(key)?.requires || []).some((required) => gone.has(required))) {
        result.delete(key);
        gone.add(key);
        changed = true;
      }
    });
  }
  return result;
};
