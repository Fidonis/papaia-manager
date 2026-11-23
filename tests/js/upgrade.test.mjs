// The release upgrade (pages/upgrade/component.js) without a browser: the sentences of the
// dialog become DOM nodes with text, never markup; the requests carry the CSRF token and the
// body they had; the toasts get escaped text; the header cluster takes over what its inline
// expressions did; the partials announce their state; the buttons of the partials act through
// `data-upgrade-action`.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createUpgradeActions,
  createUpgradePage,
  fillSentence,
} from "../../src/app/static/js/pages/upgrade/component.js";

const T = {
  upgradeTitle: "Upgrade to {target}?",
  upgradeIntro:
    'papAIa moves from <span class="font-mono">{current}</span> to <span class="font-mono">{target}</span>. It runs on.',
  upgradeCheckout: 'The checkout moves to <span class="font-mono">{target}</span>',
  upgradeLogs: 'Read it with <span class="font-mono">docker logs papaia-upgrade-{target}</span>.',
  install: "Install {version}",
  dismissFirst: "Dismiss the previous upgrade first",
  checkFailed: "Check failed",
  upToDate: "Up to date",
  checkFailedBecause: "Upgrade check failed:",
  checkUnreachable: "Upgrade check failed: the manager could not be reached",
  refused: "Upgrade refused:",
  started: "Upgrade started — this panel will go down with the stack",
  dismissFailed: "Could not dismiss the upgrade:",
  dismissedImagesFailed: "Dismissed, but outdated images could not be removed:",
  dismissedImagesOne: "Dismissed; removed {count} more image, {size} reclaimed",
  dismissedImagesOther: "Dismissed; removed {count} more images, {size} reclaimed",
  removeSelectedCount: "Remove selected ({count})",
  removeSelected: "Remove selected",
  removeTitleOne: "Remove {count} image?",
  removeTitleOther: "Remove {count} images?",
  removeFailed: "Could not remove images:",
  removedPartly: "Removed {removed}, kept {kept}: {name} — {error}",
  removedOne: "Removed {count} image, {size} reclaimed",
  removedOther: "Removed {count} images, {size} reclaimed",
};

// -- a small DOM -----------------------------------------------------------------------------

class FakeNode {
  constructor(doc, tag, attrs = {}) {
    this.ownerDocument = doc;
    this.tagName = tag;
    this.children = [];
    this.attrs = { ...attrs };
    this.dataset = {};
    this.className = attrs.class || "";
    this.classList = { contains: (name) => this.className.split(/\s+/).includes(name) };
    this.text = "";
    this.checked = false;
    this.indeterminate = false;
    this.disabled = false;
    this.value = attrs.value || "";
    this.id = attrs.id || "";
    this.open = false;
  }
  get textContent() {
    return this.tagName === "#text" ? this.text : this.text + this.children.map((c) => c.textContent).join("");
  }
  set textContent(value) {
    this.text = String(value);
    this.children = [];
  }
  replaceChildren(...nodes) {
    this.text = "";
    this.children = nodes;
  }
  appendChild(node) {
    this.children.push(node);
  }
  getAttribute(name) {
    return this.attrs[name] ?? null;
  }
  showModal() {
    this.open = true;
  }
  close() {
    this.open = false;
  }
}

function makeDoc() {
  const doc = {
    byId: {},
    boxes: [],
    createElement: (tag) => new FakeNode(doc, tag),
    createTextNode: (text) => {
      const node = new FakeNode(doc, "#text");
      node.text = text;
      return node;
    },
    getElementById: (id) => doc.byId[id] || null,
    querySelector: (selector) => (selector === "input[name=pk_csrf]" ? { value: "csrf-of-the-session" } : null),
    querySelectorAll(selector) {
      if (selector === ".prune-image-check") return doc.boxes;
      if (selector === ".prune-image-check:checked") return doc.boxes.filter((b) => b.checked);
      return [];
    },
  };
  for (const id of ["prune-images-modal", "prune-images-title", "prune-images-list", "prune-selected-button", "prune-master-check"]) {
    doc.byId[id] = new FakeNode(doc, "div", { id });
  }
  for (const [id, name] of [["sha256:a", "postgres:15"], ["sha256:b", "redis:<6>"]]) {
    doc.boxes.push(new FakeNode(doc, "input", { value: id, "data-name": name, class: "prune-image-check" }));
  }
  return doc;
}

function reply(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function setup(answer = () => reply(200, {})) {
  const doc = makeDoc();
  const sent = [];
  const toasts = [];
  const swaps = [];
  let reloaded = 0;
  const page = createUpgradePage(T, {
    doc,
    fetchImpl: async (url, options) => {
      sent.push({ url, options });
      return answer(url, options);
    },
    htmx: () => ({ ajax: (method, url, options) => swaps.push([url, options.target]) }),
    toast: (type, html) => toasts.push([type, html]),
    authGuard: (r) => r.status === 401,
    reload: () => { reloaded += 1; },
  });
  return { doc, page, sent, toasts, swaps, reloaded: () => reloaded };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const click = (action) => ({ target: { closest: () => (action ? { dataset: { upgradeAction: action } } : null) } });

// -- the sentences ---------------------------------------------------------------------------

test("a sentence becomes text and mono spans, values as text", () => {
  const doc = makeDoc();
  const element = new FakeNode(doc, "span");
  fillSentence(element, T.upgradeIntro, { current: "1.1.0", target: "<b>1.2.0</b>" });
  assert.deepEqual(element.children.map((c) => [c.tagName, c.className, c.textContent]), [
    ["#text", "", "papAIa moves from "],
    ["span", "font-mono", "1.1.0"],
    ["#text", "", " to "],
    ["span", "font-mono", "<b>1.2.0</b>"],
    ["#text", "", ". It runs on."],
  ]);
  fillSentence(element, T.upgradeLogs, { target: "1.2.0" });
  assert.equal(element.children[1].textContent, "docker logs papaia-upgrade-1.2.0");
  // Any other markup stays text.
  fillSentence(element, "<img src=x> {target}", { target: "1.2.0" });
  assert.deepEqual(element.children.map((c) => c.tagName), ["#text"]);
  assert.equal(element.textContent, "<img src=x> 1.2.0");
});

// -- the header cluster ----------------------------------------------------------------------

const READY = { ready: true, target: "1.2.0", checkFailed: false, forceable: false, gatePassed: true, hasBackupDir: true, current: "1.1.0" };

function cluster(page) {
  const actions = createUpgradeActions(T, page);
  const modal = { open: false, showModal() { this.open = true; }, close() { this.open = false; } };
  actions.$refs = { upgradeModal: modal };
  return { actions, modal };
}

test("the cluster takes the announced state and names the button", () => {
  const { page } = setup();
  const { actions } = cluster(page);
  assert.equal(actions.checking, true);
  assert.equal(actions.blocked, true);
  assert.equal(actions.installLabel, "Up to date");
  actions.applyState(READY);
  assert.equal(actions.checking, false);
  assert.equal(actions.blocked, false);
  assert.equal(actions.installLabel, "Install 1.2.0");
  actions.applyRunner({ active: true, running: false, finished: true });
  assert.equal(actions.blocked, true);
  assert.equal(actions.installLabel, "Dismiss the previous upgrade first");
  actions.applyState({ ...READY, ready: false, target: null, checkFailed: true });
  assert.equal(actions.installLabel, "Check failed");
});

test("the dialog fills its sentences from the state", () => {
  const { page } = setup();
  const { actions } = cluster(page);
  actions.applyState(READY);
  const doc = makeDoc();
  const span = (key) => {
    const element = new FakeNode(doc, "span");
    element.dataset.sentence = key;
    actions.fill(element);
    return element;
  };
  assert.equal(span("upgradeTitle").textContent, "Upgrade to 1.2.0?");
  assert.equal(span("upgradeCheckout").children[1].textContent, "v1.2.0");
  assert.equal(span("upgradeIntro").children[1].textContent, "1.1.0");
  assert.equal(span("unknown").textContent, "");
});

test("open resets the options and run starts the upgrade with them", async () => {
  const { page, sent, toasts, swaps } = setup(() => reply(202, {}));
  const { actions, modal } = cluster(page);
  actions.applyState({ ...READY, forceable: true, gatePassed: false, hasBackupDir: false });
  actions.force = true;
  actions.confirmed = true;
  actions.open();
  assert.equal(modal.open, true);
  assert.deepEqual([actions.force, actions.noBackup, actions.pruneImages, actions.confirmed], [false, true, true, false]);
  actions.run();
  assert.equal(modal.open, false);
  assert.equal(actions.starting, true);
  await settle();
  assert.equal(sent[0].url, "/api/v1/upgrade");
  assert.equal(sent[0].options.headers["X-CSRF-Token"], "csrf-of-the-session");
  assert.deepEqual(JSON.parse(sent[0].options.body), { version: "1.2.0", force: false, no_backup: true, prune_images: true });
  assert.deepEqual(toasts, [["info", "Upgrade started — this panel will go down with the stack"]]);
  assert.deepEqual(swaps, [["/partials/upgrade/runner", "#upgrade-runner-slot"]]);
  // Started: the button stays busy until the runner announces itself.
  assert.equal(actions.starting, true);
});

test("a refused start frees the button and tells why, escaped", async () => {
  const { page, toasts } = setup(() => reply(409, { detail: "add-on <x> is incompatible" }));
  const { actions } = cluster(page);
  actions.applyState(READY);
  actions.run();
  await settle();
  assert.equal(actions.starting, false);
  assert.deepEqual(toasts, [["error", "Upgrade refused: add-on &lt;x&gt; is incompatible"]]);
});

test("the check button checks for the newest release and frees itself", async () => {
  const { page, sent, swaps } = setup();
  const { actions } = cluster(page);
  actions.checking = false;
  actions.check();
  assert.equal(actions.checking, true);
  await settle();
  assert.equal(actions.checking, false);
  assert.deepEqual(JSON.parse(sent[0].options.body), { version: null });
  assert.deepEqual(swaps.map((s) => s[1]), ["#upgrade-status-slot", "#upgrade-check-slot"]);
});

test("an unreachable manager is told once", async () => {
  const { page, toasts } = setup(() => { throw new Error("offline"); });
  assert.equal(await page.runCheck(null), false);
  assert.deepEqual(toasts, [["error", "Upgrade check failed: the manager could not be reached"]]);
});

test("a session end stops a request without a toast", async () => {
  const { page, toasts, swaps } = setup(() => reply(401, {}));
  assert.equal(await page.runCheck(null), false);
  assert.deepEqual(toasts, []);
  assert.deepEqual(swaps, []);
});

// -- the partials ------------------------------------------------------------------------------

test("the picker of the check partial re-runs the check for the chosen release", async () => {
  const { page, sent } = setup();
  page.handleChange({ target: { id: "upgrade-version", value: "1.3.0" } });
  await settle();
  assert.deepEqual(JSON.parse(sent[0].options.body), { version: "1.3.0" });
});

test("dismiss clears the runner and reports the second image pass", async () => {
  const { page, sent, toasts, swaps } = setup(() =>
    reply(200, { images: { removed: [{ name: "a" }], reclaimed_human: "1.0 GB" } }),
  );
  page.handleClick(click("dismiss"));
  await settle();
  assert.equal(sent[0].url, "/api/v1/upgrade/runner/clear");
  assert.deepEqual(toasts, [["success", "Dismissed; removed 1 more image, 1.0 GB reclaimed"]]);
  assert.deepEqual(swaps.map((s) => s[1]), [
    "#upgrade-runner-slot", "#upgrade-status-slot", "#upgrade-check-slot", "#upgrade-images-slot", "#upgrade-log-slot",
  ]);
});

test("reload and the refresh of the images act through data-upgrade-action", () => {
  const { page, swaps, reloaded } = setup();
  for (const action of ["reload", "refresh-images", "unknown", null]) page.handleClick(click(action));
  assert.equal(reloaded(), 1);
  assert.deepEqual(swaps, [["/partials/upgrade/images", "#upgrade-images-slot"]]);
});

test("the selection keeps the bulk button and the master box honest", () => {
  const { doc, page } = setup();
  const [first, second] = doc.boxes;
  first.checked = true;
  page.handleChange({ target: first });
  const button = doc.getElementById("prune-selected-button");
  const master = doc.getElementById("prune-master-check");
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, "Remove selected (1)");
  assert.equal(master.indeterminate, true);
  page.handleChange({ target: { id: "prune-master-check", checked: true } });
  assert.equal(second.checked, true);
  assert.equal(master.checked, true);
  assert.equal(master.indeterminate, false);
  page.handleChange({ target: { id: "prune-master-check", checked: false } });
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, "Remove selected");
});

test("the dialog lists the chosen images as text and removes exactly those", async () => {
  const { doc, page, sent, toasts } = setup(() =>
    reply(200, { removed: [{}], failed: [{ name: "redis:<6>", error: "in <use>" }], reclaimed_human: "1 GB" }),
  );
  doc.boxes[1].checked = true;
  page.handleClick(click("prune-selected"));
  const modal = doc.getElementById("prune-images-modal");
  assert.equal(modal.open, true);
  assert.equal(doc.getElementById("prune-images-title").textContent, "Remove 1 image?");
  assert.deepEqual(doc.getElementById("prune-images-list").children.map((li) => li.textContent), ["redis:<6>"]);
  page.handleClick(click("prune-cancel"));
  assert.equal(modal.open, false);
  page.handleClick(click("prune-all"));
  assert.equal(doc.getElementById("prune-images-title").textContent, "Remove 2 images?");
  page.handleClick(click("prune-confirm"));
  assert.equal(modal.open, false);
  await settle();
  assert.deepEqual(JSON.parse(sent[0].options.body), { images: ["sha256:a", "sha256:b"] });
  assert.deepEqual(toasts, [["warning", "Removed 1, kept 1: redis:&lt;6&gt; — in &lt;use&gt;"]]);
});

test("the partials announce their state to the header cluster", () => {
  const { page } = setup();
  const events = [];
  class FakeEvent {
    constructor(name, init) {
      this.type = name;
      this.detail = init.detail;
    }
  }
  const win = { CustomEvent: FakeEvent, dispatchEvent: (e) => events.push([e.type, e.detail]) };
  const island = { textContent: '{"ready": true, "target": "1.2.0"}' };
  const runner = { dataset: { active: "true", running: "false", finished: "true" } };
  const root = {
    matches: () => false,
    querySelectorAll: (selector) => (selector === "#upgrade-state" ? [island] : [runner]),
  };
  page.announce(root, win);
  assert.deepEqual(events, [
    ["papaia-upgrade-state", { ready: true, target: "1.2.0" }],
    ["papaia-upgrade-runner", { active: true, running: false, finished: true }],
  ]);
  page.announce(null, win);
  assert.equal(events.length, 2);
});
