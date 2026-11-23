// Unit tests of the Collections page (src/app/static/js/pages/collections.js, node --test).
//
// The dialogs take the page's browser parts as arguments, so these tests hand them stand-ins:
// a recording htmx, ragRequest and toast, and a small document.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  collectionConnection,
  collectionRoles,
  createDeleteCollection,
  createEditRoles,
  createNewCollection,
  createOperatorGrant,
  installCollectionListeners,
  showListError,
} from "../../src/app/static/js/pages/collections.js";
import { MiniDocument } from "./fixtures/mini-dom.mjs";

const TEXTS = {
  loadFailed: "The list could not be loaded.",
  unavailable: "Collections are not available",
  reloadPage: "Reload the page",
  created: "Collection <b>{name}</b> created",
  rolesSaved: "Roles of <b>{name}</b> saved",
  deleted: "Collection <b>{name}</b> deleted",
  grantFailed: "The grant was not created:",
  rolesTitle: 'Roles of <span class="font-mono">{name}</span>',
  deleteTitle: 'Delete <span class="font-mono">{name}</span>?',
  points: "with its {points} points",
};

function dialog() {
  return {
    open: false,
    showModal() { this.open = true; },
    close() { this.open = false; },
  };
}

function world({ listConnection = "lab" } = {}) {
  const doc = new MiniDocument();
  doc.element("select", { id: "collection-connection", value: "default" });
  const list = doc.element("div", { id: "collection-list" });
  if (listConnection) doc.element("div", { "data-connection": listConnection }, list);
  const swaps = [];
  const toasts = [];
  const requests = [];
  const answers = [];
  const deps = {
    texts: TEXTS,
    doc,
    htmx: { ajax: (method, url, options) => swaps.push({ method, url, ...options }) },
    ragRequest: async (method, url, body) => {
      requests.push({ method, url, body });
      return answers.shift() || { ok: true, data: null };
    },
    showToast: (type, html) => toasts.push({ type, html }),
  };
  return { doc, deps, swaps, toasts, requests, answers };
}

function component(factory, deps) {
  const c = factory(deps);
  c.$root = dialog();
  return c;
}

test("changes go to the connection of the list on screen", () => {
  assert.equal(collectionConnection(world().doc), "lab");
  assert.equal(collectionConnection(world({ listConnection: "" }).doc), "default");
});

test("blank role rows are dropped and names trimmed", () => {
  assert.deepEqual(
    collectionRoles([{ role: " finance ", access: "rw" }, { role: "  ", access: "r" }]),
    [{ role: "finance", access: "rw" }],
  );
});

test("a new collection is posted, the dialog closes and the list reloads", async () => {
  const { deps, requests, swaps, toasts } = world();
  const create = component(createNewCollection, deps);
  create.name = " finance ";
  create.vectorSize = "1024";
  create.model = "";
  create.addRole();
  create.roles[0].role = "fin";
  create.addRole();
  create.removeRole(1);
  await create.submit();

  assert.deepEqual(requests[0], {
    method: "POST",
    url: "/api/v1/rag/collections?connection=lab",
    body: { name: "finance", vector_size: 1024, embedding_model: null, roles: [{ role: "fin", access: "r" }] },
  });
  assert.deepEqual(toasts, [{ type: "success", html: "Collection <b>finance</b> created" }]);
  assert.equal(swaps[0].url, "/partials/collections?connection=lab");
  assert.equal(swaps[0].target, "#collection-list");
  create.reset();
  assert.equal(create.name, "");
  assert.deepEqual(create.roles, []);
});

test("a name in a message is escaped", async () => {
  const { deps, toasts } = world();
  const create = component(createNewCollection, deps);
  create.name = "<img>";
  create.vectorSize = "4";
  await create.submit();

  assert.equal(toasts[0].html, "Collection <b>&lt;img&gt;</b> created");
});

test("a refusal stays in the dialog", async () => {
  const { deps, answers, swaps } = world();
  const create = component(createNewCollection, deps);
  create.$root.open = true;
  answers.push({ ok: false, error: "exists" });
  await create.submit();

  assert.equal(create.error, "exists");
  assert.equal(create.$root.open, true);
  assert.equal(swaps.length, 0);
});

test("the roles dialog opens from a row, copies the rows and saves them", async () => {
  const { doc, deps, requests, toasts } = world();
  const roles = component(createEditRoles, deps);
  const rows = [{ role: "fin", access: "r" }];
  roles.open({ name: "kb", roles: rows });
  roles.roles[0].access = "rw";

  assert.equal(rows[0].access, "r", "the row's own data is not changed");
  assert.equal(roles.$root.open, true);
  const title = doc.element("span");
  roles.markTitle(title);
  assert.equal(title.textContent, "Roles of kb");
  await roles.submit();
  assert.deepEqual(requests[0], {
    method: "PUT",
    url: "/api/v1/rag/collections/kb/roles?connection=lab",
    body: { roles: [{ role: "fin", access: "rw" }] },
  });
  assert.equal(roles.$root.open, false);
  assert.equal(toasts[0].html, "Roles of <b>kb</b> saved");
});

test("the delete dialog names the collection and its points, then deletes", async () => {
  const { doc, deps, requests, toasts } = world();
  const remove = component(createDeleteCollection, deps);
  remove.open({ name: "a b", points: 1200 });
  remove.typed = "x";
  remove.open({ name: "a b", points: 1200 });
  assert.equal(remove.typed, "", "the confirmation starts empty");

  const title = doc.element("span");
  remove.markTitle(title);
  assert.equal(title.textContent, "Delete a b?");
  const points = doc.element("span");
  remove.markPoints(points);
  assert.equal(points.textContent, "with its 1200 points");
  await remove.submit();

  assert.deepEqual(requests[0], { method: "DELETE", url: "/api/v1/rag/collections/a%20b?connection=lab", body: undefined });
  assert.equal(toasts[0].html, "Collection <b>a b</b> deleted");
});

test("the operator grant is created, or the button comes back with the reason", async () => {
  const { deps, requests, answers, toasts, swaps } = world();
  const button = { disabled: false };
  answers.push({ ok: false, error: "<down>" });
  await createOperatorGrant(button, deps);
  assert.equal(button.disabled, false);
  assert.deepEqual(toasts[0], { type: "error", html: "The grant was not created: &lt;down&gt;" });

  await createOperatorGrant(button, deps);
  assert.equal(requests[1].url, "/api/v1/rag/collections/operator-grant?connection=lab");
  assert.equal(button.disabled, true);
  assert.equal(swaps.length, 1);
});

test("a list that could not be loaded says why, escaped", () => {
  const target = { id: "collection-list", innerHTML: "" };
  assert.equal(showListError({ target, xhr: { status: 404, responseText: "not json" } }, TEXTS), true);
  assert.match(target.innerHTML, /The list could not be loaded\./);
  assert.equal(showListError({ target, xhr: { status: 401 } }, TEXTS), false);
});

test("the page's listeners: New collection, Create grant, the selector and its form", async () => {
  const { doc, deps, requests } = world();
  const modal = doc.element("dialog", { id: "create-collection-modal" });
  modal.showModal = () => { modal.opened = true; };
  const urls = [];
  const win = { history: { replaceState: (_s, _t, url) => urls.push(url) } };
  installCollectionListeners(win, doc, deps);

  const add = doc.element("button", { "data-collection-new": "" });
  doc.fire("click", { target: doc.element("svg", {}, add) });
  assert.equal(modal.opened, true);

  const grant = doc.element("button", { "data-collection-grant": "" });
  doc.fire("click", { target: grant });
  await Promise.resolve();
  assert.equal(requests[0].url, "/api/v1/rag/collections/operator-grant?connection=lab");

  doc.fire("change", { target: doc.getElementById("collection-connection") });
  assert.deepEqual(urls, ["/collections?connection=default"]);

  let prevented = false;
  doc.fire("submit", { target: { id: "collection-connection-form" }, preventDefault: () => { prevented = true; } });
  assert.equal(prevented, true);
});
