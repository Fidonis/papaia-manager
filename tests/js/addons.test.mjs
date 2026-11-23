// The add-on pages (pages/addons.js): the component behind the add-on verbs, the install form,
// the maintenance actions, the rollback and the configuration editor, and the header's
// "Refresh catalogs". Stand-ins replace the shared actions, htmx and the dialogs.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  addonControls,
  catalogRefresh,
  configEnv,
  inputChanged,
  installEnv,
  refreshAllCatalogs,
} from "../../src/app/static/js/pages/addons.js";

const input = (dataset, value) => ({ dataset, value });

function dialog() {
  return { opened: 0, closed: 0, showModal() { this.opened += 1; }, close() { this.closed += 1; } };
}

function setup({ ok = true, confirmAnswer = true } = {}) {
  const calls = [];
  const swaps = [];
  const polls = [];
  const verb = (label) => (...args) => {
    calls.push([label, ...args]);
    return Promise.resolve(ok);
  };
  const actions = {
    addonAction: verb("addonAction"),
    galleryAction: verb("galleryAction"),
    addonMaintenanceAction: verb("addonMaintenanceAction"),
    addonPost: (...args) => {
      calls.push(["addonPost", ...args]);
      return Promise.resolve(ok ? { job_id: "job-1" } : null);
    },
    pollJob: (id, done) => {
      polls.push(id);
      done({ id, status: "succeeded" });
    },
  };
  const htmx = () => ({ ajax: (...args) => swaps.push(args) });
  const asked = [];
  const component = addonControls({
    actions,
    htmx,
    confirm: (text) => {
      asked.push(text);
      return confirmAnswer;
    },
  });
  component.$refs = {
    installModal: dialog(),
    removeModal: dialog(),
    uninstallModal: dialog(),
    rollbackModal: dialog(),
    saveConfigModal: dialog(),
  };
  return { component, calls, swaps, polls, asked };
}

test("the install form sends a field only when it says something", () => {
  const env = installEnv([
    input({ envKey: "URL", example: "https://x" }, " https://x "),
    input({ envKey: "NAME", example: "demo" }, "other"),
    input({ envKey: "TOKEN", omitIfEmpty: "1" }, ""),
    input({ envKey: "KEY", omitIfEmpty: "1" }, "s3"),
  ]);
  assert.deepEqual(env, { NAME: "other", KEY: "s3" });
});

test("the editor keeps a blank secret and sends a cleared field that had a value", () => {
  const env = configEnv([
    input({ envKey: "TOKEN", isSecret: "1", initial: "" }, ""),
    input({ envKey: "URL", isSecret: "", initial: "https://a" }, ""),
    input({ envKey: "NEW", isSecret: "", initial: "" }, ""),
    input({ envKey: "PORT", isSecret: "", initial: "80" }, "81"),
  ]);
  assert.deepEqual(env, { URL: "", PORT: "81" });
  assert.equal(inputChanged(input({ isSecret: "1" }, "  ")), false);
  assert.equal(inputChanged(input({ isSecret: "1" }, "x")), true);
  assert.equal(inputChanged(input({ isSecret: "", initial: "a" }, "a")), false);
  assert.equal(inputChanged(input({ isSecret: "" }, "a")), true);
});

test("a verb runs through the action of its view and stays locked on success", async () => {
  const { component, calls } = setup();
  await component.run("gallery", "wiki", "start");
  assert.deepEqual(calls, [["galleryAction", "wiki", "start", {}]]);
  assert.equal(component.busy, true);
  assert.equal(component.activeVerb, "start");
  await component.run("detail", "wiki", "stop");
  assert.deepEqual(calls.at(-1), ["addonAction", "wiki", "stop", {}]);
});

test("a refused verb frees the buttons", async () => {
  const { component } = setup({ ok: false });
  await component.run("detail", "wiki", "stop");
  assert.equal(component.busy, false);
  assert.equal(component.activeVerb, "");
});

test("an unknown view is refused", () => {
  const { component, calls } = setup();
  assert.throws(() => component.run("window", "wiki", "start"), /unknown add-on view/);
  assert.deepEqual(calls, []);
});

test("remove and uninstall close their dialog first", async () => {
  const { component, calls } = setup();
  await component.confirmRun("removeModal", "gallery", "wiki", "remove");
  await component.confirmRun("uninstallModal", "detail", "wiki", "uninstall");
  assert.equal(component.$refs.removeModal.closed, 1);
  assert.equal(component.$refs.uninstallModal.closed, 1);
  assert.deepEqual(calls, [
    ["galleryAction", "wiki", "remove", {}],
    ["addonAction", "wiki", "uninstall", {}],
  ]);
});

test("install sends the catalog, the fields and the start choice", async () => {
  const { component, calls } = setup();
  const form = {
    querySelectorAll: () => [input({ envKey: "URL", example: "" }, "https://wiki")],
    querySelector: () => ({ checked: false }),
  };
  await component.install(form, "gallery", "wiki", "main");
  assert.equal(component.$refs.installModal.closed, 1);
  assert.deepEqual(calls, [
    ["galleryAction", "wiki", "install", { catalog: "main", env: { URL: "https://wiki" }, start: false }],
  ]);
  assert.equal(component.activeVerb, "install");
});

test("a destructive maintenance action asks first and stops on no", async () => {
  const declined = setup({ confirmAnswer: false });
  await declined.component.maintain("wiki", "reset", true, "Run reset?");
  assert.deepEqual(declined.asked, ["Run reset?"]);
  assert.deepEqual(declined.calls, []);
  assert.equal(declined.component.busy, false);

  const plain = setup();
  await plain.component.maintain("wiki", "check", false, "Run check?");
  assert.deepEqual(plain.asked, []);
  assert.deepEqual(plain.calls, [["addonMaintenanceAction", "wiki", "check"]]);
  assert.equal(plain.component.activeVerb, "check");
});

test("the rollback opens without a choice and sends whether the data goes back", async () => {
  const { component, calls } = setup({ ok: false });
  component.mode = "data";
  component.openRollback();
  assert.equal(component.mode, "");
  assert.equal(component.$refs.rollbackModal.opened, 1);
  component.mode = "data";
  await component.rollback("wiki");
  assert.deepEqual(calls, [["addonAction", "wiki", "rollback", { restore_data: true }]]);
  assert.equal(component.busy, false);
});

test("the editor marks itself changed, resets on cancel and saves with the job", async () => {
  const { component, calls, swaps, polls } = setup();
  const fields = [
    input({ envKey: "URL", isSecret: "", initial: "https://a" }, "https://b"),
    input({ envKey: "TOKEN", isSecret: "1", initial: "" }, "new"),
  ];
  component.$el = { querySelectorAll: () => fields };
  component.noteInput({ target: { dataset: {} } });
  assert.equal(component.dirty, false);
  component.noteInput({ target: fields[0] });
  assert.equal(component.dirty, true);

  await component.saveConfig("wiki", true);
  assert.deepEqual(calls, [["addonPost", "wiki", "save-config", { env: { URL: "https://b", TOKEN: "new" }, restart: true }]]);
  assert.deepEqual(polls, ["job-1"]);
  assert.deepEqual(swaps, [["GET", "/partials/addons/wiki", { target: "#addon-detail", swap: "innerHTML" }]]);

  component.editMode = true;
  component.cancelEdit();
  assert.deepEqual(fields.map((f) => f.value), ["https://a", ""]);
  assert.equal(component.editMode, false);
  assert.equal(component.dirty, false);
});

test("a refused save frees the editor", async () => {
  const { component, polls } = setup({ ok: false });
  component.$el = { querySelectorAll: () => [] };
  await component.saveConfig("wiki", false);
  assert.equal(component.busy, false);
  assert.deepEqual(polls, []);
});

test("refreshing the catalogs fetches each enabled git catalog and redraws the gallery", async () => {
  const refreshed = [];
  const swaps = [];
  const deps = {
    actions: {
      authGuard: () => false,
      catalogAction: (name, verb) => {
        refreshed.push([name, verb]);
        return Promise.resolve(true);
      },
    },
    fetchImpl: () => Promise.resolve({
      ok: true,
      json: () => Promise.resolve([
        { name: "main", type: "git", enabled: true },
        { name: "off", type: "git", enabled: false },
        { name: "disk", type: "local", enabled: true },
      ]),
    }),
    htmx: () => ({ ajax: (...args) => swaps.push(args) }),
  };
  const button = catalogRefresh(deps);
  const done = button.refreshAll();
  assert.equal(button.busy, true);
  await done;
  assert.equal(button.busy, false);
  assert.deepEqual(refreshed, [["main", "refresh"]]);
  assert.deepEqual(swaps, [["GET", "/partials/addons", { target: "#addon-gallery", swap: "innerHTML" }]]);

  const ended = [];
  await refreshAllCatalogs({ ...deps, actions: { ...deps.actions, authGuard: () => true }, htmx: () => ({ ajax: () => ended.push(1) }) });
  assert.deepEqual(refreshed.length, 1);
  assert.deepEqual(ended, [1]);
});
