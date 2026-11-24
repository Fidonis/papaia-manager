// The overlay editor of Settings -> Overlay (pages/settings-overlay.js) as an Alpine component
// against stand-ins: the requests it makes, what it shows, the job it follows and when the
// page is reloaded. The component reaches nothing global; everything comes in as an argument.
import assert from "node:assert/strict";
import { test } from "node:test";

import { fmt, fmtPlural } from "../../src/app/static/js/core/format.js";
import { overlayEditor } from "../../src/app/static/js/pages/settings-overlay.js";

const TEXTS = {
  rollbackTitle: "Roll back to {when}?",
  applyTitle: "Apply overlay to {label}?",
  rollbackButton: "Roll back",
  applyButton: "Apply overlay",
  cannotMerge: "The draft can't be merged.",
  applyNoteOne: "Apply saves the current file to Versions first, then recreates {label} ({count} service).",
  applyNoteOther: "Apply saves the current file to Versions first, then recreates {label} ({count} services).",
  applyNoteOff: "Apply saves the file. {label} is switched off, so it takes effect when it is switched on.",
  applying: "Applying the overlay …",
  rollingBack: "Rolling back the overlay …",
  discarding: "Discarding the overlay …",
  done: "Done",
  failed: "The change failed. The log is above.",
};

const INIT = {
  selected: "llm",
  preview_available: true,
  targets: [
    { key: "llm", label: "Models", path: "llm/overlay.yaml", group_active: true, services: 2, exists: false, lines: 0 },
    { key: "web", label: "Web", path: "web/overlay.yaml", group_active: false, services: 1, exists: false, lines: 0 },
  ],
  other: [],
};

const FILE = {
  content: "a: 1\n",
  exists: true,
  lines: 1,
  revision: "0123456789abcdef",
  versions: [{ id: "v1", when: "2026-11-20T10:00:00Z", size: 5, revision: "aaaa", event: "apply" }],
  preview_available: true,
};

function setup(answer = () => ({ ok: true, data: {} })) {
  const sent = [];
  const shown = [];
  const polled = [];
  const timers = [];
  const listeners = {};
  const went = [];
  let strips = 0;
  let reloads = 0;
  const dialogs = { confirmDialog: dialog(), discard: dialog() };
  const editor = overlayEditor({
    init: structuredClone(INIT),
    texts: TEXTS,
    request: async (method, url, body) => {
      sent.push([method, url, body]);
      return answer(method, url, body);
    },
    show: (type, html) => shown.push([type, html]),
    pollJob: (id, onDone) => polled.push([id, onDone]),
    refreshStrip: () => strips++,
    events: { addEventListener: (name, listener) => (listeners[name] = listener) },
    navigate: (url) => went.push(url),
    reload: () => reloads++,
    clock: { setTimeout: (fn, ms) => timers.push([fn, ms]) },
  });
  editor.$refs = dialogs;
  return {
    editor, sent, shown, polled, timers, listeners, went, dialogs,
    strips: () => strips,
    reloads: () => reloads,
  };
}

function dialog() {
  const d = { open: false, showModal: () => (d.open = true), close: () => (d.open = false) };
  return d;
}

function files(method, url) {
  if (method === "GET" && url === "/api/v1/config/overlay/llm") return { ok: true, data: structuredClone(FILE) };
  if (url.endsWith("/check")) return { ok: true, data: { problems: [], summary: "1 key" } };
  return { ok: true, data: {} };
}

test("the template's sentence helpers are the shared ones", () => {
  const { editor } = setup();
  assert.equal(editor.fmt, fmt);
  assert.equal(editor.fmtPlural, fmtPlural);
});

test("start opens the selected file and checks it", async () => {
  const { editor, sent } = setup(files);
  await editor.start();
  assert.deepEqual(sent, [
    ["GET", "/api/v1/config/overlay/llm", undefined],
    ["POST", "/api/v1/config/overlay/llm/check", { content: "a: 1\n" }],
  ]);
  assert.equal(editor.draft, "a: 1\n");
  assert.equal(editor.saved, "a: 1\n");
  assert.equal(editor.versions.length, 1);
  assert.equal(editor.summary, "1 key");
  assert.deepEqual(editor.problems, []);
  assert.equal(editor.current.key, "llm");
  assert.equal(editor.current.exists, true);
  assert.equal(editor.current.lines, 1);
  assert.equal(editor.current.revision, "0123456789abcdef");
});

test("a file that cannot be read is told, escaped, and nothing is open", async () => {
  const { editor, shown } = setup(() => ({ ok: false, error: "<b>no</b>" }));
  await editor.select("web");
  assert.equal(editor.file, null);
  assert.deepEqual(shown, [["error", "&lt;b&gt;no&lt;/b&gt;"]]);
});

test("a refusal the shared page script has told is not told twice", async () => {
  const { editor, shown } = setup(() => ({ ok: false, handled: true }));
  await editor.select("web");
  assert.deepEqual(shown, []);
});

test("line numbers follow the draft", () => {
  const { editor } = setup();
  editor.draft = "";
  assert.equal(editor.lineNumbers(), "1");
  editor.draft = "a\nb\nc";
  assert.equal(editor.lineNumbers(), "1\n2\n3");
});

test("the dialog says what is done to which target", async () => {
  const { editor, dialogs } = setup(files);
  await editor.start();
  editor.askApply();
  assert.equal(dialogs.confirmDialog.open, true);
  assert.equal(editor.confirmTitle, "Apply overlay to Models?");
  assert.equal(editor.confirmButton, "Apply overlay");
  editor.askRollback(FILE.versions[0]);
  assert.equal(editor.confirmButton, "Roll back");
  assert.match(editor.confirmTitle, /^Roll back to .+\?$/);
});

test("the note under Apply depends on whether the group runs", async () => {
  const { editor } = setup(files);
  await editor.start();
  assert.equal(editor.applyNote(), "Apply saves the current file to Versions first, then recreates Models (2 services).");
  editor.key = "web";
  assert.equal(editor.applyNote(), "Apply saves the file. Web is switched off, so it takes effect when it is switched on.");
});

test("apply writes the draft against the revision opened and follows the job", async () => {
  const answer = (method, url, body) => (method === "PUT" ? { ok: true, data: { job_id: "job-1" } } : files(method, url, body));
  const t = setup(answer);
  await t.editor.start();
  t.editor.draft = "a: 2\n";
  t.editor.askApply();
  await t.editor.confirmAction();
  assert.equal(t.dialogs.confirmDialog.open, false);
  assert.deepEqual(t.sent.at(-1), ["PUT", "/api/v1/config/overlay/llm", { content: "a: 2\n", revision: "0123456789abcdef" }]);
  assert.deepEqual(t.shown, [["info", "Applying the overlay …"]]);
  assert.equal(t.strips(), 1);
  assert.equal(t.polled[0][0], "job-1");
  t.polled[0][1]({ status: "succeeded" });
  assert.deepEqual(t.shown.at(-1), ["success", "Done"]);
  assert.equal(t.timers[0][1], 900);
  t.timers[0][0]();
  assert.deepEqual(t.went, ["/settings/overlay?target=llm"]);
});

test("a failed job is told and the page returns to the target", async () => {
  const answer = (method, url, body) => (method === "PUT" ? { ok: true, data: { job_id: "job-2" } } : files(method, url, body));
  const t = setup(answer);
  await t.editor.start();
  await t.editor.apply("0123456789abcdef");
  t.polled[0][1]({ status: "failed" });
  assert.deepEqual(t.shown.at(-1), ["error", "The change failed. The log is above."]);
  t.timers[0][0]();
  assert.deepEqual(t.went, ["/settings/overlay?target=llm"]);
});

test("a host-side change is a conflict, not an error toast", async () => {
  const answer = (method, url, body) =>
    method === "PUT" ? { ok: false, error: "The file changed after you opened it." } : files(method, url, body);
  const t = setup(answer);
  await t.editor.start();
  await t.editor.apply("0123456789abcdef");
  assert.equal(t.editor.conflict, true);
  assert.deepEqual(t.shown, []);
  assert.equal(t.editor.busy, false);
});

test("apply anyway reads the host's revision and applies the draft against it", async () => {
  let reads = 0;
  const answer = (method, url, body) => {
    if (method === "GET") {
      reads++;
      return { ok: true, data: { ...structuredClone(FILE), revision: reads === 1 ? "0123456789abcdef" : "fedcba9876543210" } };
    }
    if (method === "PUT") return { ok: true, data: { job_id: "job-3" } };
    return files(method, url, body);
  };
  const t = setup(answer);
  await t.editor.start();
  t.editor.draft = "mine: 1\n";
  t.editor.conflict = true;
  t.editor.askApplyAnyway();
  assert.equal(t.editor.anyway, true);
  await t.editor.confirmAction();
  assert.equal(t.editor.conflict, false);
  assert.deepEqual(t.sent.at(-1), ["PUT", "/api/v1/config/overlay/llm", { content: "mine: 1\n", revision: "fedcba9876543210" }]);
});

test("roll back and discard name the version and the revision", async () => {
  const answer = (method, url, body) =>
    method === "POST" && url.endsWith("/rollback") || method === "DELETE"
      ? { ok: true, data: { job_id: "job-4" } }
      : files(method, url, body);
  const t = setup(answer);
  await t.editor.start();
  t.editor.askRollback(FILE.versions[0]);
  await t.editor.confirmAction();
  assert.deepEqual(t.sent.at(-1), ["POST", "/api/v1/config/overlay/llm/rollback", { version: "v1", revision: "0123456789abcdef" }]);
  assert.deepEqual(t.shown.at(-1), ["info", "Rolling back the overlay …"]);
  t.editor.file.revision = "a b&c";
  t.dialogs.discard.showModal();
  await t.editor.discard();
  assert.equal(t.dialogs.discard.open, false);
  assert.deepEqual(t.sent.at(-1), ["DELETE", "/api/v1/config/overlay/llm?revision=a%20b%26c", undefined]);
  assert.deepEqual(t.shown.at(-1), ["info", "Discarding the overlay …"]);
});

test("the effective result: merged, unavailable, refused", async () => {
  let preview = { ok: true, changed: true, added: 1, removed: 0, diff: [{ kind: "add", text: "a: 2" }] };
  const answer = (method, url, body) => (url.endsWith("/preview") ? { ok: true, data: preview } : files(method, url, body));
  const t = setup(answer);
  await t.editor.start();
  await t.editor.showEffective();
  assert.equal(t.editor.tab, "effective");
  assert.equal(t.editor.previewing, false);
  assert.deepEqual(t.editor.effective, preview);
  preview = { ok: false, available: false };
  await t.editor.showEffective();
  assert.equal(t.editor.previewUnavailable, true);
  preview = { ok: false, problems: [{ message: "bad indent" }] };
  await t.editor.showEffective();
  assert.equal(t.editor.previewError, "bad indent");
  preview = { ok: false };
  await t.editor.showEffective();
  assert.equal(t.editor.previewError, "The draft can't be merged.");
});

test("no preview while the draft has problems", async () => {
  const t = setup(files);
  await t.editor.start();
  t.editor.problems = [{ line: 1, message: "x" }];
  const before = t.sent.length;
  await t.editor.showEffective();
  assert.equal(t.sent.length, before);
  assert.equal(t.editor.tab, "overlay");
});

test("a version is shown as a diff or restored as the draft", async () => {
  const answer = (method, url, body) =>
    url.endsWith("/versions/v1") ? { ok: true, data: { content: "old: 1\n", diff: [{ kind: "del", text: "a: 1" }] } } : files(method, url, body);
  const t = setup(answer);
  await t.editor.start();
  await t.editor.viewVersion(FILE.versions[0]);
  assert.deepEqual(t.editor.versionDiff, [{ kind: "del", text: "a: 1" }]);
  assert.equal(t.editor.versionDiffFor, FILE.versions[0]);
  await t.editor.restoreDraft(FILE.versions[0]);
  assert.equal(t.editor.draft, "old: 1\n");
  assert.equal(t.editor.tab, "overlay");
  assert.notEqual(t.editor.restoredFrom, "");
});

test("a configuration job that ends elsewhere reloads the page once it was seen running", async () => {
  const t = setup(files);
  await t.editor.start();
  const announce = t.listeners["papaia-config-job"];
  announce({ detail: { active: false } });
  assert.equal(t.reloads(), 0);
  announce({ detail: { active: true } });
  assert.equal(t.reloads(), 0);
  announce({ detail: { active: false } });
  assert.equal(t.reloads(), 1);
});

test("a job announced while the first file is still loading is not missed", async () => {
  let release;
  const pending = new Promise((resolve) => {
    release = resolve;
  });
  const t = setup(async (method, url, body) => {
    await pending;
    return files(method, url, body);
  });
  const started = t.editor.start();
  const announce = t.listeners["papaia-config-job"];
  assert.equal(typeof announce, "function");
  announce({ detail: { active: true } });
  release();
  await started;
  announce({ detail: { active: false } });
  assert.equal(t.reloads(), 1);
});

test("while the page follows its own job, the strip's announcements are left alone", async () => {
  const answer = (method, url, body) => (method === "PUT" ? { ok: true, data: { job_id: "job-5" } } : files(method, url, body));
  const t = setup(answer);
  await t.editor.start();
  await t.editor.apply("0123456789abcdef");
  t.listeners["papaia-config-job"]({ detail: { active: true } });
  t.listeners["papaia-config-job"]({ detail: { active: false } });
  assert.equal(t.reloads(), 0);
});
