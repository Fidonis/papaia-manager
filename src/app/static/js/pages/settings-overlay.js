// Settings -> Overlay (settings_overlay.html): the editor of the overlay files, their effective
// result and their saved versions, as the Alpine component `overlayEditor`.
//
// The server's values come from two data islands: `overlay-init` (the targets, the other
// files and the selected target) and `overlay-texts` (the sentences, in the language of the
// page). The template's expressions call only what the component offers, the sentence helpers
// fmt() and fmtPlural() among them, so they run on the CSP build of Alpine. Everything the
// component talks to comes in as an argument; the bottom of this file wires up the page.
import { escapeHtml, fmt, fmtPlural } from "../core/format.js";
import { readIsland } from "../core/islands.js";
import { pageCore } from "../core/page.js";

const BASE = "/api/v1/config/overlay/";
const CHANGED_AFTER = /changed after/;
// The toast of the outcome stays readable for a moment before the page is opened again.
const RETURN_MS = 900;

export function overlayEditor({ init, texts, request, show, pollJob, refreshStrip, events, navigate, reload, clock }) {
  return {
    targets: init.targets,
    other: init.other,
    key: init.selected,
    previewAvailable: init.preview_available,
    file: null,
    draft: "",
    saved: "",
    tab: "overlay",
    problems: [],
    summary: "",
    versions: [],
    effective: null,
    previewing: false,
    previewError: "",
    previewUnavailable: false,
    versionDiff: null,
    versionDiffFor: null,
    restoredFrom: "",
    conflict: false,
    busy: false,
    confirmKind: "apply",
    confirmVersion: null,
    anyway: false,
    following: false,

    // The sentence helpers of the template's expressions.
    fmt,
    fmtPlural,

    get current() { return this.targets.find((t) => t.key === this.key); },
    get confirmTitle() {
      if (this.confirmKind === "rollback") return fmt(texts.rollbackTitle, { when: this.when(this.confirmVersion.when) });
      return fmt(texts.applyTitle, { label: this.current.label });
    },
    get confirmButton() { return this.confirmKind === "rollback" ? texts.rollbackButton : texts.applyButton; },

    async start() {
      await this.select(this.key);
      let wasActive = false;
      events.addEventListener("papaia-config-job", (event) => {
        if (this.following) return;
        if (event.detail.active) wasActive = true;
        else if (wasActive) reload();
      });
    },

    async select(key) {
      this.key = key;
      this.tab = "overlay";
      this.effective = null;
      this.previewError = "";
      this.versionDiff = null;
      this.restoredFrom = "";
      this.conflict = false;
      const reply = await request("GET", BASE + key);
      if (!reply.ok) {
        if (!reply.handled) show("error", escapeHtml(reply.error));
        this.file = null;
        return;
      }
      this.file = reply.data;
      this.draft = reply.data.content;
      this.saved = reply.data.content;
      this.versions = reply.data.versions;
      this.previewAvailable = reply.data.preview_available;
      const t = this.targets.findIndex((x) => x.key === key);
      this.targets[t] = Object.assign({}, this.targets[t], {
        exists: reply.data.exists, lines: reply.data.lines, revision: reply.data.revision,
      });
      await this.runCheck();
    },

    lineNumbers() {
      const n = Math.max(1, this.draft.split("\n").length);
      return Array.from({ length: n }, (_, i) => i + 1).join("\n");
    },

    async runCheck() {
      const reply = await request("POST", BASE + this.key + "/check", { content: this.draft });
      if (!reply.ok) return;
      this.problems = reply.data.problems;
      this.summary = reply.data.summary;
      this.effective = null;
    },

    async showEffective() {
      if (this.problems.length) return;
      this.tab = "effective";
      this.previewing = true;
      this.previewError = "";
      this.previewUnavailable = false;
      this.effective = null;
      const reply = await request("POST", BASE + this.key + "/preview", { content: this.draft });
      this.previewing = false;
      if (!reply.ok) {
        if (!reply.handled) this.previewError = reply.error;
        return;
      }
      const r = reply.data;
      if (r.ok) this.effective = r;
      else if (r.available === false) this.previewUnavailable = true;
      else if (r.problems && r.problems.length) this.previewError = r.problems[0].message;
      else this.previewError = r.error || texts.cannotMerge;
    },

    applyNote() {
      if (!this.current) return "";
      return this.current.group_active
        ? fmtPlural(this.current.services, texts.applyNoteOne, texts.applyNoteOther, { label: this.current.label })
        : fmt(texts.applyNoteOff, { label: this.current.label });
    },

    when(iso) {
      const d = new Date(iso);
      return d.toLocaleString(undefined, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
    },

    askApply() {
      this.confirmKind = "apply";
      this.anyway = false;
      this.$refs.confirmDialog.showModal();
    },

    // After a conflict: the same dialog, which says the host's version is saved first.
    askApplyAnyway() {
      this.confirmKind = "apply";
      this.anyway = true;
      this.$refs.confirmDialog.showModal();
    },

    askRollback(version) {
      this.confirmKind = "rollback";
      this.confirmVersion = version;
      this.$refs.confirmDialog.showModal();
    },

    async confirmAction() {
      this.$refs.confirmDialog.close();
      if (this.confirmKind === "rollback") await this.rollback(this.confirmVersion);
      else if (this.anyway) await this.applyAnyway();
      else await this.apply(this.file.revision);
    },

    async apply(revision) {
      this.busy = true;
      const reply = await request("PUT", BASE + this.key, { content: this.draft, revision: revision });
      this.busy = false;
      if (!reply.ok) {
        if (reply.handled) return;
        if (CHANGED_AFTER.test(reply.error)) this.conflict = true;
        else show("error", escapeHtml(reply.error));
        return;
      }
      this.follow(reply.data.job_id, texts.applying);
    },

    async rollback(version) {
      this.busy = true;
      const reply = await request("POST", BASE + this.key + "/rollback", { version: version.id, revision: this.file.revision });
      this.busy = false;
      if (!reply.ok) {
        if (reply.handled) return;
        if (CHANGED_AFTER.test(reply.error)) this.conflict = true;
        else show("error", escapeHtml(reply.error));
        return;
      }
      this.follow(reply.data.job_id, texts.rollingBack);
    },

    async discard() {
      this.$refs.discard.close();
      const reply = await request("DELETE", BASE + this.key + "?revision=" + encodeURIComponent(this.file.revision));
      if (!reply.ok) {
        if (reply.handled) return;
        if (CHANGED_AFTER.test(reply.error)) this.conflict = true;
        else show("error", escapeHtml(reply.error));
        return;
      }
      this.follow(reply.data.job_id, texts.discarding);
    },

    // The host's file is read again for its revision only; the draft stays what it is.
    async applyAnyway() {
      const reply = await request("GET", BASE + this.key);
      if (!reply.ok) {
        if (!reply.handled) show("error", escapeHtml(reply.error));
        return;
      }
      this.file = reply.data;
      this.conflict = false;
      await this.apply(reply.data.revision);
    },

    async reloadFile() { await this.select(this.key); },

    async viewVersion(version) {
      const reply = await request("GET", BASE + this.key + "/versions/" + version.id);
      if (!reply.ok) {
        if (!reply.handled) show("error", escapeHtml(reply.error));
        return;
      }
      this.versionDiff = reply.data.diff;
      this.versionDiffFor = version;
    },

    async restoreDraft(version) {
      const reply = await request("GET", BASE + this.key + "/versions/" + version.id);
      if (!reply.ok) {
        if (!reply.handled) show("error", escapeHtml(reply.error));
        return;
      }
      this.draft = reply.data.content;
      this.restoredFrom = this.when(version.when);
      this.tab = "overlay";
      await this.runCheck();
    },

    follow(jobId, message) {
      this.following = true;
      show("info", message);
      refreshStrip();
      pollJob(jobId, (job) => {
        if (job.status === "succeeded") show("success", texts.done);
        else show("error", texts.failed);
        clock.setTimeout(() => navigate("/settings/overlay?target=" + this.key), RETURN_MS);
      });
    },
  };
}

// The page: the component is registered when Alpine starts, which is after this module ran
// (base.html loads page modules before Alpine).
if (globalThis.document) {
  document.addEventListener("alpine:init", () => {
    const { actions, toaster } = pageCore();
    window.Alpine.data("overlayEditor", () =>
      overlayEditor({
        init: readIsland("overlay-init"),
        texts: readIsland("overlay-texts"),
        request: actions.ragRequest,
        show: toaster.show,
        pollJob: actions.pollJob,
        refreshStrip: () =>
          window.htmx.ajax("GET", "/partials/config/job-status", { target: "#config-job-status", swap: "outerHTML" }),
        events: window,
        navigate: (url) => {
          window.location.href = url;
        },
        reload: () => window.location.reload(),
        clock: window,
      }),
    );
  });
}
