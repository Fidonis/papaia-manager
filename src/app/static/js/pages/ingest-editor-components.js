// The job editor and the credential dialogs (static/js/pages/ingest-editor.js), without the page.
//
// Every factory takes `env`, what it needs from the page: `texts` (the data island
// `ingest-editor`, partials/_ingest_texts.html), `lang`, `request(method, url, body)` (the
// shared ragRequest of core/actions.js: { ok, handled, data, error }), `toast(level, html)`,
// `navigate(url)`, `here()` (the page's address as a URL), `watchLeaving(editor)` (the page
// asks the editor before a link or a reload leaves it), `askForCredential()` (opens the
// credential dialog), and `credentialSaved(name)` / `credentialDeleted(name)` (tell the page).
// So node can run them (tests/js/ingest-editor.test.mjs) and the page module wires the real ones.
//
// The Alpine components are registered with Alpine.data by the page module; their expressions
// in the templates only read fields and call methods, as the CSP build of Alpine requires.
import { escapeHtml, fmt, fmtHtml, fmtPlural } from "../core/format.js";
import { ING, ingestOutcome, jobUrl } from "./ingest-components.js";

export function linesOf(list) {
  return (list || []).join("\n");
}

// The buttons of the schedule, in their order; the labels come from the island.
export function scheduleModes(texts) {
  return texts.scheduleModes.map((mode) => ({ value: mode.value, label: mode.label }));
}

// The editor of one job. `boot` is what the server knows: the job as the form shows it, the
// choices of the form and the catalog defaults (routers/ui_ingest.py, job_editor).
export function createJobEditor(env, boot) {
  const texts = env.texts;
  const lines = linesOf;
  return {
    job: boot.state, etag: boot.etag, isNew: boot.isNew, originalId: boot.originalId,
    sources: boot.sources, credentials: boot.credentials, connections: boot.connections,
    modes: boot.modes, strategies: boot.strategies, startups: boot.startups, presets: boot.presets,
    timezones: boot.timezones, weekdays: boot.weekdays, defaults: boot.defaults, inherited: boot.inherited,
    ingestTz: boot.timezone, scheduleModes: scheduleModes(texts),
    text: { rclone: lines(boot.state.source.rclone_flags), acl: lines(boot.state.target.acl_tags), include: lines(boot.state.filters.include), exclude: lines(boot.state.filters.exclude) },
    maxMb: boot.state.filters.max_file_bytes ? boot.state.filters.max_file_bytes / 1048576 : "",
    errors: {}, summary: [], busy: false, saveError: "", dirty: false, validating: false, saving: false,
    advancedSource: false, collections: [], preview: null, pendingKey: "",
    pick: { path: "", entries: [], loading: false, error: "" },
    timers: { validate: null, preview: null }, leaving: "",

    init() {
      this.loadCollections();
      this.refreshPreview();
      this.syncModel();
      // A job that is already saved may have a problem the ingester refused it for; say so at once.
      if (!this.isNew) this.validate();
      this.$watch("job", () => { this.dirty = true; this.later("validate", 700, () => this.validate()); this.later("preview", 400, () => this.refreshPreview()); });
      this.$watch("text", () => { this.dirty = true; this.later("validate", 700, () => this.validate()); });
      this.$watch("maxMb", () => { this.dirty = true; this.later("validate", 700, () => this.validate()); });
      this.$watch("job.target.collection", () => this.syncModel());
      // A link that leaves the page asks in the app's own dialog. Only a reload, closing the tab, the
      // back button or a typed address still reach the browser's prompt, which a page cannot replace.
      env.watchLeaving(this);
    },
    blocksLeaving() { return this.dirty && !this.saving; },
    guardLink(event) {
      if (!this.dirty || this.saving || event.defaultPrevented || event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const link = event.target && event.target.closest ? event.target.closest("a[href]") : null;
      if (!link || (link.target && link.target !== "_self") || link.hasAttribute("download")) return;
      const here = env.here();
      const url = new URL(link.href, here.href);
      if (url.origin === here.origin && url.pathname === here.pathname
          && url.search === here.search) return;
      event.preventDefault();
      this.leaving = url.href;
      this.$refs.leave.showModal();
    },
    leaveNow() {
      const target = this.leaving;
      this.dirty = false;
      if (target) env.navigate(target);
    },
    later(name, ms, fn) { clearTimeout(this.timers[name]); this.timers[name] = setTimeout(fn, ms); },

    // -- texts of the form
    summaryTitle() { return fmtPlural(this.summary.length, texts.problemsToFix[0], texts.problemsToFix[1], {}, false, env.lang); },
    shownIssues() { return this.summary.slice(0, 6); },
    advancedLabel() { return this.advancedSource ? texts.advancedHide : texts.advancedShow; },
    toggleAdvancedSource() { this.advancedSource = !this.advancedSource; },

    // -- source
    form() { return this.sources.find((s) => s.type === this.job.source.type) || this.sources[0]; },
    fields() { return this.form().fields; },
    setType(type) {
      const form = this.sources.find((s) => s.type === type);
      const next = { type: type, label: this.job.source.label, rclone_flags: this.job.source.rclone_flags };
      form.fields.forEach((f) => { next[f.key] = f.default === undefined || f.default === null ? "" : f.default; });
      this.job.source = next;
    },
    // x-model fills a select before its x-for options exist, so the first option would show
    // whatever the model holds; this puts the shown option right once they are there.
    sync(el, value) { this.$nextTick(() => { el.value = value === undefined || value === null ? "" : value; }); },
    labelFromId() { if (this.isNew && !this.job.source.label && this.job.id) this.job.source.label = this.job.id; },
    credentialOptions(key) {
      const out = this.credentials.map((c) => ({ name: c.name, label: fmt(c.origin === "store" ? texts.credentialFromStore : texts.credentialFromEnvironment, { name: c.name }) }));
      const current = this.job.source[key];
      if (current && !out.some((c) => c.name === current)) out.push({ name: current, label: fmt(texts.notFound, { name: current }) });
      return out;
    },
    newCredential(key) {
      this.pendingKey = key;
      env.askForCredential();
    },
    credentialSaved(detail) {
      if (!this.credentials.some((c) => c.name === detail.name)) this.credentials.push({ name: detail.name, origin: "store" });
      if (this.pendingKey) this.job.source[this.pendingKey] = detail.name;
      this.pendingKey = "";
    },

    // -- folder picker
    openPicker() {
      const current = String(this.job.source.path || "");
      const start = current.indexOf("/data/local/") === 0 ? current.slice("/data/local/".length) : "";
      this.pick = { path: "", entries: [], loading: false, error: "" };
      this.$refs.picker.showModal();
      this.browse(start.replace(/\/+$/, ""));
    },
    parentOf(path) { const i = path.lastIndexOf("/"); return i === -1 ? "" : path.slice(0, i); },
    pickedPath() { return this.pick.path ? "/" + this.pick.path : ""; },
    async browse(path) {
      this.pick.loading = true; this.pick.error = "";
      const res = await env.request("GET", ING + "/tree?source=folder&path=" + encodeURIComponent(path));
      this.pick.loading = false;
      if (res.handled) return;
      if (!res.ok) { this.pick.error = res.error; return; }
      this.pick.path = path;
      this.pick.entries = res.data.entries;
    },
    choose() {
      this.job.source.path = "/data/local" + (this.pick.path ? "/" + this.pick.path : "");
      this.$refs.picker.close();
    },

    // -- target
    async loadCollections() {
      this.collections = [];
      const res = await env.request("GET", ING + "/collections?connection=" + encodeURIComponent(this.job.target.connection));
      if (res.handled || !res.ok) return;
      this.collections = res.data.collections || [];
      this.syncModel();
    },
    connectionMissing() { return this.connections.indexOf(this.job.target.connection) === -1; },
    connectionLabel() { return fmt(texts.notFound, { name: this.job.target.connection }); },
    currentCollection() { return this.collections.find((c) => c.name === this.job.target.collection) || null; },
    collectionNote() {
      const c = this.currentCollection();
      if (c) {
        return c.points !== null
          ? fmt(texts.collectionWithPoints, { points: c.points.toLocaleString(env.lang) })
          : texts.collectionExisting;
      }
      return this.job.target.collection ? texts.collectionNew : texts.collectionChoose;
    },
    modelLocked() { const c = this.currentCollection(); return !!(c && c.embedding_model); },
    syncModel() { const c = this.currentCollection(); if (c && c.embedding_model && this.job.embedding.model !== c.embedding_model) this.job.embedding.model = c.embedding_model; },

    // -- files
    lineSet(text) { return text.split("\n").map((l) => l.trim()).filter((l) => l); },
    presetOn(p) { const have = this.lineSet(this.text.include); return p.include.every((x) => have.indexOf(x) !== -1); },
    togglePreset(p) {
      const have = this.lineSet(this.text.include);
      this.text.include = (this.presetOn(p) ? have.filter((x) => p.include.indexOf(x) === -1) : have.concat(p.include.filter((x) => have.indexOf(x) === -1))).join("\n");
    },

    // -- schedule
    setScheduleMode(mode) { this.job.schedule.mode = mode; },
    toggleDay(day) {
      const days = this.job.schedule.weekdays.slice();
      const at = days.indexOf(day);
      if (at === -1) days.push(day); else days.splice(at, 1);
      this.job.schedule.weekdays = days;
    },
    async refreshPreview() {
      const res = await env.request("POST", ING + "/schedule/preview", { schedule: this.job.schedule });
      if (res.handled || !res.ok) return;
      this.preview = res.data;
    },
    // The next runs, as text: a value of the ingester never becomes markup.
    nextRunsText() {
      if (!this.preview || !this.preview.runs) return "";
      return fmt(texts.nextRuns, { runs: this.preview.runs.join("  ·  "), timezone: this.preview.timezone });
    },
    inheritedNote(path) {
      const value = this.defaults[path];
      return value === undefined || value === null ? "" : fmt(texts.catalogDefault, { value: Array.isArray(value) ? value.join(", ") : value });
    },

    // -- validation and saving
    payload() {
      const job = JSON.parse(JSON.stringify(this.job));
      job.source.rclone_flags = this.text.rclone;
      job.target.acl_tags = this.text.acl;
      job.filters.include = this.text.include;
      job.filters.exclude = this.text.exclude;
      job.filters.max_file_bytes = this.maxMb === "" || this.maxMb === null ? null : Math.round(Number(this.maxMb) * 1048576);
      return job;
    },
    err(field) { return this.errors[field] || ""; },
    setIssues(issues) {
      const map = {};
      issues.forEach((i) => { if (!map[i.field]) map[i.field] = i.message; });
      this.errors = map;
      this.summary = issues;
    },
    async validate() {
      this.validating = true;
      const res = await env.request("POST", ING + "/jobs/validate", { job: this.payload(), create: this.isNew, original_id: this.originalId || null });
      this.validating = false;
      if (res.handled || !res.ok) return;
      this.setIssues(res.data.issues);
    },
    async save(thenRun) {
      if (this.busy) return;
      this.saveError = ""; this.busy = true;
      const job = this.payload();
      const res = this.isNew
        ? await env.request("POST", ING + "/jobs", { job: job })
        : await env.request("PUT", jobUrl(this.originalId), { job: job, etag: this.etag || null, original_id: this.originalId });
      this.busy = false;
      if (res.handled) return;
      if (!res.ok) { this.saveError = res.error; await this.validate(); return; }
      this.saving = true; this.dirty = false;
      const out = ingestOutcome(texts, res.data, fmtHtml(texts.jobSaved, { id: job.id }));
      env.toast(out.level, out.text);
      if (thenRun) {
        const run = await env.request("POST", jobUrl(job.id) + "/run", { dry_run: true });
        if (run.ok) { env.navigate("/ingest/runs/" + encodeURIComponent(run.data.run_id)); return; }
        env.toast("error", texts.dryRunFailed + " " + escapeHtml(run.error || ""));
      }
      env.navigate("/ingest/jobs/" + encodeURIComponent(job.id));
    },
  };
}

// The dialog that stores a credential, shared by the job editor and the Credentials page
// (partials/_ingest_credential_dialog.html). What is typed goes to the manager, which encrypts
// it and never sends it back; the field is wiped as soon as it was sent.
export function createCredentialDialog(env) {
  const texts = env.texts;
  return {
    name: "", value: "", multiline: false, existing: false, busy: false, error: "",
    reset() { this.name = ""; this.value = ""; this.multiline = false; this.existing = false; this.busy = false; this.error = ""; },
    openNew(detail) {
      this.reset();
      if (detail && detail.name) this.name = String(detail.name).replace(/^QI_SECRET_/, "");
      this.$root.showModal();
    },
    openReplace(detail) {
      this.reset();
      this.name = String(detail.name).replace(/^QI_SECRET_/, "");
      this.existing = true;
      this.$root.showModal();
    },
    title() { return this.existing ? texts.credentialReplaceTitle : texts.credentialNewTitle; },
    valueLabel() { return this.multiline ? texts.credentialOneLine : texts.credentialSeveralLines; },
    toggleMultiline() { this.multiline = !this.multiline; },
    canSubmit() { return !this.busy && !!this.name.trim() && !!this.value.trim(); },
    async submit() {
      this.error = "";
      this.busy = true;
      const res = await env.request("PUT", ING + "/secrets/" + encodeURIComponent(this.name.trim()), { value: this.value });
      this.busy = false;
      this.value = "";  // gone from the page as soon as it was sent
      if (res.handled) return;
      if (!res.ok) { this.error = res.error; return; }
      this.$root.close();
      env.toast("success", fmtHtml(texts.credentialStored, { name: res.data.name }));
      env.credentialSaved(res.data.name);
    },
  };
}

// Delete confirms in a dialog of the page: a credential cannot be restored once it is gone.
export function createDeleteCredential(env) {
  const texts = env.texts;
  return {
    name: "", busy: false, error: "",
    open(detail) { this.name = detail.name; this.error = ""; this.busy = false; this.$root.showModal(); },
    async submit() {
      this.error = ""; this.busy = true;
      const res = await env.request("DELETE", ING + "/secrets/" + encodeURIComponent(this.name));
      this.busy = false;
      if (res.handled) return;
      if (!res.ok) { this.error = res.error; return; }
      this.$root.close();
      env.toast("success", fmtHtml(texts.credentialDeleted, { name: this.name }));
      env.credentialDeleted(this.name);
    },
  };
}
