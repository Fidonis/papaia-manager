// Add-on catalogs (pages/catalogs.js) against a stand-in document: the listeners the module adds
// in place of the former on* attributes, the add and edit requests, and the row component.
import assert from "node:assert/strict";
import { test } from "node:test";

import { catalogRow, installCatalogPage } from "../../src/app/static/js/pages/catalogs.js";

class ClassList {
  constructor() { this.names = new Set(); }
  add(n) { this.names.add(n); }
  remove(n) { this.names.delete(n); }
  contains(n) { return this.names.has(n); }
}

function element(props = {}) {
  const listeners = {};
  return {
    value: "", checked: false, required: false, disabled: false, dataset: {}, classList: new ClassList(),
    opened: 0, closed: 0,
    showModal() { this.opened += 1; }, close() { this.closed += 1; },
    addEventListener(name, l) { (listeners[name] ||= []).push(l); },
    fire(name, event = {}) { (listeners[name] || []).forEach((l) => l(event)); },
    ...props,
  };
}

function page({ status = 201, body = {} } = {}) {
  const ids = {};
  for (const id of ["add-catalog-modal", "edit-catalog-modal", "catalog-type-select", "edit-name", "edit-type",
    "edit-url", "edit-ref", "edit-path", "edit-enabled", "edit-field-url", "edit-field-ref", "edit-field-path"]) {
    ids[id] = element();
  }
  for (const id of ["field-url", "field-ref", "field-path"]) {
    const field = element();
    field.input = element();
    field.querySelector = () => field.input;
    ids[id] = field;
  }
  const named = { name: element({ value: "extra" }), type: element({ value: "git" }), url: element({ value: "https://git.example.test/a.git" }),
    ref: element({ value: "main" }), path: element({ value: "/srv" }) };
  ids["add-catalog-form"] = element({ elements: named });
  ids["edit-catalog-form"] = element();
  const open = element({ dataset: { dialogOpen: "add-catalog-modal" } });
  const close = element({ dataset: { dialogClose: "edit-catalog-modal" } });
  const doc = {
    getElementById: (id) => ids[id],
    querySelector: () => ({ value: "csrf-1" }),
    querySelectorAll: (sel) => (sel === "[data-dialog-open]" ? [open] : sel === "[data-dialog-close]" ? [close] : []),
  };
  const sent = [];
  const alerts = [];
  const swaps = [];
  const updates = [];
  const api = installCatalogPage(doc, {
    texts: { addFailed: "Add catalog failed:" },
    actions: {
      authGuard: () => false,
      catalogUpdate: (name, b) => { updates.push([name, b]); return Promise.resolve(true); },
    },
    fetchImpl: (url, init) => {
      sent.push([url, init]);
      return Promise.resolve({ ok: status < 300, status, json: () => Promise.resolve(body) });
    },
    htmx: () => ({ ajax: (...a) => swaps.push(a) }),
    alert: (t) => alerts.push(t),
  });
  return { ids, named, open, close, api, sent, alerts, swaps, updates };
}

test("the buttons open and close their dialogs", () => {
  const { ids, open, close } = page();
  open.fire("click");
  close.fire("click");
  assert.equal(ids["add-catalog-modal"].opened, 1);
  assert.equal(ids["edit-catalog-modal"].closed, 1);
});

test("a git catalog is added with url and branch, then the list is redrawn", async () => {
  const { ids, sent, swaps } = page();
  let prevented = false;
  ids["add-catalog-form"].fire("submit", { preventDefault: () => { prevented = true; } });
  await new Promise((r) => setImmediate(r));
  assert.ok(prevented);
  assert.equal(sent[0][0], "/api/v1/catalogs");
  assert.equal(sent[0][1].headers["X-CSRF-Token"], "csrf-1");
  assert.deepEqual(JSON.parse(sent[0][1].body), { name: "extra", type: "git", url: "https://git.example.test/a.git", ref: "main" });
  assert.equal(ids["add-catalog-modal"].closed, 1);
  assert.deepEqual(swaps, [["GET", "/partials/catalogs", { target: "#catalog-list", swap: "innerHTML" }]]);
});

test("a refused add says why and keeps the dialog open", async () => {
  const { ids, alerts } = page({ status: 409, body: { detail: "exists" } });
  ids["add-catalog-form"].fire("submit", { preventDefault() {} });
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(alerts, ["Add catalog failed: exists"]);
  assert.equal(ids["add-catalog-modal"].closed, 0);
});

test("the type switches the fields, and closing the dialog resets it to git", () => {
  const { ids } = page();
  ids["catalog-type-select"].fire("change", { target: { value: "local" } });
  assert.ok(ids["field-url"].classList.contains("hidden"));
  assert.equal(ids["field-path"].input.required, true);
  assert.equal(ids["field-url"].input.disabled, true);
  ids["add-catalog-modal"].fire("close");
  assert.equal(ids["catalog-type-select"].value, "git");
  assert.ok(ids["field-path"].classList.contains("hidden"));
  assert.equal(ids["field-url"].input.required, true);
});

test("the edit dialog is filled from the row and saves through the update action", async () => {
  const { ids, api, updates } = page();
  api.openEditCatalogModal("main", "git", "https://git.example.test/a.git", "", "", true);
  assert.equal(ids["edit-ref"].value, "main");
  assert.equal(ids["edit-enabled"].checked, true);
  assert.equal(ids["edit-catalog-modal"].opened, 1);
  ids["edit-url"].value = "https://git.example.test/b.git";
  ids["edit-catalog-form"].fire("submit", { preventDefault() {} });
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(updates, [["main", { enabled: true, url: "https://git.example.test/b.git", ref: "main" }]]);
  assert.equal(ids["edit-catalog-modal"].closed, 1);
});

test("a row refreshes with its job id, deletes only after yes and opens the edit dialog", async () => {
  const calls = [];
  let answer = false;
  const opened = [];
  const row = catalogRow({
    actions: {
      catalogAction: (name, verb, onJob) => { calls.push([name, verb]); onJob("job-7"); assert.equal(row.jobId, "job-7"); return Promise.resolve(true); },
      catalogDelete: (name) => { calls.push(["delete", name]); return Promise.resolve(true); },
    },
    confirm: () => answer,
    openEdit: (...a) => opened.push(a),
  });
  const refreshing = row.refresh("main");
  assert.equal(row.busy, true);
  await refreshing;
  assert.equal(row.busy, false);
  assert.equal(row.jobId, null);
  await row.remove("main", "Delete?");
  answer = true;
  await row.remove("main", "Delete?");
  assert.deepEqual(calls, [["main", "refresh"], ["delete", "main"]]);
  row.edit({ dataset: { name: "disk", type: "local", url: "", ref: "", path: "/srv", enabled: "false" } });
  assert.deepEqual(opened, [["disk", "local", "", "", "/srv", false]]);
});
