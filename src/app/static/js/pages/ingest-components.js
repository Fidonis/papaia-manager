// The verbs and dialogs of the ingest pages (static/js/pages/ingest.js), without the page.
//
// Every factory takes `env`, what it needs from the page: `texts` (the data island
// `ingest-page`, partials/_ingest_texts.html), `request(method, url, body)` (the shared
// ragRequest of core/actions.js: { ok, handled, data, error }), `toast(level, html)`,
// `navigate(url)`, `store` (a waiting toast that survives a navigation), `afterRunsDeleted()`
// and `today()`. So node can run them (tests/js/ingest.test.mjs) and the page module wires the
// real ones.
//
// The Alpine components are registered with Alpine.data by the page module; their expressions
// in the templates only read fields and call methods, as the CSP build of Alpine requires.
//
// Every verb answers with a toast that says what happened *and* what did not: a change that is
// saved but not active, or saved without the ingester having been asked, is told apart from one
// that is live, because they look the same in the file and not in the world.
import { escapeHtml, fmt, fmtHtml, fmtPlural } from "../core/format.js";

export const ING = "/api/v1/rag/ingest";

export function jobUrl(id) {
  return ING + "/jobs/" + encodeURIComponent(id);
}

// A counted noun of the island is [one, other].
function plural(env, count, pair, values, html) {
  return fmtPlural(count, pair[0], pair[1], values, html, env.lang);
}

// What a save or a toggle did, in a sentence for a toast.
export function ingestOutcome(texts, result, what) {
  let text = what;
  let level = "success";
  if (!result.verified) {
    level = "warning";
    text = result.note
      ? fmt(texts.outcomeNotAskedNote, { what: what, note: escapeHtml(result.note) })
      : fmt(texts.outcomeNotAsked, { what: what });
  } else if (!result.applied) {
    level = "warning";
    text = fmt(texts.outcomeNotActive, {
      what: what,
      why: result.elsewhere.length ? escapeHtml(result.elsewhere.join("; ")) : texts.outcomeNotApplied,
    });
  }
  return { level: level, text: text };
}

// -- the verbs on a job -----------------------------------------------------------------------

export function createVerbs(env) {
  const texts = env.texts;
  return {
    async toggle(id, enable, after) {
      const res = await env.request("POST", jobUrl(id) + (enable ? "/enable" : "/disable"));
      if (res.handled) return;
      const label = "<b>" + escapeHtml(id) + "</b>";
      if (!res.ok) {
        env.toast("error", label + ": " + escapeHtml(res.error));
        return;
      }
      const out = ingestOutcome(texts, res.data, fmtHtml(enable ? texts.jobEnabled : texts.jobDisabled, { id: id }));
      env.toast(out.level, out.text);
      if (after) after();
    },

    async resume(id, after) {
      const res = await env.request("POST", jobUrl(id) + "/resume");
      if (res.handled) return;
      if (!res.ok) {
        env.toast("error", escapeHtml(res.error));
        return;
      }
      env.toast("success", fmtHtml(texts.resumed, { id: id }));
      if (after) after();
    },

    async reloadCatalog(after) {
      const res = await env.request("POST", ING + "/reload");
      if (res.handled) return;
      if (!res.ok) {
        env.toast("error", escapeHtml(res.error));
        return;
      }
      env.toast(res.data.refused ? "warning" : "success", res.data.refused ? texts.reloadRefused : texts.reloaded);
      if (after) after();
    },

    async abort(runId, after) {
      const res = await env.request("DELETE", ING + "/job-runs/" + encodeURIComponent(runId));
      if (res.handled) return;
      if (!res.ok) {
        env.toast("error", escapeHtml(res.error));
        return;
      }
      env.toast("info", texts.abortRequested);
      if (after) after();
    },
  };
}

// A toast that has to be read after the page changes (a deleted job's page is gone) waits in the
// session and is shown by the next ingest page. Storage can be blocked: then the caller shows it
// at once and it is lost with the page, which is all there was before.
export const WAITING_TOAST = "ingestToast";

export function showWaitingToast(storage, toast) {
  try {
    const raw = storage.getItem(WAITING_TOAST);
    if (!raw) return;
    storage.removeItem(WAITING_TOAST);
    const waiting = JSON.parse(raw);
    toast(waiting.type, waiting.html);
  } catch (_) {
    // nothing was waiting, or storage is not available
  }
}

// -- the dialogs ------------------------------------------------------------------------------

// The run dialog. The row that opens it hands over what the dialog has to know about the job,
// so it needs no request of its own to open.
export function createRunDialog(env) {
  return {
    job: { id: "", collection: "", mode: "", mode_label: "", full_scope: "job", remote: false, first_run: false, siblings: [] },
    mode: "", dry: false, skipSync: false, keep: false, confirmRebuild: false, typed: "",
    busy: false, error: "", advanced: false,
    open(detail) {
      env.closeMenu();
      this.job = detail;
      this.mode = "";
      // The first run of a job is the one worth looking at before it writes anything.
      this.dry = !!detail.first_run;
      this.skipSync = false; this.keep = false; this.confirmRebuild = false; this.typed = "";
      this.error = ""; this.advanced = false; this.busy = false;
      this.$root.showModal();
    },
    modeOption() { return fmt(env.texts.modeAsSetUp, { mode: this.job.mode_label }); },
    toggleAdvanced() { this.advanced = !this.advanced; },
    effectiveMode() { return this.mode ? this.mode.split("/")[0] : this.job.mode; },
    effectiveScope() {
      if (this.mode === "full/collection") return "collection";
      if (this.mode === "full/job") return "job";
      return this.job.full_scope;
    },
    rebuild() { return this.effectiveMode() === "full"; },
    wholeCollection() { return this.rebuild() && this.effectiveScope() === "collection"; },
    canSubmit() {
      if (this.busy) return false;
      if (this.dry) return true;
      if (this.rebuild() && !this.confirmRebuild) return false;
      if (this.wholeCollection() && this.typed !== this.job.collection) return false;
      return true;
    },
    body() {
      const body = {
        dry_run: this.dry,
        skip_sync: this.skipSync && this.job.remote,
        delete_vanished: !(this.keep && this.effectiveMode() === "upsert"),
      };
      if (this.mode) {
        const parts = this.mode.split("/");
        body.mode = parts[0];
        if (parts[1]) body.full_scope = parts[1];
      }
      if (!this.dry) {
        body.confirm_rebuild = this.confirmRebuild;
        body.confirm_collection = this.typed === this.job.collection;
        if (this.wholeCollection() && this.job.siblings.length) body.force = true;
      }
      return body;
    },
    async submit() {
      this.error = "";
      this.busy = true;
      const res = await env.request("POST", jobUrl(this.job.id) + "/run", this.body());
      this.busy = false;
      if (res.handled) return;
      if (!res.ok) { this.error = res.error; return; }
      this.$root.close();
      env.navigate("/ingest/runs/" + encodeURIComponent(res.data.run_id));
    },
  };
}

// The delete dialog: the job's entry and its runs always go; what it embedded goes only on request.
export function createDeleteJobDialog(env) {
  const texts = env.texts;
  return {
    job: { id: "", etag: "", collection: "", managed: false, can_delete_runs: false },
    purge: false, typed: "", busy: false, error: "", runs: null,
    open(detail) {
      env.closeMenu();
      this.job = detail; this.purge = false; this.typed = ""; this.error = ""; this.busy = false;
      this.runs = null;
      this.$root.showModal();
      if (detail.can_delete_runs) this.countRuns(detail.id);
    },
    // Asks the ingester how many runs would go; the warning reads the same without the number.
    async countRuns(id) {
      const res = await env.request("POST", jobUrl(id) + "/runs/delete", { dry_run: true });
      if (res.ok && this.job.id === id) this.runs = res.data.matched;
    },
    runsText() {
      if (this.runs === null) return "";
      if (this.runs === 0) return texts.noFinishedRuns;
      return plural(env, this.runs, texts.runsWillBeDeleted);
    },
    canSubmit() { return !this.busy && (!this.purge || this.typed === this.job.id); },
    async submit() {
      this.error = "";
      this.busy = true;
      const res = await env.request("DELETE", jobUrl(this.job.id) +
        "?purge=" + (this.purge ? "true" : "false") + "&etag=" + encodeURIComponent(this.job.etag || ""));
      this.busy = false;
      if (res.handled) return;
      if (!res.ok) { this.error = res.error; return; }
      this.$root.close();
      let text = fmtHtml(texts.jobDeleted, { id: this.job.id });
      if (res.data.deleted_runs) {
        text += "; " + plural(env, res.data.deleted_runs, texts.runsDeleted);
      }
      if (res.data.purged) text += "; " + fmt(texts.pointsRemoved, { count: res.data.deleted_points });
      if (res.data.note) text += ". " + escapeHtml(res.data.note);
      // The job's page is gone, so the answer has to be read on the list; a toast does not survive
      // the navigation, so it waits in the session until the next page shows it.
      const level = res.data.note ? "warning" : "success";
      try {
        env.store.setItem(WAITING_TOAST, JSON.stringify({ type: level, html: text }));
      } catch (_) {
        env.toast(level, text);
      }
      env.navigate("/ingest/jobs");
    },
  };
}

// Delete everything before the last `days` days: the last day to go is `days` days ago.
export function keepUntil(today, days) {
  const d = new Date(today.getTime());
  d.setDate(d.getDate() - days);
  const pad = function (n) { return String(n).padStart(2, "0"); };
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
}

// The delete-runs dialog: the history of one job, all of it or whole days. It asks the ingester what
// it would delete before it deletes anything. It never touches what the job embedded.
export function createDeleteRunsDialog(env) {
  const texts = env.texts;
  return {
    jobId: "", jobs: [], choose: false, tz: "",
    since: "", until: "", typed: "",
    count: null, counting: false, error: "", busy: false, seq: 0,
    open(detail) {
      env.closeMenu();
      this.jobId = detail.job_id || ""; this.jobs = detail.jobs || []; this.choose = !!detail.choose;
      this.tz = detail.tz || "";
      this.since = ""; this.until = ""; this.typed = ""; this.error = ""; this.busy = false;
      this.count = null;
      this.$root.showModal();
      this.refresh();
    },
    keep(days) {
      this.since = "";
      this.until = keepUntil(env.today(), days);
      this.refresh();
    },
    unbounded() { return !this.since && !this.until; },
    hasRuns() { return !this.counting && !this.error && !!this.count && this.count.matched > 0; },
    body(dry) { return { since: this.since || null, until: this.until || null, dry_run: dry }; },
    // The question is asked again for every change; an answer to an older question is dropped.
    async refresh() {
      const seq = ++this.seq;
      this.error = ""; this.count = null;
      if (!this.jobId) return;
      if (this.since && this.until && this.since > this.until) {
        this.error = texts.firstAfterLast;
        return;
      }
      this.counting = true;
      const res = await env.request("POST", jobUrl(this.jobId) + "/runs/delete", this.body(true));
      if (seq !== this.seq) return;
      this.counting = false;
      if (res.handled) return;
      if (!res.ok) { this.error = res.error; return; }
      this.count = res.data;
    },
    previewText() {
      if (this.counting || !this.count) return texts.counting;
      const c = this.count;
      let text = c.matched === 0
        ? texts.noRunInPeriod
        : fmt(texts.willBeDeleted, {
            runs: plural(env, c.matched, texts.runCount),
            events: plural(env, c.matched_events, texts.logLines),
          });
      if (c.skipped_running) {
        text += " " + plural(env, c.skipped_running, texts.stillWorking);
      }
      return text;
    },
    canSubmit() {
      return !this.busy && this.hasRuns() && (!this.unbounded() || this.typed === this.jobId);
    },
    submitText() {
      return this.hasRuns() ? plural(env, this.count.matched, texts.deleteCount) : texts.delete;
    },
    async submit() {
      if (!this.canSubmit()) return;
      this.error = "";
      this.busy = true;
      const res = await env.request("POST", jobUrl(this.jobId) + "/runs/delete", this.body(false));
      this.busy = false;
      if (res.handled) return;
      if (!res.ok) { this.error = res.error; return; }
      this.$root.close();
      const n = res.data.deleted_runs;
      let text = plural(env, n, texts.runsOfJobDeleted, { jobId: this.jobId }, true);
      if (res.data.skipped_running) text += ". " + texts.runKept;
      env.toast(res.data.skipped_running ? "warning" : "success", text);
      // Whichever page this is shows runs somewhere: its own refresh, else a reload.
      env.afterRunsDeleted();
    },
  };
}

// Removing leftovers deletes points from a collection, so it confirms by the id of the job.
export function createDeleteOrphan(env) {
  return {
    item: { job_id: "", collection: "", points: 0 }, typed: "", busy: false, error: "",
    open(detail) { this.item = detail; this.typed = ""; this.error = ""; this.busy = false; this.$root.showModal(); },
    pointsText() { return (this.item.points || 0).toLocaleString(env.lang); },
    async submit() {
      this.error = ""; this.busy = true;
      const res = await env.request("DELETE", ING + "/orphans/" + encodeURIComponent(this.item.job_id));
      this.busy = false;
      if (res.handled) return;
      if (!res.ok) { this.error = res.error; return; }
      this.$root.close();
      env.toast("success", fmtHtml(env.texts.orphanRemoved, { points: res.data.deleted_points, rows: res.data.deleted_rows, job: this.item.job_id }));
      env.afterOrphanRemoved();
    },
  };
}

// -- the job file -------------------------------------------------------------------------------

function copy(value) {
  return JSON.parse(JSON.stringify(value));
}

// The defaults every job inherits, as a form over the stored section. What is not in the form is
// not touched: the catalog may carry settings of its own.
export function createCatalogDefaults(env, stored) {
  const pick = (section, key) => (stored[section] && stored[section][key] !== undefined && stored[section][key] !== null) ? stored[section][key] : "";
  const exclude = (stored.filters && stored.filters.exclude) ? stored.filters.exclude.join("\n") : "";
  return {
    d: {
      embedding: { model: pick("embedding", "model"), batch_size: pick("embedding", "batch_size") },
      chunking: { strategy: pick("chunking", "strategy"), words: pick("chunking", "words"), overlap: pick("chunking", "overlap") },
      filters: { exclude: exclude },
      schedule: { timezone: pick("schedule", "timezone") },
      safety: { max_delete_ratio: pick("safety", "max_delete_ratio") },
    },
    base: copy(stored),
    busy: false, error: "",
    build() {
      const out = copy(this.base);
      const set = (section, key, value, numeric) => {
        const empty = value === "" || value === null || value === undefined;
        out[section] = out[section] || {};
        if (empty) delete out[section][key]; else out[section][key] = numeric ? Number(value) : value;
        if (!Object.keys(out[section]).length) delete out[section];
      };
      set("embedding", "model", String(this.d.embedding.model).trim(), false);
      set("embedding", "batch_size", this.d.embedding.batch_size, true);
      set("chunking", "strategy", this.d.chunking.strategy, false);
      set("chunking", "words", this.d.chunking.words, true);
      set("chunking", "overlap", this.d.chunking.overlap, true);
      const lines = String(this.d.filters.exclude).split("\n").map((l) => l.trim()).filter((l) => l);
      set("filters", "exclude", lines.length ? lines : "", false);
      set("schedule", "timezone", String(this.d.schedule.timezone).trim(), false);
      set("safety", "max_delete_ratio", this.d.safety.max_delete_ratio, true);
      return out;
    },
    async save() {
      this.error = ""; this.busy = true;
      const res = await env.request("PUT", ING + "/catalog/defaults", { defaults: this.build() });
      this.busy = false;
      if (res.handled) return;
      if (!res.ok) { this.error = res.error; return; }
      this.base = copy(res.data.defaults);
      const out = ingestOutcome(env.texts, res.data, env.texts.defaultsSaved);
      env.toast(out.level, out.text);
    },
  };
}

// jobs.yaml itself, edited exactly as typed.
export const SHOWN_ISSUES = 12;

export function createRawEditor(env, initial) {
  return {
    text: initial.text, revision: initial.revision, writable: !!initial.writable,
    issues: [], ok: false, error: "", busy: false, checked: false, stale: false,
    shownIssues() { return this.issues.slice(0, SHOWN_ISSUES); },
    issueKey(issue) { return issue.field + issue.message + String(issue.job_id); },
    problemsText() { return plural(env, this.issues.length, env.texts.problems); },
    async check() {
      this.error = ""; this.ok = false; this.busy = true;
      const res = await env.request("POST", ING + "/catalog/validate", { text: this.text });
      this.busy = false;
      if (res.handled) return;
      if (!res.ok) { this.error = res.error; return; }
      this.issues = res.data.issues; this.ok = res.data.ok; this.checked = true;
    },
    async save() {
      this.error = ""; this.ok = false; this.busy = true; this.stale = false;
      const res = await env.request("PUT", ING + "/catalog/raw", { text: this.text, revision: this.revision });
      this.busy = false;
      if (res.handled) return;
      if (!res.ok) {
        this.error = res.error;
        this.stale = /changed since|changed while/.test(res.error);
        await this.check();
        return;
      }
      this.issues = []; this.revision = res.data.raw.revision;
      const out = ingestOutcome(env.texts, res.data, env.texts.fileSaved);
      env.toast(out.level, out.text);
    },
  };
}

// -- the job list filter ----------------------------------------------------------------------

// Rows carry what they can be found by and which group they belong to; the filter hides rows
// instead of asking the server, so it answers as fast as it is typed.
export function rowMatches(row, filter) {
  return (!filter.q || row.search.indexOf(filter.q) !== -1) &&
    (filter.group === "all" || row.group === filter.group);
}
