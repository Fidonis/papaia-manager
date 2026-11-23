// The verbs and dialogs of the ingest pages (pages/ingest-components.js) against a stand-in
// request: what each sends, what it says afterwards, and what it refuses to send.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createCatalogDefaults,
  createDeleteJobDialog,
  createDeleteOrphan,
  createDeleteRunsDialog,
  createRawEditor,
  createRunDialog,
  createVerbs,
  ingestOutcome,
  jobUrl,
  keepUntil,
  rowMatches,
  showWaitingToast,
  WAITING_TOAST,
} from "../../src/app/static/js/pages/ingest-components.js";

const TEXTS = {
  outcomeNotAskedNote: "{what} (the ingester could not be asked: {note})",
  outcomeNotAsked: "{what} (the ingester could not be asked)",
  outcomeNotActive: "{what}. It is saved but not active yet: {why}",
  outcomeNotApplied: "the ingester did not apply the catalog",
  jobEnabled: "Job <b>{id}</b> enabled",
  jobDisabled: "Job <b>{id}</b> disabled",
  resumed: "Scheduling of <b>{id}</b> resumed",
  reloadRefused: "The ingester read jobs.yaml and found problems",
  reloaded: "The ingester read jobs.yaml",
  abortRequested: "Abort requested. The run stops after the file it is working on.",
  modeAsSetUp: "As the job is set up: {mode}",
  noFinishedRuns: "The job has no finished runs.",
  runsWillBeDeleted: ["{count} run will be deleted.", "{count} runs will be deleted."],
  jobDeleted: "Job <b>{id}</b> deleted",
  runsDeleted: ["{count} run deleted", "{count} runs deleted"],
  pointsRemoved: "{count} points removed from the collection",
  firstAfterLast: "The first day is after the last day.",
  counting: "Counting the runs ...",
  noRunInPeriod: "No finished run in this period.",
  willBeDeleted: "{runs} ({events}) will be deleted.",
  runCount: ["{count} run", "{count} runs"],
  logLines: ["{count} log line", "{count} log lines"],
  stillWorking: ["{count} run is still working and will be kept.", "{count} runs are still working and will be kept."],
  deleteCount: ["Delete {count} run", "Delete {count} runs"],
  delete: "Delete",
  runsOfJobDeleted: ["{count} run of <b>{jobId}</b> deleted", "{count} runs of <b>{jobId}</b> deleted"],
  runKept: "A run that is still working was kept",
  orphanRemoved: "Removed {points} points and {rows} records of <b>{job}</b>",
  defaultsSaved: "Defaults saved",
  fileSaved: "jobs.yaml saved",
  problems: ["{count} problem", "{count} problems"],
};

// A page in a test: what was sent, shown, stored and where it went.
function setup(answers = {}) {
  const sent = [];
  const toasts = [];
  const stored = {};
  const went = [];
  const calls = { runsDeleted: 0, orphanRemoved: 0, closed: 0 };
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
    store: { setItem: (key, value) => { stored[key] = value; } },
    today: () => new Date(2026, 10, 23),
    closeMenu: () => { calls.closeMenu = (calls.closeMenu || 0) + 1; },
    afterRunsDeleted: () => { calls.runsDeleted += 1; },
    afterOrphanRemoved: () => { calls.orphanRemoved += 1; },
  };
  return { env, sent, toasts, stored, went, calls };
}

// A component as Alpine makes it: the object, with $root a stand-in dialog.
function component(made) {
  const dialog = { open: false, showModal() { this.open = true; }, close() { this.open = false; } };
  made.$root = dialog;
  return made;
}

test("a job's url escapes its id", () => {
  assert.equal(jobUrl("a/b c"), "/api/v1/rag/ingest/jobs/a%2Fb%20c");
});

test("an outcome tells a saved but inactive change from a live one", () => {
  assert.deepEqual(ingestOutcome(TEXTS, { verified: true, applied: true }, "Saved"), { level: "success", text: "Saved" });
  assert.deepEqual(ingestOutcome(TEXTS, { verified: false, note: "<down>" }, "Saved"),
    { level: "warning", text: "Saved (the ingester could not be asked: &lt;down&gt;)" });
  assert.deepEqual(ingestOutcome(TEXTS, { verified: false }, "Saved"),
    { level: "warning", text: "Saved (the ingester could not be asked)" });
  assert.equal(ingestOutcome(TEXTS, { verified: true, applied: false, elsewhere: ["x <y>"] }, "Saved").text,
    "Saved. It is saved but not active yet: x &lt;y&gt;");
  assert.equal(ingestOutcome(TEXTS, { verified: true, applied: false, elsewhere: [] }, "Saved").text,
    "Saved. It is saved but not active yet: the ingester did not apply the catalog");
});

test("the verbs send their request, escape what comes back and refresh after", async () => {
  const { env, sent, toasts } = setup({
    "POST /api/v1/rag/ingest/jobs/hand%3Cbook/disable": { ok: true, data: { verified: true, applied: true } },
    "POST /api/v1/rag/ingest/jobs/x/enable": { ok: false, error: "<no>" },
    "POST /api/v1/rag/ingest/jobs/x/resume": { ok: true, data: {} },
    "POST /api/v1/rag/ingest/reload": { ok: true, data: { refused: true } },
    "DELETE /api/v1/rag/ingest/job-runs/r%201": { ok: true, data: null },
  });
  const verbs = createVerbs(env);
  let after = 0;
  const done = () => { after += 1; };
  await verbs.toggle("hand<book", false, done);
  await verbs.toggle("x", true, done);
  await verbs.resume("x", done);
  await verbs.reloadCatalog(done);
  await verbs.abort("r 1", done);
  assert.deepEqual(sent.map((s) => `${s.method} ${s.url}`), [
    "POST /api/v1/rag/ingest/jobs/hand%3Cbook/disable",
    "POST /api/v1/rag/ingest/jobs/x/enable",
    "POST /api/v1/rag/ingest/jobs/x/resume",
    "POST /api/v1/rag/ingest/reload",
    "DELETE /api/v1/rag/ingest/job-runs/r%201",
  ]);
  assert.deepEqual(toasts, [
    ["success", "Job <b>hand&lt;book</b> disabled"],
    ["error", "<b>x</b>: &lt;no&gt;"],
    ["success", "Scheduling of <b>x</b> resumed"],
    ["warning", "The ingester read jobs.yaml and found problems"],
    ["info", "Abort requested. The run stops after the file it is working on."],
  ]);
  assert.equal(after, 4, "a refused verb does not refresh");
});

test("a verb whose failure was told already says nothing more", async () => {
  const { env, toasts } = setup({ "POST /api/v1/rag/ingest/reload": { ok: false, handled: true } });
  let after = 0;
  await createVerbs(env).reloadCatalog(() => { after += 1; });
  assert.deepEqual(toasts, []);
  assert.equal(after, 0);
});

test("a waiting toast is shown once by the next page", () => {
  const shown = [];
  const items = { [WAITING_TOAST]: JSON.stringify({ type: "success", html: "Job <b>x</b> deleted" }) };
  const storage = {
    getItem: (key) => items[key] || null,
    removeItem: (key) => { delete items[key]; },
  };
  showWaitingToast(storage, (type, html) => shown.push([type, html]));
  showWaitingToast(storage, (type, html) => shown.push([type, html]));
  assert.deepEqual(shown, [["success", "Job <b>x</b> deleted"]]);
  showWaitingToast({ getItem() { throw new Error("blocked"); } }, () => assert.fail("nothing waits"));
});

test("the run dialog guards a rebuild and sends what was chosen", async () => {
  const { env, sent, went, calls } = setup({
    "POST /api/v1/rag/ingest/jobs/kb%2Fdocs/run": { ok: true, data: { run_id: "r/1" } },
  });
  const dialog = component(createRunDialog(env));
  dialog.open({ id: "kb/docs", collection: "kb", mode: "upsert", mode_label: "Keep in sync", full_scope: "job",
    remote: true, first_run: true, siblings: ["other"] });
  assert.ok(dialog.$root.open);
  assert.equal(calls.closeMenu, 1, "the row menu closes when its dialog opens");
  assert.equal(dialog.dry, true, "a job that never ran starts as a dry run");
  assert.equal(dialog.modeOption(), "As the job is set up: Keep in sync");
  dialog.toggleAdvanced();
  assert.equal(dialog.advanced, true);

  dialog.dry = false;
  dialog.mode = "full/collection";
  assert.equal(dialog.wholeCollection(), true);
  assert.equal(dialog.canSubmit(), false, "a rebuild needs the confirmation");
  dialog.confirmRebuild = true;
  assert.equal(dialog.canSubmit(), false, "the whole collection needs its name typed");
  dialog.typed = "kb";
  assert.equal(dialog.canSubmit(), true);
  await dialog.submit();
  assert.deepEqual(sent[0].body, {
    dry_run: false, skip_sync: false, delete_vanished: true, mode: "full", full_scope: "collection",
    confirm_rebuild: true, confirm_collection: true, force: true,
  });
  assert.equal(dialog.$root.open, false);
  assert.deepEqual(went, ["/ingest/runs/r%2F1"]);
});

test("the run dialog keeps a refusal in the dialog", async () => {
  const { env, went } = setup({ "POST /api/v1/rag/ingest/jobs/kb/run": { ok: false, error: "busy" } });
  const dialog = component(createRunDialog(env));
  dialog.open({ id: "kb", collection: "kb", mode: "upsert", mode_label: "", full_scope: "job", remote: false,
    first_run: false, siblings: [] });
  dialog.keep = true;
  await dialog.submit();
  assert.equal(dialog.error, "busy");
  assert.equal(dialog.busy, false);
  assert.ok(dialog.$root.open);
  assert.deepEqual(went, []);
});

test("the delete dialog counts the runs, confirms a purge by the id and leaves its answer waiting", async () => {
  const { env, sent, stored, went } = setup({
    "POST /api/v1/rag/ingest/jobs/kb/runs/delete": { ok: true, data: { matched: 3 } },
    "DELETE /api/v1/rag/ingest/jobs/kb?purge=true&etag=e%201": {
      ok: true, data: { deleted_runs: 3, purged: true, deleted_points: 12, note: "<late>" },
    },
  });
  const dialog = component(createDeleteJobDialog(env));
  dialog.open({ id: "kb", etag: "e 1", collection: "kb", managed: false, can_delete_runs: true });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(dialog.runsText(), "3 runs will be deleted.");
  dialog.purge = true;
  assert.equal(dialog.canSubmit(), false);
  dialog.typed = "kb";
  assert.equal(dialog.canSubmit(), true);
  await dialog.submit();
  assert.deepEqual(sent.map((s) => s.method), ["POST", "DELETE"]);
  assert.deepEqual(JSON.parse(stored[WAITING_TOAST]), {
    type: "warning",
    html: "Job <b>kb</b> deleted; 3 runs deleted; 12 points removed from the collection. &lt;late&gt;",
  });
  assert.deepEqual(went, ["/ingest/jobs"]);
});

test("the delete dialog shows its answer at once when storage is blocked", async () => {
  const { env, toasts } = setup({ "DELETE /api/v1/rag/ingest/jobs/kb?purge=false&etag=": { ok: true, data: {} } });
  env.store = { setItem() { throw new Error("blocked"); } };
  const dialog = component(createDeleteJobDialog(env));
  dialog.open({ id: "kb", etag: "", collection: "kb", managed: false, can_delete_runs: false });
  assert.equal(dialog.runsText(), "");
  await dialog.submit();
  assert.deepEqual(toasts, [["success", "Job <b>kb</b> deleted"]]);
});

test("keeping the last days ends the period that many days ago", () => {
  assert.equal(keepUntil(new Date(2026, 10, 23), 30), "2026-10-24");
  assert.equal(keepUntil(new Date(2026, 0, 5), 365), "2025-01-05");
});

test("the delete-runs dialog counts before it deletes and refreshes the page after", async () => {
  let answer = 0;
  const { env, sent, toasts, calls } = setup({
    "POST /api/v1/rag/ingest/jobs/kb/runs/delete": (body) => {
      answer += 1;
      if (!body.dry_run) return { ok: true, data: { deleted_runs: 2, skipped_running: 1 } };
      return { ok: true, data: { matched: 2, matched_events: 1, skipped_running: 1 } };
    },
  });
  const dialog = component(createDeleteRunsDialog(env));
  dialog.open({ job_id: "kb", tz: "Europe/Berlin" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(dialog.previewText(),
    "2 runs (1 log line) will be deleted. 1 run is still working and will be kept.");
  assert.equal(dialog.unbounded(), true);
  assert.equal(dialog.canSubmit(), false, "every run goes: the id has to be typed");
  dialog.typed = "kb";
  assert.equal(dialog.submitText(), "Delete 2 runs");
  dialog.keep(30);
  assert.equal(dialog.until, "2026-10-24");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(dialog.canSubmit(), true);
  await dialog.submit();
  assert.deepEqual(sent.at(-1).body, { since: null, until: "2026-10-24", dry_run: false });
  assert.deepEqual(toasts, [["warning", "2 runs of <b>kb</b> deleted. A run that is still working was kept"]]);
  assert.equal(calls.runsDeleted, 1);
  assert.equal(answer, 3);
});

test("the delete-runs dialog refuses a period that ends before it starts", async () => {
  const { env, sent } = setup();
  const dialog = component(createDeleteRunsDialog(env));
  dialog.open({ job_id: "" , choose: true, jobs: ["kb"] });
  assert.equal(dialog.choose, true);
  dialog.jobId = "kb";
  dialog.since = "2026-02-01";
  dialog.until = "2026-01-01";
  await dialog.refresh();
  assert.equal(dialog.error, "The first day is after the last day.");
  assert.equal(sent.length, 0);
  assert.equal(dialog.submitText(), "Delete");
  await dialog.submit();
  assert.equal(sent.length, 0, "nothing is deleted without a count");
});

test("removing leftovers sends the job and refreshes the list", async () => {
  const { env, toasts, calls } = setup({
    "DELETE /api/v1/rag/ingest/orphans/old%20job": { ok: true, data: { deleted_points: 1200, deleted_rows: 2 } },
  });
  const dialog = component(createDeleteOrphan(env));
  dialog.open({ job_id: "old job", collection: "kb", points: 1200 });
  assert.equal(dialog.pointsText(), "1,200");
  await dialog.submit();
  assert.deepEqual(toasts, [["success", "Removed 1200 points and 2 records of <b>old job</b>"]]);
  assert.equal(calls.orphanRemoved, 1);
});

test("the defaults form keeps what it does not show and drops what was emptied", async () => {
  const stored = { embedding: { model: "bge-m3", batch_size: 8 }, filters: { exclude: ["a", "b"] }, custom: { x: 1 } };
  const { env, sent, toasts } = setup({
    "PUT /api/v1/rag/ingest/catalog/defaults": (body) => ({ ok: true, data: { defaults: body.defaults, verified: true, applied: true } }),
  });
  const form = createCatalogDefaults(env, stored);
  assert.equal(form.d.filters.exclude, "a\nb");
  assert.equal(form.d.chunking.words, "");
  form.d.embedding.batch_size = "";
  form.d.chunking.words = "400";
  form.d.filters.exclude = " a \n\n c ";
  await form.save();
  assert.deepEqual(sent[0].body.defaults, {
    embedding: { model: "bge-m3" }, filters: { exclude: ["a", "c"] }, custom: { x: 1 }, chunking: { words: 400 },
  });
  assert.deepEqual(toasts, [["success", "Defaults saved"]]);
});

test("the file editor checks, shows the first problems and marks a stale file", async () => {
  const issues = Array.from({ length: 14 }, (_, i) => ({ field: "f" + i, message: "m", job_id: i ? "kb" : null }));
  const { env, toasts } = setup({
    "POST /api/v1/rag/ingest/catalog/validate": { ok: true, data: { issues, ok: false } },
    "PUT /api/v1/rag/ingest/catalog/raw": { ok: false, error: "jobs.yaml changed since it was read" },
  });
  const editor = createRawEditor(env, { text: "version: 1\n", revision: "r1", writable: true });
  await editor.save();
  assert.equal(editor.stale, true);
  assert.equal(editor.problemsText(), "14 problems");
  assert.equal(editor.shownIssues().length, 12);
  assert.equal(editor.issueKey(issues[0]), "f0mnull");
  assert.deepEqual(toasts, []);
});

test("the job filter matches search text and group", () => {
  const row = { search: "kb handbook docs", group: "active" };
  assert.equal(rowMatches(row, { q: "", group: "all" }), true);
  assert.equal(rowMatches(row, { q: "hand", group: "active" }), true);
  assert.equal(rowMatches(row, { q: "hand", group: "disabled" }), false);
  assert.equal(rowMatches(row, { q: "nope", group: "all" }), false);
});
