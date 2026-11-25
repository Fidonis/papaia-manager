// The job editor and the credential dialogs (pages/ingest-editor-components.js) against a
// stand-in request: what each sends, what it says afterwards, and what it refuses to send.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createCredentialDialog,
  createDeleteCredential,
  createJobEditor,
  linesOf,
  scheduleModes,
} from "../../src/app/static/js/pages/ingest-editor-components.js";

const TEXTS = {
  outcomeNotAskedNote: "{what} (the ingester could not be asked: {note})",
  outcomeNotAsked: "{what} (the ingester could not be asked)",
  outcomeNotActive: "{what}. It is saved but not active yet: {why}",
  outcomeNotApplied: "the ingester did not apply the catalog",
  scheduleModes: [
    { value: "manual", label: "Only when started" },
    { value: "interval", label: "Every…" },
    { value: "hourly", label: "Hourly" },
    { value: "daily", label: "Daily" },
    { value: "weekly", label: "Weekly" },
    { value: "monthly", label: "Monthly" },
    { value: "cron", label: "Custom" },
  ],
  problemsToFix: ["{count} problem to fix", "{count} problems to fix"],
  advancedShow: "Advanced options",
  advancedHide: "Hide advanced options",
  notFound: "{name} (not found)",
  credentialFromStore: "{name} (stored)",
  credentialFromEnvironment: "{name} (environment)",
  collectionWithPoints: "An existing collection with {points} points.",
  collectionExisting: "An existing collection.",
  collectionNew: "Not there yet: the first run creates it.",
  collectionChoose: "Choose an existing collection or name a new one.",
  catalogDefault: "catalog default: {value}",
  nextRuns: "Next: {runs} ({timezone})",
  jobSaved: "Job <b>{id}</b> saved",
  dryRunFailed: "The dry run could not be started:",
  credentialReplaceTitle: "Replace the value",
  credentialNewTitle: "New credential",
  credentialOneLine: "One line (password, token)",
  credentialSeveralLines: "Several lines (key, JSON)",
  credentialStored: "Credential <b>{name}</b> stored",
  credentialDeleted: "Credential <b>{name}</b> deleted",
};

function boot(overrides = {}) {
  return {
    state: {
      id: "", description: "", enabled: true,
      source: { type: "local", label: "", path: "", rclone_flags: ["--a", "--b"] },
      target: { connection: "default", collection: "", acl_tags: ["dept:x"], extra_payload: "" },
      filters: { include: ["**/*.pdf"], exclude: [], max_file_bytes: 2097152 },
      embedding: { model: "" },
      schedule: { mode: "manual", weekdays: ["mon"] },
      mode: "upsert",
    },
    etag: "", isNew: true, originalId: "",
    sources: [
      { type: "local", fields: [{ key: "path", kind: "folder" }], remote: false },
      { type: "webdav", fields: [{ key: "url", kind: "url" }, { key: "pass", kind: "secret" }, { key: "depth", kind: "integer", default: 3 }], remote: true },
    ],
    credentials: [{ name: "QI_SECRET_ENV", origin: "environment" }],
    connections: ["default"],
    modes: [], strategies: {}, startups: {}, presets: [{ title: "PDF", include: ["**/*.pdf"] }, { title: "Word", include: ["**/*.docx", "**/*.doc"] }],
    timezones: ["Europe/Berlin"], weekdays: [{ key: "mon", label: "Monday" }],
    defaults: { "chunking.words": 300, "filters.include": ["a", "b"] }, inherited: {},
    timezone: "Europe/Berlin",
    ...overrides,
  };
}

// A page in a test: what was sent, shown, where it went and what the page was told.
function setup(answers = {}) {
  const sent = [];
  const toasts = [];
  const went = [];
  const told = [];
  const env = {
    texts: TEXTS,
    lang: "en",
    request: async (method, url, body) => {
      sent.push({ method, url, body });
      const answer = answers[`${method} ${url}`];
      return typeof answer === "function" ? answer(body) : answer || { ok: true, data: {} };
    },
    toast: (level, html) => toasts.push([level, html]),
    navigate: (url) => went.push(url),
    here: () => new URL("http://manager.test/ingest/new"),
    watchLeaving: (editor) => told.push(["watch", editor]),
    askForCredential: () => told.push(["credential-new"]),
    credentialSaved: (name) => told.push(["credential-saved", name]),
    credentialDeleted: (name) => told.push(["credential-deleted", name]),
  };
  return { env, sent, toasts, went, told };
}

// A component as Alpine makes it: the object, with its magics stood in.
function component(made) {
  const dialog = { open: false, showModal() { this.open = true; }, close() { this.open = false; } };
  made.$root = dialog;
  made.$refs = {
    leave: { open: false, showModal() { this.open = true; }, close() { this.open = false; } },
    picker: { open: false, showModal() { this.open = true; }, close() { this.open = false; } },
  };
  made.$watch = () => {};
  made.$nextTick = (fn) => fn();
  return made;
}

test("the schedule modes come from the texts and lines join with newlines", () => {
  assert.deepEqual(scheduleModes(TEXTS).map((m) => m.value), ["manual", "interval", "hourly", "daily", "weekly", "monthly", "cron"]);
  assert.equal(linesOf(["a", "b"]), "a\nb");
  assert.equal(linesOf(null), "");
});

test("the editor starts from the island's values and says what it needs to the page", () => {
  const { env, told } = setup();
  const editor = component(createJobEditor(env, boot()));
  assert.equal(editor.text.rclone, "--a\n--b");
  assert.equal(editor.text.acl, "dept:x");
  assert.equal(editor.maxMb, 2);
  assert.equal(editor.scheduleModes.length, 7);
  editor.init();
  assert.deepEqual(told.map((t) => t[0]), ["watch"]);
  assert.equal(told[0][1], editor);
});

test("the payload carries the text fields as lines and the size in bytes", () => {
  const { env } = setup();
  const editor = component(createJobEditor(env, boot()));
  editor.text.include = "**/*.md\n**/*.txt";
  editor.maxMb = "1.5";
  const job = editor.payload();
  assert.equal(job.filters.include, "**/*.md\n**/*.txt");
  assert.equal(job.filters.max_file_bytes, 1572864);
  assert.equal(job.source.rclone_flags, "--a\n--b");
  editor.maxMb = "";
  assert.equal(editor.payload().filters.max_file_bytes, null);
  assert.notEqual(editor.payload(), editor.job, "the payload is a copy");
});

test("the texts of the form are whole sentences filled as text", () => {
  const { env } = setup();
  const editor = component(createJobEditor(env, boot()));
  editor.summary = [{ field: "id", message: "x" }];
  assert.equal(editor.summaryTitle(), "1 problem to fix");
  editor.summary.push({ field: "a", message: "y" });
  assert.equal(editor.summaryTitle(), "2 problems to fix");
  assert.deepEqual(editor.shownIssues(), editor.summary);
  assert.equal(editor.advancedLabel(), "Advanced options");
  editor.toggleAdvancedSource();
  assert.equal(editor.advancedLabel(), "Hide advanced options");
  assert.equal(editor.inheritedNote("chunking.words"), "catalog default: 300");
  assert.equal(editor.inheritedNote("filters.include"), "catalog default: a, b");
  assert.equal(editor.inheritedNote("nope"), "");
  editor.preview = { ok: true, runs: ["<b>Mon</b>", "Tue"], timezone: "Europe/Berlin" };
  assert.equal(editor.nextRunsText(), "Next: <b>Mon</b>  ·  Tue (Europe/Berlin)", "a value stays text");
  editor.job.target.connection = "gone";
  assert.equal(editor.connectionMissing(), true);
  assert.equal(editor.connectionLabel(), "gone (not found)");
  editor.job.target.connection = "default";
  assert.equal(editor.connectionMissing(), false);
});

test("the credentials offered name where they are kept and keep one that is gone", () => {
  const { env, told } = setup();
  const editor = component(createJobEditor(env, boot()));
  editor.setType("webdav");
  assert.deepEqual(Object.keys(editor.job.source), ["type", "label", "rclone_flags", "url", "pass", "depth"]);
  assert.equal(editor.job.source.depth, 3);
  editor.job.source.pass = "QI_SECRET_OLD";
  assert.deepEqual(editor.credentialOptions("pass"), [
    { name: "QI_SECRET_ENV", label: "QI_SECRET_ENV (environment)" },
    { name: "QI_SECRET_OLD", label: "QI_SECRET_OLD (not found)" },
  ]);
  editor.newCredential("pass");
  assert.deepEqual(told.at(-1), ["credential-new"]);
  editor.credentialSaved({ name: "QI_SECRET_NEW" });
  assert.equal(editor.job.source.pass, "QI_SECRET_NEW");
  assert.deepEqual(editor.credentials.at(-1), { name: "QI_SECRET_NEW", origin: "store" });
  assert.equal(editor.credentialOptions("pass")[1].label, "QI_SECRET_NEW (stored)");
  assert.equal(editor.form().remote, true);
});

test("presets, weekdays and the collection note", async () => {
  const { env } = setup({
    "GET /api/v1/rag/ingest/collections?connection=default": {
      ok: true, data: { collections: [{ name: "kb", points: 12345, embedding_model: "bge-m3" }] },
    },
  });
  const editor = component(createJobEditor(env, boot()));
  const word = editor.presets[1];
  assert.equal(editor.presetOn(editor.presets[0]), true);
  editor.togglePreset(word);
  assert.equal(editor.text.include, "**/*.pdf\n**/*.docx\n**/*.doc");
  editor.togglePreset(editor.presets[0]);
  assert.equal(editor.text.include, "**/*.docx\n**/*.doc");
  editor.toggleDay("tue");
  editor.toggleDay("mon");
  assert.deepEqual(editor.job.schedule.weekdays, ["tue"]);
  editor.setScheduleMode("weekly");
  assert.equal(editor.job.schedule.mode, "weekly");
  assert.equal(editor.collectionNote(), "Choose an existing collection or name a new one.");
  editor.job.target.collection = "new-one";
  assert.equal(editor.collectionNote(), "Not there yet: the first run creates it.");
  await editor.loadCollections();
  editor.job.target.collection = "kb";
  assert.equal(editor.collectionNote(), "An existing collection with 12,345 points.");
  assert.equal(editor.modelLocked(), true);
  editor.syncModel();
  assert.equal(editor.job.embedding.model, "bge-m3");
});

test("the folder picker browses below the documents folder", async () => {
  const { env, sent } = setup({
    "GET /api/v1/rag/ingest/tree?source=folder&path=docs%2Fa": { ok: true, data: { entries: [{ name: "b", path: "docs/a/b", dir: true }] } },
    "GET /api/v1/rag/ingest/tree?source=folder&path=docs": { ok: false, error: "gone" },
  });
  const editor = component(createJobEditor(env, boot()));
  editor.job.source.path = "/data/local/docs/a/";
  editor.openPicker();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(editor.$refs.picker.open, true);
  assert.equal(sent[0].url, "/api/v1/rag/ingest/tree?source=folder&path=docs%2Fa");
  assert.equal(editor.pick.path, "docs/a");
  assert.equal(editor.parentOf("docs/a"), "docs");
  assert.equal(editor.pickedPath(), "/docs/a");
  await editor.browse("docs");
  assert.equal(editor.pick.error, "gone");
  assert.equal(editor.pick.path, "docs/a", "a refused folder keeps the one shown");
  editor.choose();
  assert.equal(editor.job.source.path, "/data/local/docs/a");
  assert.equal(editor.$refs.picker.open, false);
});

test("a new job is created, its outcome told, and the page goes to it", async () => {
  const { env, sent, toasts, went } = setup({
    "POST /api/v1/rag/ingest/jobs": { ok: true, data: { verified: true, applied: true } },
  });
  const editor = component(createJobEditor(env, boot()));
  editor.job.id = "hand<book";
  editor.dirty = true;
  await editor.save(false);
  assert.deepEqual(sent.map((s) => `${s.method} ${s.url}`), ["POST /api/v1/rag/ingest/jobs"]);
  assert.equal(sent[0].body.job.id, "hand<book");
  assert.deepEqual(toasts, [["success", "Job <b>hand&lt;book</b> saved"]]);
  assert.deepEqual(went, ["/ingest/jobs/hand%3Cbook"]);
  assert.equal(editor.dirty, false);
  assert.equal(editor.saving, true);
});

test("an existing job is replaced with its etag and a dry run follows on request", async () => {
  const { env, sent, toasts, went } = setup({
    "PUT /api/v1/rag/ingest/jobs/old": { ok: true, data: { verified: false, note: "<down>" } },
    "POST /api/v1/rag/ingest/jobs/new/run": { ok: true, data: { run_id: "r 1" } },
  });
  const editor = component(createJobEditor(env, boot({ isNew: false, originalId: "old", etag: "e1" })));
  editor.job.id = "new";
  await editor.save(true);
  assert.deepEqual(sent.map((s) => `${s.method} ${s.url}`), [
    "PUT /api/v1/rag/ingest/jobs/old",
    "POST /api/v1/rag/ingest/jobs/new/run",
  ]);
  assert.equal(sent[0].body.etag, "e1");
  assert.equal(sent[0].body.original_id, "old");
  assert.deepEqual(sent[1].body, { dry_run: true });
  assert.deepEqual(toasts, [["warning", "Job <b>new</b> saved (the ingester could not be asked: &lt;down&gt;)"]]);
  assert.deepEqual(went, ["/ingest/runs/r%201"]);
});

test("a dry run that cannot start is told and the page goes to the job", async () => {
  const { env, toasts, went } = setup({
    "POST /api/v1/rag/ingest/jobs/j/run": { ok: false, error: "<busy>" },
  });
  const editor = component(createJobEditor(env, boot()));
  editor.job.id = "j";
  await editor.save(true);
  assert.deepEqual(toasts.at(-1), ["error", "The dry run could not be started: &lt;busy&gt;"]);
  assert.deepEqual(went, ["/ingest/jobs/j"]);
});

test("a refused save shows why and checks the form again", async () => {
  const { env, sent, went } = setup({
    "POST /api/v1/rag/ingest/jobs": { ok: false, error: "refused" },
    "POST /api/v1/rag/ingest/jobs/validate": { ok: true, data: { issues: [{ field: "id", message: "taken" }, { field: "id", message: "second" }] } },
  });
  const editor = component(createJobEditor(env, boot()));
  await editor.save(false);
  assert.equal(editor.saveError, "refused");
  assert.equal(editor.err("id"), "taken");
  assert.equal(editor.summary.length, 2);
  assert.deepEqual(sent.map((s) => s.url), ["/api/v1/rag/ingest/jobs", "/api/v1/rag/ingest/jobs/validate"]);
  assert.deepEqual(went, []);
  assert.equal(editor.busy, false);
});

test("a link out of a dirty editor asks first, one on the same page does not", () => {
  const { env, went } = setup();
  const editor = component(createJobEditor(env, boot()));
  const click = (href, extra = {}) => {
    const link = { href, target: "", hasAttribute: () => false };
    const event = {
      button: 0, defaultPrevented: false, target: { closest: () => link },
      prevented: false, preventDefault() { this.prevented = true; }, ...extra,
    };
    editor.guardLink(event);
    return event;
  };
  assert.equal(click("/ingest/jobs").prevented, false, "a clean editor lets the link go");
  editor.dirty = true;
  assert.equal(editor.blocksLeaving(), true);
  assert.equal(click("http://manager.test/ingest/new").prevented, false);
  assert.equal(click("/ingest/jobs", { ctrlKey: true }).prevented, false);
  const event = click("/ingest/jobs");
  assert.equal(event.prevented, true);
  assert.equal(editor.$refs.leave.open, true);
  editor.leaveNow();
  assert.deepEqual(went, ["http://manager.test/ingest/jobs"]);
  assert.equal(editor.dirty, false);
});

test("the credential dialog stores a value, forgets it and tells the page the name", async () => {
  const { env, sent, toasts, told } = setup({
    "PUT /api/v1/rag/ingest/secrets/DAV": { ok: true, data: { name: "QI_SECRET_<DAV>" } },
  });
  const dialog = component(createCredentialDialog(env));
  dialog.openNew({ name: "QI_SECRET_DAV" });
  assert.equal(dialog.name, "DAV");
  assert.equal(dialog.$root.open, true);
  assert.equal(dialog.title(), "New credential");
  assert.equal(dialog.valueLabel(), "Several lines (key, JSON)");
  dialog.toggleMultiline();
  assert.equal(dialog.valueLabel(), "One line (password, token)");
  dialog.value = "synthetic-value";
  assert.equal(dialog.canSubmit(), true);
  await dialog.submit();
  assert.deepEqual(sent, [{ method: "PUT", url: "/api/v1/rag/ingest/secrets/DAV", body: { value: "synthetic-value" } }]);
  assert.equal(dialog.value, "", "the value is gone from the page once it was sent");
  assert.equal(dialog.$root.open, false);
  assert.deepEqual(toasts, [["success", "Credential <b>QI_SECRET_&lt;DAV&gt;</b> stored"]]);
  assert.deepEqual(told.at(-1), ["credential-saved", "QI_SECRET_<DAV>"]);
});

test("replacing keeps the name and a refusal stays in the dialog", async () => {
  const { env, toasts, told } = setup({
    "PUT /api/v1/rag/ingest/secrets/DAV": { ok: false, error: "no key" },
  });
  const dialog = component(createCredentialDialog(env));
  dialog.openReplace({ name: "QI_SECRET_DAV" });
  assert.equal(dialog.existing, true);
  assert.equal(dialog.title(), "Replace the value");
  assert.equal(dialog.canSubmit(), false, "nothing typed yet");
  dialog.value = "new";
  await dialog.submit();
  assert.equal(dialog.error, "no key");
  assert.equal(dialog.value, "");
  assert.equal(dialog.$root.open, true);
  assert.deepEqual(toasts, []);
  assert.deepEqual(told, []);
});

test("deleting a credential confirms in a dialog and reloads the list", async () => {
  const { env, sent, toasts, told } = setup({
    "DELETE /api/v1/rag/ingest/secrets/QI_SECRET_%3CX%3E": { ok: true, data: null },
  });
  const dialog = component(createDeleteCredential(env));
  dialog.open({ name: "QI_SECRET_<X>" });
  assert.equal(dialog.$root.open, true);
  await dialog.submit();
  assert.deepEqual(sent.map((s) => `${s.method} ${s.url}`), ["DELETE /api/v1/rag/ingest/secrets/QI_SECRET_%3CX%3E"]);
  assert.deepEqual(toasts, [["success", "Credential <b>QI_SECRET_&lt;X&gt;</b> deleted"]]);
  assert.deepEqual(told, [["credential-deleted", "QI_SECRET_<X>"]]);
  assert.equal(dialog.$root.open, false);
});

test("a failure that was told already leaves the dialogs as they are", async () => {
  const { env, toasts, told } = setup({
    "DELETE /api/v1/rag/ingest/secrets/X": { ok: false, handled: true },
  });
  const dialog = component(createDeleteCredential(env));
  dialog.open({ name: "X" });
  await dialog.submit();
  assert.deepEqual(toasts, []);
  assert.deepEqual(told, []);
  assert.equal(dialog.$root.open, true);
});

test("an unavailable collection list shows its reason, and an answered one clears it", async () => {
  let answer = { ok: true, data: { available: false, reason: "Qdrant did not answer within 20 seconds.", collections: [] } };
  const { env } = setup({ "GET /api/v1/rag/ingest/collections?connection=default": () => answer });
  const editor = component(createJobEditor(env, boot()));
  assert.equal(editor.collectionsReason, "");
  await editor.loadCollections();
  assert.equal(editor.collectionsReason, "Qdrant did not answer within 20 seconds.");
  assert.deepEqual(editor.collections, []);
  answer = { ok: true, data: { available: true, reason: null, collections: [{ name: "kb", points: 1, embedding_model: null }] } };
  await editor.loadCollections();
  assert.equal(editor.collectionsReason, "");
  assert.equal(editor.collections.length, 1);
});

test("a validation without the ingester's verdict says why, next to the problems", async () => {
  let answer = { ok: true, data: { ok: true, issues: [], reason: "The ingester did not answer within 20 seconds." } };
  const { env } = setup({ "POST /api/v1/rag/ingest/jobs/validate": () => answer });
  const editor = component(createJobEditor(env, boot()));
  await editor.validate();
  assert.equal(editor.checkReason, "The ingester did not answer within 20 seconds.");
  answer = { ok: true, data: { ok: true, issues: [] } };
  await editor.validate();
  assert.equal(editor.checkReason, "");
});

test("the editor sends its start requests once and nothing more until the job changes", async () => {
  const { env, sent } = setup();
  const watchers = [];
  const editor = component(createJobEditor(env, boot()));
  editor.$watch = (what, fn) => watchers.push([what, fn]);
  editor.init();
  await new Promise((resolve) => setTimeout(resolve, 900));
  assert.deepEqual(sent.map((s) => `${s.method} ${s.url}`), [
    "GET /api/v1/rag/ingest/collections?connection=default",
    "POST /api/v1/rag/ingest/schedule/preview",
  ]);
  // The answers changed nothing a watcher looks at: no validation, no second preview.
  assert.equal(editor.dirty, false);
});
