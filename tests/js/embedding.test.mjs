// Unit tests of the Embedding page (src/app/static/js/pages/embedding.js, node --test).
//
// The component takes the page's browser parts as arguments, so these tests hand it
// stand-ins: a recording htmx, ragRequest, fetch and toast, and a small document.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createEmbedPage,
  installEmbeddingListeners,
  showBodyError,
} from "../../src/app/static/js/pages/embedding.js";
import { MiniDocument } from "./fixtures/mini-dom.mjs";

const TEXTS = {
  loadFailed: "The page could not be loaded.",
  unavailable: "Embedding is not available",
  reloadPage: "Reload the page",
  replaceTitle: 'Replace <span class="font-mono">{collection}</span>?',
  points: "({points} points)",
  discardWhat: '<span class="font-semibold">{name}</span> from {owner}: {files} file',
  uploading: "Uploading {current} of {total}",
  interrupted: "{path}: the connection was interrupted",
  discarded: "Upload discarded",
  startedOne: "Embedding started for {count} file",
  startedOther: "Embedding started for {count} files",
  abortFailed: "The run was not aborted:",
  abortRequested: "Abort requested.",
};
const STATE = {
  collection: "kb",
  points: 12,
  model: "nomic-embed-text",
  supportsAdd: true,
  ready: true,
  otherJobs: ["nightly"],
  activeRun: "",
};

function dialog() {
  return {
    open: false,
    showModal() { this.open = true; },
    close() { this.open = false; },
  };
}

function world(overrides = {}) {
  const doc = new MiniDocument();
  const form = doc.element("form", { id: "embed-connection-form" });
  doc.element("select", { id: "embed-connection", value: "default" }, form);
  doc.element("div", { id: "embed-batch-tree" });
  const swaps = [];
  const toasts = [];
  const requests = [];
  const answers = [];
  const page = createEmbedPage({
    texts: TEXTS,
    doc,
    htmx: { ajax: (method, url, options) => swaps.push({ method, url, ...options }) },
    ragRequest: async (method, url, body) => {
      requests.push({ method, url, body });
      return answers.shift() || { ok: true, data: null };
    },
    ragError: async (response) => `refused ${response.status}`,
    authGuard: (response) => response.status === 401,
    showToast: (type, html) => toasts.push({ type, html }),
    csrf: () => "csrf-1",
    fetchImpl: overrides.fetchImpl || (async () => ({ ok: true, status: 200 })),
    readState: () => STATE,
  });
  page.$refs = { confirmDialog: dialog(), discardDialog: dialog() };
  page.init();
  return { doc, page, swaps, toasts, requests, answers };
}

test("the body hands its state over through the island", () => {
  const { page } = world();
  page.mode = "add";
  page.batch = "old";
  page.selected = { a: true };
  page.loaded();

  assert.equal(page.collection, "kb");
  assert.equal(page.recorded, "nomic-embed-text");
  assert.equal(page.model, "nomic-embed-text");
  assert.deepEqual(page.otherJobs, ["nightly"]);
  assert.equal(page.otherJobList(), "nightly");
  assert.equal(page.batch, "");
  assert.deepEqual(page.selected, {});
  assert.equal(page.runActive, false);
});

test("an ingester without add switches to replace, an active run is taken over", () => {
  const { page } = world();
  page.loaded({ ...STATE, supportsAdd: false, activeRun: "run-1" });

  assert.equal(page.mode, "replace");
  assert.equal(page.runId, "run-1");
  assert.equal(page.runActive, true);
});

test("a selected folder covers what is below it", () => {
  const { page } = world();
  page.toggle("docs/a.md", true);
  page.toggle("docs", true);

  assert.deepEqual(page.paths(), ["docs"]);
  assert.equal(page.covered("docs/a.md"), true);
  assert.equal(page.covered("other"), false);
  assert.equal(page.isOn("docs"), true);
  page.toggle("docs", false);
  assert.deepEqual(page.paths(), []);
});

test("starting needs a model, a collection and a selection", () => {
  const { page } = world();
  page.loaded();
  page.tab = "folder";
  assert.equal(page.canStart(), false);
  page.toggle("", true);
  assert.equal(page.canStart(), true);
  page.model = "  ";
  assert.equal(page.canStart(), false);
});

test("the sentences with markup are built as nodes, with the values as text", () => {
  const { doc, page } = world();
  page.loaded({ ...STATE, collection: "<b>kb</b>" });
  const title = doc.element("span");
  page.markReplaceTitle(title);
  assert.equal(title.textContent, "Replace <b>kb</b>?");
  assert.equal(title.childNodes[1].tagName, "SPAN");
  assert.equal(title.childNodes[1].childNodes.length, 1);

  const points = doc.element("span");
  page.markPoints(points);
  assert.equal(points.textContent, "(12 points)");

  page.discardItem = { id: "u1", name: "Q3", owner: "anna", files: 2, size: "3 KB" };
  const what = doc.element("span");
  page.markDiscard(what);
  assert.equal(what.textContent, "Q3 from anna: 2 file");
  page.discardItem = null;
  page.markDiscard(what);
  assert.equal(what.textContent, " from :  file");
});

test("a replace asks first, then posts the run and shows its status", async () => {
  const { page, requests, swaps, toasts, answers } = world();
  page.loaded();
  page.tab = "folder";
  page.toggle("", true);
  page.mode = "replace";
  page.model = " bge-m3 ";
  await page.start();
  assert.equal(page.$refs.confirmDialog.open, true);
  assert.equal(requests.length, 0);

  answers.push({ ok: true, data: { run_id: "run-9", files: 3 } });
  page.confirmOthers = true;
  await page.submit();

  assert.deepEqual(requests[0], {
    method: "POST",
    url: "/api/v1/rag/ingest/runs?connection=default",
    body: {
      collection: "kb",
      mode: "replace",
      model: "bge-m3",
      source: { kind: "folder", paths: [""] },
      confirm_replace: true,
      confirm_other_jobs: true,
    },
  });
  assert.equal(page.$refs.confirmDialog.open, false);
  assert.equal(page.runId, "run-9");
  assert.equal(page.runActive, true);
  assert.deepEqual(toasts, [{ type: "success", html: "Embedding started for 3 files" }]);
  assert.equal(swaps[0].url, "/partials/embedding/status?connection=default&collection=kb&run=run-9");
  assert.equal(swaps[0].swap, "outerHTML");
  assert.equal(swaps[1].target, "#embed-uploads");
});

test("a refused run keeps the dialog's error and posts nothing else", async () => {
  const { page, answers, swaps } = world();
  page.loaded();
  answers.push({ ok: false, error: "the model does not match" });
  await page.submit();

  assert.equal(page.error, "the model does not match");
  assert.equal(page.busy, false);
  assert.equal(swaps.length, 0);
});

test("abort says what happens and escapes the server's words", async () => {
  const { page, answers, toasts } = world();
  page.loaded({ ...STATE, activeRun: "run-1" });
  answers.push({ ok: false, error: "<b>busy</b>" });
  await page.abort();
  assert.deepEqual(toasts.pop(), { type: "error", html: "The run was not aborted: &lt;b&gt;busy&lt;/b&gt;" });
  assert.equal(page.aborting, false);

  answers.push({ ok: true, data: null });
  await page.abort();
  assert.equal(page.aborting, true);
  assert.equal(toasts.pop().type, "info");
  await page.abort();
  assert.equal(toasts.length, 0, "a second click while aborting does nothing");
});

test("the end of a run drops the upload and reloads the list", () => {
  const { page, swaps } = world();
  page.loaded({ ...STATE, activeRun: "run-1" });
  page.batch = "u1";
  page.runState({ active: true });
  assert.equal(page.runActive, true);
  page.runState({ active: false });

  assert.equal(page.runActive, false);
  assert.equal(page.batch, "");
  assert.equal(swaps.at(-1).target, "#embed-uploads");
  const count = swaps.length;
  page.runState({ active: false });
  assert.equal(swaps.length, count, "an ended run is handled once");
});

test("files are uploaded one by one into a new upload, refusals are listed", async () => {
  const posted = [];
  const { page, answers, requests, swaps } = world({
    fetchImpl: async (url, options) => {
      posted.push({ url, csrf: options.headers["X-CSRF-Token"], path: options.body.get("path") });
      return posted.length === 2 ? { ok: false, status: 413 } : { ok: true, status: 201 };
    },
  });
  answers.push({ ok: true, data: { id: "u7" } });
  const input = { files: [{ name: "a.md" }, { name: "b.md", webkitRelativePath: "dir/b.md" }], value: "x" };
  await page.uploadPicked(input);

  assert.equal(input.value, "", "the picker is cleared");
  assert.equal(requests[0].url, "/api/v1/rag/ingest/uploads");
  assert.deepEqual(posted.map((p) => p.path), ["a.md", "dir/b.md"]);
  assert.equal(posted[0].url, "/api/v1/rag/ingest/uploads/u7/files");
  assert.equal(posted[0].csrf, "csrf-1");
  assert.deepEqual(page.uploadErrors, ["dir/b.md: refused 413"]);
  assert.equal(page.batch, "u7");
  assert.equal(page.uploading, false);
  assert.ok(swaps.some((s) => s.target === "#embed-batch-tree"));
});

test("an interrupted upload says which file", async () => {
  const { page } = world({
    fetchImpl: async () => {
      throw new Error("offline");
    },
  });
  page.batch = "u1";
  await page.uploadFiles([{ name: "a.md" }]);

  assert.deepEqual(page.uploadErrors, ["a.md: the connection was interrupted"]);
});

test("discarding asks in the dialog, then deletes and reloads the list", async () => {
  const { page, requests, toasts } = world();
  page.batch = "u1";
  page.askDiscard({ id: "u1", name: "Q3", owner: "anna", files: 1, size: "1 KB" });
  assert.equal(page.$refs.discardDialog.open, true);
  await page.confirmDiscard();

  assert.deepEqual(requests[0], { method: "DELETE", url: "/api/v1/rag/ingest/uploads/u1", body: undefined });
  assert.equal(page.$refs.discardDialog.open, false);
  assert.deepEqual(toasts, [{ type: "success", html: "Upload discarded" }]);
  assert.equal(page.batch, "");
});

test("picking a collection reloads the body for it", () => {
  const { page, swaps } = world();
  page.pickCollection("my kb");

  assert.deepEqual(swaps[0], {
    method: "GET",
    url: "/partials/embedding?connection=default&collection=my%20kb",
    target: "#embed-body",
    swap: "innerHTML",
  });
});

test("a body that could not be loaded says why, escaped", () => {
  const target = { id: "embed-body", innerHTML: "" };
  const shown = showBodyError(
    { target, xhr: { status: 502, responseText: JSON.stringify({ detail: "<i>down</i>" }) } },
    TEXTS,
  );

  assert.equal(shown, true);
  assert.match(target.innerHTML, /&lt;i&gt;down&lt;\/i&gt;/);
  assert.match(target.innerHTML, /Embedding is not available/);
  assert.equal(showBodyError({ target, xhr: { status: 401 } }, TEXTS), false);
  assert.equal(showBodyError({ target: { id: "other" } }, TEXTS), false);
});

test("the page's listeners: Refresh, the connection selector and its form", () => {
  const doc = new MiniDocument();
  const select = doc.element("select", { id: "embed-connection", value: "other" });
  const swaps = [];
  const urls = [];
  const win = {
    htmx: { ajax: (method, url, options) => swaps.push({ url, ...options }) },
    history: { replaceState: (_s, _t, url) => urls.push(url) },
  };
  installEmbeddingListeners(win, doc, TEXTS);

  const refresh = doc.element("button", { "data-embed-reload": "" });
  const label = doc.element("span", {}, refresh);
  doc.fire("click", { target: label });
  assert.equal(swaps[0].url, "/partials/embedding?connection=other");

  doc.fire("change", { target: select });
  assert.deepEqual(urls, ["/embedding?connection=other"]);
  assert.equal(swaps.length, 2);

  let prevented = false;
  doc.fire("submit", { target: { id: "embed-connection-form" }, preventDefault: () => { prevented = true; } });
  assert.equal(prevented, true);
});
