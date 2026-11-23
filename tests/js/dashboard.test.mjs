// Unit tests of the dashboard editor (src/app/static/js/pages/dashboard.js, node --test).
//
// The store takes the page's browser parts as arguments, so these tests hand it stand-ins:
// a recording htmx, fetch, toast and confirm, a manual clock and a small document.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createTileEditor,
  createTilesStore,
  EDITOR_ISLAND,
} from "../../src/app/static/js/pages/dashboard.js";

const TEXTS = {
  whereGroup: "group {group}",
  whereTile: "group {group}, tile {tile}",
  applicationOne: "{count} application",
  applicationOther: "{count} applications",
  widgetOne: "{count} widget",
  widgetOther: "{count} widgets",
  newGroup: "New group",
  nameCopy: "{name} copy",
  nameMissing: "give the tile a name",
  widgetGone: "this widget does not exist any more; remove it instead",
  titleTooLong: "keep the title to 80 characters",
  rawReadFailed: "Could not read tiles.yaml:",
  rawSaved: "tiles.yaml saved.",
  saveConflict: "tiles.yaml changed on disk since the editor opened.",
  saveFailed: "Save failed:",
  saved: "Overview saved.",
  discardConfirm: "Discard all unsaved dashboard changes?",
  leaveConfirm: "Leave the editor and discard all unsaved changes?",
  deleteGroupOne: "Delete the group and its {count} tile?",
  deleteGroupOther: "Delete the group and its {count} tiles?",
  addLinkTo: "+ Add link to “{name}”",
  unsavedOne: '<span class="font-semibold">{count}</span> unsaved change',
  unsavedOther: '<span class="font-semibold">{count}</span> unsaved changes',
  undoRemoved: "Undo: removed “{tiles}”",
  refreshesEvery: "Refreshes about every {seconds} s",
  noWidget: "No widget with this id exists.",
};

const DRAFT = {
  groups: [
    {
      name: "Apps",
      tiles: [
        { name: "LibreChat", href: "https://chat.example.test", visibility: "all" },
        { name: "Keycloak", href: "https://kc.example.test", visibility: "admin", icon: "/k.svg" },
      ],
    },
    {
      name: "State",
      tiles: [{ type: "widget", id: "w1", widget: "stack", title: "", size: "m" }],
    },
  ],
};
const WIDGET_TYPES = [
  { id: "stack", name: "Stack", description: "Containers", icon: "server", size: "s", visibility: "admin", available: true, refresh: 30 },
  { id: "gone", name: "Gone", description: "Not here", icon: "x", size: "s", visibility: "admin", available: false, reason: "no add-on" },
];

function clock() {
  let next = 1;
  const timers = new Map();
  return {
    setTimeout(fn, ms) {
      const id = next++;
      timers.set(id, { fn, ms });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    pending() {
      return [...timers.values()].map((t) => t.ms);
    },
    runAll() {
      const due = [...timers.entries()];
      timers.clear();
      due.forEach(([, t]) => t.fn());
    },
  };
}

class Node {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.textContent = "";
    this.className = "";
  }

  replaceChildren(...nodes) {
    this.children = nodes;
  }
}

function fakeDocument(elements = {}) {
  return {
    getElementById: (id) => elements[id] || null,
    querySelectorAll: () => [],
    querySelector: () => null,
    createElement: (tag) => new Node(tag),
    createTextNode: (text) => ({ text }),
  };
}

function response(status, body) {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: () => null },
    json: async () => body,
  };
}

function setup({ answers = [], confirmAnswer = true, elements = {} } = {}) {
  const calls = { htmx: [], fetch: [], toasts: [], confirms: [], guarded: [] };
  const queue = [...answers];
  const timers = clock();
  const store = createTilesStore({
    texts: TEXTS,
    doc: fakeDocument(elements),
    fetchImpl: async (url, options) => {
      calls.fetch.push({ url, options });
      return queue.shift();
    },
    htmx: { ajax: (...args) => calls.htmx.push(args) },
    showToast: (type, html) => calls.toasts.push([type, html]),
    authGuard: (r) => {
      calls.guarded.push(r.status);
      return r.status === 401;
    },
    confirm: (message) => {
      calls.confirms.push(message);
      return confirmAnswer;
    },
    csrf: () => "csrf-1",
    clock: timers,
  });
  store.load(DRAFT, "rev-1", ["PAPAIA_HOST"], WIDGET_TYPES, { server: "M1", alert: "A1" }, { librechat: "/l.svg" });
  return { store, calls, timers };
}

const names = (store) => store.groups.map((g) => [g.name, g.tiles.map((t) => t.name || t.widget)]);

test("the draft loads with client ids and normalised tiles", () => {
  const { store } = setup();
  assert.deepEqual(names(store), [["Apps", ["LibreChat", "Keycloak"]], ["State", ["stack"]]]);
  const ids = store.groups.flatMap((g) => [g._id, ...g.tiles.map((t) => t._id)]);
  assert.equal(new Set(ids).size, ids.length);
  assert.equal(store.groups[1].tiles[0].visibility, "admin");
  assert.equal(store.groups[0].tiles[0].icon, "");
  assert.equal(store.revision, "rev-1");
  assert.equal(store.dirty, 0);
});

test("the editor component hands the island's draft to the store", () => {
  let loaded = null;
  const island = { draft: DRAFT, revision: "r", linkKeys: ["K"], widgetTypes: [], widgetIcons: {}, defaultIcons: {} };
  const doc = {
    getElementById: (id) => id === EDITOR_ISLAND
      ? { tagName: "SCRIPT", getAttribute: () => "application/json", textContent: JSON.stringify(island) }
      : null,
  };
  const editor = createTileEditor({ doc });
  editor.$store = { tiles: { load: (...args) => { loaded = args; } } };
  editor.init();
  assert.deepEqual(loaded, [DRAFT, "r", ["K"], [], {}, {}]);
});

test("the layout effect reads groups and preview and re-creates drag and drop after the tick", () => {
  const reads = [];
  let made = 0;
  const tiles = {
    get groups() { reads.push("groups"); return []; },
    get preview() { reads.push("preview"); return false; },
    initSortable() { made += 1; },
  };
  const editor = createTileEditor({ doc: fakeDocument() });
  let tick = null;
  editor.$store = { tiles };
  editor.$nextTick = (fn) => { tick = fn; };
  editor.layout();
  assert.deepEqual(reads, ["groups", "preview"]);
  assert.equal(made, 0);
  tick();
  assert.equal(made, 1);
});

test("gallery and editor are swapped through htmx", () => {
  const { store, calls } = setup();
  store.enterEdit();
  store.exitEdit();
  assert.deepEqual(calls.htmx, [
    ["GET", "/partials/tiles/edit", { target: "#tile-gallery", swap: "innerHTML" }],
    ["GET", "/partials/tiles", { target: "#tile-gallery", swap: "innerHTML" }],
  ]);
});

test("groups are added, renamed, moved and deleted after a confirmation", () => {
  const { store, calls } = setup();
  store.addGroup();
  assert.equal(store.groups[2].name, "New group");
  store.renameGroup(store.groups[2], "Docs");
  store.moveGroup(2, -1);
  assert.deepEqual(names(store).map(([n]) => n), ["Apps", "Docs", "State"]);
  store.moveGroup(0, -1);
  assert.equal(store.dirty, 3);
  store.confirmDeleteGroup(0);
  assert.deepEqual(calls.confirms, ["Delete the group and its 2 tiles?"]);
  assert.deepEqual(names(store).map(([n]) => n), ["Docs", "State"]);
});

test("a group is kept when the confirmation is declined", () => {
  const { store, calls } = setup({ confirmAnswer: false });
  store.confirmDeleteGroup(1);
  assert.deepEqual(calls.confirms, ["Delete the group and its 1 tile?"]);
  assert.equal(store.groups.length, 2);
  assert.equal(store.dirty, 0);
});

test("tiles move inside and between groups, and duplicates are named copies", () => {
  const { store } = setup();
  store.moveTile(0, 0, 1);
  assert.deepEqual(names(store)[0][1], ["Keycloak", "LibreChat"]);
  store.moveTile(0, 1, 1);
  store.moveTileToGroup(0, 0, 1);
  assert.deepEqual(names(store), [["Apps", ["LibreChat"]], ["State", ["stack", "Keycloak"]]]);
  store.duplicateTile(0, 0);
  store.duplicateTile(1, 0);
  assert.equal(store.groups[0].tiles[1].name, "LibreChat copy");
  assert.equal(store.groups[1].tiles[1].title, "Stack copy");
  assert.equal(store.groups[1].tiles[1].id, "");
  assert.equal(store.dirty, 4);
});

test("a removed tile can be brought back until the undo strip times out", () => {
  const { store, timers } = setup();
  store.removeTile(0, 0);
  assert.equal(store.undoLabel(), "Undo: removed “LibreChat”");
  assert.deepEqual(timers.pending(), [8000]);
  store.undoRemove();
  assert.deepEqual(names(store)[0][1], ["LibreChat", "Keycloak"]);
  assert.equal(store.dirty, 0);
  store.removeTile(1, 0);
  assert.equal(store.removedName(), "Stack");
  timers.runAll();
  assert.equal(store.removed, null);
  assert.equal(store.dirty, 1);
});

test("labels, counts and classes come from the store", () => {
  const { store } = setup();
  assert.equal(store.groupCount(store.groups[0]), "2 applications");
  assert.equal(store.groupCount(store.groups[1]), "1 widget");
  assert.equal(store.addLinkLabel(store.groups[0]), "+ Add link to “Apps”");
  assert.equal(store.initial(""), "?");
  assert.equal(store.initial("Keycloak"), "K");
  assert.equal(store.iconFor(store.groups[0].tiles[0]), "/l.svg");
  // The enforced policy loads images from this site only: an icon elsewhere is never put
  // into an <img>; the bundled icon or the badge stands in, as on the dashboard.
  assert.equal(store.iconFor({ name: "LibreChat", icon: "https://example.com/icon.svg" }), "/l.svg");
  assert.equal(store.iconFor({ name: "Wiki", icon: "https://example.com/icon.svg" }), "");
  assert.equal(store.iconFor({ name: "Wiki", icon: "//cdn.example.test/i.svg" }), "");
  assert.equal(store.iconFor({ name: "Wiki", icon: "/own/w.svg" }), "/own/w.svg");
  assert.equal(store.iconFor({ name: "Wiki", icon: "data:image/png;base64,AA==" }), "data:image/png;base64,AA==");
  assert.equal(store.iconFor({ name: "Wiki", icon: "data:text/html,x" }), "");
  assert.equal(store.widgetTileIcon(store.groups[1].tiles[0]), "M1");
  assert.equal(store.widgetDescription(store.groups[1].tiles[0]), "Containers");
  assert.equal(store.widgetDescription({ widget: "nope" }), "No widget with this id exists.");
  assert.equal(store.cardClass(store.groups[1].tiles[0]),
    "cursor-pointer hover:border-base-content/25 transition-colors sm:col-span-2");
  store.togglePreview();
  assert.equal(store.cardClass(store.groups[0].tiles[0]), "");
  assert.equal(store.tileVisible(store.groups[0].tiles[1]), false);
  assert.equal(store.groupVisible(store.groups[1]), false);
});

test("the unsaved count is built as nodes, the number in its bold mark", () => {
  const { store } = setup();
  const element = new Node("span");
  store.touch();
  store.showDirty(element);
  assert.deepEqual(element.children.map((n) => n.text ?? [n.tagName, n.className, n.textContent]),
    ["", ["span", "font-semibold", "1"], " unsaved change"]);
  store.touch();
  store.showDirty(element);
  assert.deepEqual(element.children.map((n) => n.text ?? n.textContent), ["", "2", " unsaved changes"]);
});

test("an unsaved sentence without the mark is shown as text, its markup dropped", () => {
  const texts = { ...TEXTS, unsavedOther: "<b>{count}</b> offen" };
  const store = createTilesStore({ texts, doc: fakeDocument(), clock: clock() });
  const element = new Node("span");
  store.dirty = 3;
  store.showDirty(element);
  assert.deepEqual(element.children, [{ text: "3 offen" }]);
});

test("a click on a card opens it, except in the preview", () => {
  const { store } = setup();
  store.togglePreview();
  store.clickTile(0, 0);
  assert.equal(store.dialog, null);
  store.togglePreview();
  store.clickTile(0, 0);
  assert.equal(store.dialog.tile.name, "LibreChat");
});

test("the tile dialog checks the name and applies to the draft", async () => {
  const { store, calls, timers } = setup({
    answers: [response(200, [{ ok: true, resolved: "https://papaia.example.test/x" }, { ok: true }])],
  });
  store.newTile(0);
  store.applyTile();
  assert.equal(store.dialog.nameProblem, "give the tile a name");
  store.dialog.tile.name = "Docs";
  store.dialog.tile.href = "/x";
  store.insertPlaceholder("PAPAIA_HOST");
  assert.equal(store.dialog.tile.href, "{{PAPAIA_HOST}}/x");
  timers.runAll();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls.fetch[0].url, "/api/v1/tiles/resolve");
  assert.equal(calls.fetch[0].options.headers["X-CSRF-Token"], "csrf-1");
  assert.deepEqual(JSON.parse(calls.fetch[0].options.body), { values: ["{{PAPAIA_HOST}}/x"], icons: [""] });
  assert.equal(store.dialog.resolved, "https://papaia.example.test/x");
  store.setTileVisibility("admin");
  store.applyTile();
  assert.equal(store.dialog, null);
  assert.deepEqual(names(store)[0][1], ["LibreChat", "Keycloak", "Docs"]);
  assert.equal(store.groups[0].tiles[2].visibility, "admin");
});

test("the widget dialog picks an available type and places it", () => {
  const { store, calls, timers } = setup({ elements: { "widget-preview": {} } });
  store.newWidget(0);
  store.widgetDialog.query = "gone";
  assert.deepEqual(store.widgetChoices().map((t) => t.id), ["gone"]);
  store.pickWidget(WIDGET_TYPES[1]);
  assert.equal(store.widgetDialog.step, "pick");
  store.pickWidget(WIDGET_TYPES[0]);
  assert.equal(store.widgetDialog.step, "configure");
  store.widgetDialog.tile.title = "Containers here";
  store.setWidgetSize("l");
  store.setWidgetVisibility("all");
  assert.equal(store.refreshLabel(), "Refreshes about every 30 s");
  timers.runAll();
  assert.deepEqual(calls.htmx.at(-1),
    ["GET", "/partials/widgets/preview/stack?title=Containers%20here", { target: "#widget-preview", swap: "innerHTML" }]);
  store.applyWidget();
  assert.equal(store.widgetDialog, null);
  const tile = store.groups[0].tiles[2];
  assert.deepEqual([tile.type, tile.widget, tile.size, tile.visibility, tile.title],
    ["widget", "stack", "l", "all", "Containers here"]);
});

test("a widget title over 80 characters and a missing type are refused", () => {
  const { store } = setup();
  store.openTile(1, 0);
  store.widgetDialog.tile.title = "x".repeat(81);
  store.applyWidget();
  assert.equal(store.widgetDialog.problem, "keep the title to 80 characters");
  store.widgetDialog.type = null;
  store.applyWidget();
  assert.equal(store.widgetDialog.problem, "this widget does not exist any more; remove it instead");
});

test("save sends the whole draft and returns to the gallery", async () => {
  const { store, calls } = setup({ answers: [response(200, { revision: "rev-2" })] });
  store.touch();
  await store.save();
  const sent = calls.fetch[0];
  assert.equal(sent.url, "/api/v1/tiles");
  assert.equal(sent.options.method, "PUT");
  assert.equal(sent.options.headers["X-CSRF-Token"], "csrf-1");
  assert.deepEqual(JSON.parse(sent.options.body), {
    revision: "rev-1",
    version: 1,
    groups: [
      { name: "Apps", tiles: [
        { name: "LibreChat", href: "https://chat.example.test", description: "", icon: null, visibility: "all" },
        { name: "Keycloak", href: "https://kc.example.test", description: "", icon: "/k.svg", visibility: "admin" },
      ] },
      { name: "State", tiles: [
        { type: "widget", id: "w1", widget: "stack", title: "", size: "m", visibility: "admin" },
      ] },
    ],
  });
  assert.equal(store.revision, "rev-2");
  assert.equal(store.dirty, 0);
  assert.equal(store.busy, false);
  assert.deepEqual(calls.toasts, [["success", "Overview saved."]]);
  assert.deepEqual(calls.htmx, [["GET", "/partials/tiles", { target: "#tile-gallery", swap: "innerHTML" }]]);
});

test("a conflict and a refusal are told, the server's words escaped", async () => {
  const { store, calls } = setup({
    answers: [
      response(409, { detail: "changed" }),
      response(422, { detail: [{ group: 0, tile: 1, message: "<b>bad</b> link" }] }),
      response(401, {}),
    ],
  });
  await store.save();
  await store.save();
  await store.save();
  assert.deepEqual(calls.toasts, [
    ["error", "tiles.yaml changed on disk since the editor opened."],
    ["error", "Save failed: &lt;b&gt;bad&lt;/b&gt; link (group 1, tile 2)"],
  ]);
  assert.deepEqual(calls.guarded, [409, 422, 401]);
  assert.deepEqual(calls.htmx, []);
  assert.equal(store.busy, false);
});

test("the raw file is read, refused saves stay in the dialog, a saved file reloads the editor", async () => {
  const { store, calls } = setup({
    answers: [
      response(200, { yaml: "version: 1\n", revision: "rev-1" }),
      response(422, { detail: "not a mapping" }),
      response(200, { revision: "rev-3" }),
      response(500, { detail: "<i>disk</i>" }),
    ],
  });
  await store.openRaw();
  assert.deepEqual(store.raw, { text: "version: 1\n", revision: "rev-1", problem: null });
  await store.applyRaw();
  assert.equal(store.raw.problem, "not a mapping");
  await store.applyRaw();
  assert.equal(store.raw, null);
  assert.deepEqual(calls.toasts, [["success", "tiles.yaml saved."]]);
  assert.deepEqual(calls.htmx, [["GET", "/partials/tiles/edit", { target: "#tile-gallery", swap: "innerHTML" }]]);
  await store.openRaw();
  assert.deepEqual(calls.toasts.at(-1), ["error", "Could not read tiles.yaml: &lt;i&gt;disk&lt;/i&gt;"]);
});

test("discard and leave ask only when there are unsaved changes", () => {
  const { store, calls } = setup({ confirmAnswer: false });
  store.leave();
  assert.deepEqual(calls.htmx.length, 1);
  store.touch();
  store.discard();
  store.leave();
  assert.deepEqual(calls.confirms, [
    "Discard all unsaved dashboard changes?",
    "Leave the editor and discard all unsaved changes?",
  ]);
  assert.equal(calls.htmx.length, 1);
});

test("dialogs closed by Escape or the backdrop clear their state", () => {
  const { store } = setup();
  store.newTile(0);
  store.tileDialogClosed();
  store.newWidget(0);
  store.widgetDialogClosed();
  store.raw = { text: "", revision: "", problem: null };
  store.rawDialogClosed();
  assert.deepEqual([store.dialog, store.widgetDialog, store.raw], [null, null, null]);
});

test("drag and drop is skipped without Sortable and in the preview", () => {
  const made = [];
  class FakeSortable {
    constructor(el, options) {
      made.push(options.draggable);
      this.destroyed = false;
    }

    destroy() {
      this.destroyed = true;
    }
  }
  const lists = [{ dataset: {} }];
  const doc = {
    ...fakeDocument(),
    querySelectorAll: (sel) => (sel === "[data-tile-list]" ? lists : []),
    querySelector: (sel) => (sel === "[data-group-list]" ? {} : null),
  };
  const store = createTilesStore({ texts: TEXTS, doc, clock: clock(), Sortable: FakeSortable });
  store.initSortable();
  assert.deepEqual(made, ["[data-tile-id]", "[data-group-id]"]);
  store.preview = true;
  store.initSortable();
  assert.equal(made.length, 2);
  const without = createTilesStore({ texts: TEXTS, doc, clock: clock() });
  without.initSortable();
});
