// Unit tests of the restore wizard's selection (src/app/static/js/pages/backup-scope.js).
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  applySelectors,
  freshScope,
  isFullRestore,
  nextBlocked,
  restoreRequest,
  selectedCount,
  selectedGroups,
  setMode,
  toggleGroup,
  toggleItem,
} from "../../src/app/static/js/pages/backup-scope.js";

const ID = "2026-07-30_10-19-38";
const LIBRECHAT = {
  kind: "module",
  selector: "module:librechat",
  archives: 2,
  requires_config: false,
  items: [
    { selector: "volume:papaia_librechat_mongo", target: "papaia_librechat_mongo" },
    { selector: "volume:papaia_librechat_files", target: "papaia_librechat_files" },
  ],
};
const KEYCLOAK = {
  kind: "module",
  selector: "module:keycloak",
  archives: 1,
  requires_config: true,
  items: [{ selector: "volume:papaia_keycloak_db", target: "papaia_keycloak_db" }],
};

function scoped() {
  const scope = freshScope(ID, 5, "ok");
  applySelectors(scope, {
    supported: true,
    groups: [LIBRECHAT, KEYCLOAK],
    services: [],
    notes: [],
    config_selector: "config",
  });
  setMode(scope, "some");
  return scope;
}

test("a fresh scope restores the whole point", () => {
  const scope = freshScope(ID, 5, "ok");
  assert.equal(scope.mode, "all");
  assert.equal(scope.step, 1);
  assert.equal(isFullRestore(scope), true);
  assert.equal(nextBlocked(scope), false);
  assert.deepEqual(restoreRequest(scope, true), {
    url: "/api/v1/maintenance/restore",
    body: { restore_point: ID, restart_clean: true },
  });
});

test("choosing by selection with nothing ticked blocks the next step", () => {
  const scope = scoped();
  assert.equal(nextBlocked(scope), true);
  assert.deepEqual(selectedGroups(scope), []);
  assert.equal(selectedCount(scope), 0);
});

test("a whole module replaces its single volumes and counts its archives", () => {
  const scope = scoped();
  toggleItem(scope, LIBRECHAT, LIBRECHAT.items[0], true);
  toggleGroup(scope, LIBRECHAT, true);
  assert.deepEqual([...scope.selected], ["module:librechat"]);
  assert.equal(selectedCount(scope), 2);
  assert.equal(isFullRestore(scope), false);
  assert.deepEqual(restoreRequest(scope, true), {
    url: "/api/v1/maintenance/restore/scoped",
    body: { restore_point: ID, only: ["module:librechat"] },
  });
});

test("single volumes are counted one by one", () => {
  const scope = scoped();
  toggleItem(scope, LIBRECHAT, LIBRECHAT.items[1], true);
  assert.equal(selectedCount(scope), 1);
  assert.deepEqual(selectedGroups(scope), [LIBRECHAT]);
  toggleItem(scope, LIBRECHAT, LIBRECHAT.items[1], false);
  assert.equal(nextBlocked(scope), true);
});

test("a group that needs the configuration turns the restore into a full one", () => {
  const scope = scoped();
  toggleGroup(scope, KEYCLOAK, true);
  assert.equal(scope.selected.has("config"), true);
  assert.equal(isFullRestore(scope), true);
  assert.equal(restoreRequest(scope, false).url, "/api/v1/maintenance/restore");
  toggleGroup(scope, KEYCLOAK, false);
  assert.equal(scope.selected.size, 0);
  assert.equal(isFullRestore(scope), false);
});

test("the configuration goes with the last volume of a group that needs it", () => {
  const scope = scoped();
  toggleItem(scope, KEYCLOAK, KEYCLOAK.items[0], true);
  assert.equal(scope.selected.has("config"), true);
  toggleItem(scope, KEYCLOAK, KEYCLOAK.items[0], false);
  assert.equal(scope.selected.has("config"), false);
});

test("going back to everything clears the selection", () => {
  const scope = scoped();
  toggleGroup(scope, LIBRECHAT, true);
  setMode(scope, "all");
  assert.equal(scope.selected.size, 0);
  assert.equal(isFullRestore(scope), true);
});

test("an answer without fields leaves safe defaults", () => {
  const scope = freshScope(ID, 1, "partial");
  applySelectors(scope, {});
  assert.equal(scope.supported, false);
  assert.deepEqual(scope.groups, []);
  assert.equal(scope.configSelector, "config");
});
