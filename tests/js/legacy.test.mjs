// The transition list of core/legacy.js: exactly the listed names reach `window`, each with a
// value. tests/test_js_core.py checks that the list equals the names the templates still use.
import assert from "node:assert/strict";
import { test } from "node:test";

import { installLegacy, LEGACY_NAMES } from "../../src/app/static/js/core/legacy.js";

test("the list is sorted, without repeats, and frozen", () => {
  assert.deepEqual([...LEGACY_NAMES], [...new Set(LEGACY_NAMES)].sort());
  assert.ok(Object.isFrozen(LEGACY_NAMES));
  assert.ok(LEGACY_NAMES.includes("showToast"));
  assert.ok(LEGACY_NAMES.includes("fmt"));
  // The add-on, stack and catalog pages import their actions (pages/addons.js, services.js,
  // catalogs.js), so these are no globals any more.
  for (const name of ["addonAction", "galleryAction", "stackAction", "stackGroupAction", "catalogAction"]) {
    assert.ok(!LEGACY_NAMES.includes(name), name);
  }
});

test("exactly the listed names are assigned", () => {
  const api = Object.fromEntries(LEGACY_NAMES.map((name) => [name, () => name]));
  api.notListed = () => "no";
  const target = {};
  installLegacy(target, api);
  assert.deepEqual(Object.keys(target).sort(), [...LEGACY_NAMES]);
  assert.equal(target.showToast(), "showToast");
});

test("a listed name without a value is an error", () => {
  const api = Object.fromEntries(LEGACY_NAMES.filter((n) => n !== "showToast").map((n) => [n, 1]));
  assert.throws(() => installLegacy({}, api), /legacy names without a value: showToast/);
});
