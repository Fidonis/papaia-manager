// The review of an add-on upgrade (addon_upgrade.html, T-UI-03) as the Alpine component
// `addonUpgrade`. The plan is drawn from JSON; the add-on's name, whether this core can
// upgrade add-ons and the one sentence put together in the browser come from the data island
// `mgr-addon-upgrade`. Browser globals are injected, so the module runs under node --test.
import { csrfToken } from "../../core/csrf.js";
import { fmt } from "../../core/format.js";

export function createAddonUpgrade(page, { doc, fetchImpl, authGuard, clock }) {
  var name = page.name;
  var supported = page.supported;
  var T = page.texts;
  var csrf = function () { return csrfToken(doc); };
  var base = "/api/v1/addons/" + encodeURIComponent(name);
  var messageOf = async function (r) {
    try {
      var e = await r.json();
      if (e.detail && e.detail.message) return e.detail.field + ": " + e.detail.message;
      return typeof e.detail === "string" ? e.detail : JSON.stringify(e.detail || e);
    } catch (_) { return String(r.status); }
  };
  return {
    supported: supported, loading: supported, error: "", data: null, plan: null,
    values: {}, backup: true, submitting: false, applied: false,
    jobId: null, jobStatus: "", log: "",

    get missing() {
      var self = this;
      return ((this.plan && this.plan.needs_input) || []).filter(function (k) {
        return !(self.values[k] || "").trim();
      });
    },
    get canApply() {
      return !!this.plan && !this.plan.blocked && !this.plan.in_place
        && this.missing.length === 0 && !this.submitting;
    },
    get hasManifestChanges() {
      var m = this.plan.manifest;
      return this.apiChanged || m.attach.added.length || m.attach.removed.length
        || this.hasManualSteps;
    },
    get apiChanged() {
      var a = this.plan.manifest.addon_api;
      return this.show(a.from) !== this.show(a.to);
    },
    show: function (v) {
      if (v === null || v === undefined) return "–";
      return Array.isArray(v) ? v.join(", ") : String(v);
    },
    get hasManualSteps() {
      var m = this.plan.manifest;
      return m.keycloak.clients_added.length || Object.keys(m.keycloak.mappers_added).length
        || m.replace_secrets_added.length;
    },
    // The label of the button: "Upgrade to {version}" in the language of the page.
    get applyLabel() {
      return this.plan ? fmt(T.upgradeTo, { version: this.plan.candidate.version }) : "";
    },

    load: async function () {
      if (!this.supported) return;
      try {
        var r = await fetchImpl(base + "/upgrade-plan", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
          body: "{}",
        });
        if (authGuard(r)) return;
        if (!r.ok) { this.error = await messageOf(r); return; }
        var data = await r.json();
        this.data = data;
        this.plan = data.plan;
      } catch (e) {
        this.error = String(e);
      } finally {
        this.loading = false;
      }
    },

    apply: async function () {
      this.submitting = true;
      this.error = "";
      var env = {};
      var self = this;
      Object.keys(this.values).forEach(function (k) {
        var v = (self.values[k] || "").trim();
        if (v) env[k] = v;
      });
      try {
        var r = await fetchImpl(base + "/upgrade", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
          body: JSON.stringify({ env: env, revision: this.data.revision, backup: this.backup }),
        });
        if (authGuard(r)) return;
        if (!r.ok) { this.error = await messageOf(r); this.submitting = false; return; }
        var job = await r.json();
        this.jobId = job.job_id;
        this.jobStatus = "queued";
        this.applied = true;
        this.follow();
      } catch (e) {
        this.error = String(e);
        this.submitting = false;
      }
    },

    follow: function () {
      var self = this;
      var tick = async function () {
        try {
          var j = await (await fetchImpl("/api/v1/jobs/" + self.jobId)).json();
          var l = await (await fetchImpl("/api/v1/jobs/" + self.jobId + "/log")).json();
          self.jobStatus = j.status;
          self.log = l.log;
          if (j.status === "succeeded" || j.status === "failed") return;
        } catch (_) {}
        clock.setTimeout(tick, 1500);
      };
      tick();
    },
  };
}
