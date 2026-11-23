// What to restore: the state the three steps of the restore wizard share (backup.js), without
// the DOM, so node --test can run it (tests/js/backup.test.mjs).
//
// `selected` carries selectors exactly as the API takes them ("module:librechat",
// "volume:papaia_x"), so nothing has to be translated at submit time -- and picking a whole
// module is genuinely a different request from picking each of its volumes, not another
// spelling.

export function freshScope(id, artifacts, result) {
  return {
    id: id, artifacts: artifacts, result: result, supported: false,
    groups: [], services: [], notes: [], configSelector: 'config',
    mode: 'all', selected: new Set(), step: 1,
    // Which disclosures are open. Has to live here, not in the DOM: ticking a
    // checkbox rebuilds the picker, and a freshly built list would come back
    // collapsed under the hand that just opened it.
    expanded: new Set(),
  };
}

// The answer of GET /api/v1/maintenance/restore-points/{id}/selectors.
export function applySelectors(scope, data) {
  scope.supported = !!data.supported;
  scope.groups = data.groups || [];
  scope.services = data.services || [];
  scope.notes = data.notes || [];
  scope.configSelector = data.config_selector || 'config';
}

export function setMode(scope, mode) {
  scope.mode = mode;
  if (mode === 'all') scope.selected.clear();
}

export function toggleGroup(scope, group, checked) {
  if (checked) {
    scope.selected.add(group.selector);
    // A module and its volumes are alternatives, not a hierarchy: holding
    // both would ask the core to restore the same archive twice.
    group.items.forEach(function (i) { scope.selected.delete(i.selector); });
    if (group.requires_config) scope.selected.add(scope.configSelector);
  } else {
    scope.selected.delete(group.selector);
    group.items.forEach(function (i) { scope.selected.delete(i.selector); });
    if (group.requires_config) scope.selected.delete(scope.configSelector);
  }
}

export function toggleItem(scope, group, item, checked) {
  if (checked) {
    scope.selected.add(item.selector);
    if (group.requires_config) scope.selected.add(scope.configSelector);
  } else {
    scope.selected.delete(item.selector);
    var anyLeft = group.items.some(function (i) { return scope.selected.has(i.selector); });
    if (group.requires_config && !anyLeft) scope.selected.delete(scope.configSelector);
  }
}

// True when the selection can only be carried out by replacing the config
// directory -- which means the detached runner and the whole stack, not a job.
export function isFullRestore(scope) {
  return scope.mode === 'all' || scope.selected.has(scope.configSelector);
}

export function selectedGroups(scope) {
  return scope.groups.filter(function (g) {
    return scope.selected.has(g.selector) ||
           g.items.some(function (i) { return scope.selected.has(i.selector); });
  });
}

export function selectedCount(scope) {
  var count = 0;
  selectedGroups(scope).forEach(function (g) {
    count += scope.selected.has(g.selector)
      ? g.archives
      : g.items.filter(function (i) { return scope.selected.has(i.selector); }).length;
  });
  return count;
}

export function nextBlocked(scope) {
  return scope.mode === 'some' && selectedGroups(scope).length === 0;
}

// The request that starts the restore. A whole restore replaces the config directory and
// runs in the detached runner; a scoped one is an ordinary job.
export function restoreRequest(scope, restartClean) {
  if (isFullRestore(scope)) {
    return {
      url: '/api/v1/maintenance/restore',
      body: { restore_point: scope.id, restart_clean: !!restartClean },
    };
  }
  return {
    url: '/api/v1/maintenance/restore/scoped',
    body: { restore_point: scope.id, only: Array.from(scope.selected) },
  };
}
