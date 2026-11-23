// Connections (connections.html and the list htmx swaps in, partials/connection_list.html).
// The page carries no inline script and no event attribute (T-UI-03): it loads this module in
// the block `page_modules` of base.html, before Alpine starts, and the module registers the
// dialogs and the row component of the list as Alpine components on `alpine:init`. Its
// sentences come from the data island `mgr-connections`, the registered connection types
// from the island `mgr-connection-types` of the list.
import { ragDetail } from "../core/actions.js";
import { escapeHtml, fmt, fmtHtml } from "../core/format.js";
import { readIsland } from "../core/islands.js";
import { actions, showToast } from "../core/runtime.js";

const texts = readIsland("mgr-connections").texts;

function reloadConnections() {
  window.htmx.ajax("GET", "/partials/connections", { target: "#connection-list", swap: "innerHTML" });
}

// htmx does not swap a 4xx answer, so a failed load would leave the spinner up.
document.body.addEventListener("htmx:responseError", function (event) {
  const detail = event.detail || {};
  if (!detail.target || detail.target.id !== "connection-list") return;
  if (detail.xhr && detail.xhr.status === 401) return;
  let reason = texts.listFailed;
  try { reason = ragDetail(JSON.parse(detail.xhr.responseText), detail.xhr.status); } catch (_) {}
  detail.target.innerHTML =
    '<div class="flex flex-col items-center py-24 gap-3 text-pk-text-muted text-center">' +
    '<p class="text-lg font-semibold">' + escapeHtml(texts.listUnavailable) + "</p>" +
    '<p class="max-w-xl text-sm">' + escapeHtml(reason) + "</p></div>";
});

function _connUrl(name) {
  return "/api/v1/rag/connections" + (name === undefined ? "" : "/" + encodeURIComponent(name));
}

// Test a stored connection from its row: its own key, at its own address.
async function testConnection(name) {
  const res = await actions.ragRequest("POST", _connUrl() + "/test", { name: name });
  if (res.handled) return;
  const label = "<b>" + escapeHtml(name) + "</b>: ";
  if (!res.ok) { showToast("error", label + escapeHtml(res.error)); return; }
  showToast(res.data.ok ? "success" : "error", label + escapeHtml(res.data.detail));
}

// The Test button of a row hands itself in; the name of its connection is its
// `data-connection`.
function connectionRow() {
  return {
    testing: false,
    async test(button) {
      this.testing = true;
      try {
        await testConnection(button.dataset.connection);
      } finally {
        this.testing = false;
      }
    },
  };
}

// The create and edit dialog. What it asks for comes from the registered types, so a
// type added to the manager shows up here without a change to this page.
function connectionDialog() {
  const types = readIsland("mgr-connection-types");
  return {
    types: types,
    mode: "create", name: "", type: types.length ? types[0].id : "qdrant",
    fields: {}, original: {}, apiKey: "", clearKey: false, hasKey: false,
    etag: "", usedBy: [], confirmJobs: false,
    busy: false, testing: false, error: "", result: null,
    fmt: fmt,
    specs() {
      const found = this.types.find(function (t) { return t.id === this.type; }, this);
      return found ? found.fields : [];
    },
    plain() { return this.specs().filter(function (s) { return s.kind !== "secret"; }); },
    secret() { return this.specs().find(function (s) { return s.kind === "secret"; }) || null; },
    trimmed() {
      const out = {};
      const self = this;
      this.plain().forEach(function (s) { out[s.name] = String(self.fields[s.name] || "").trim(); });
      return out;
    },
    moved() {
      if (this.mode !== "edit") return false;
      const now = this.trimmed();
      const before = this.original;
      return Object.keys(now).some(function (k) { return now[k] !== String(before[k] || "").trim(); });
    },
    usedByText() { return this.usedBy.join(", "); },
    keyCleared() { if (this.clearKey) this.apiKey = ""; },
    reset() {
      this.mode = "create"; this.name = ""; this.fields = {}; this.original = {};
      this.apiKey = ""; this.clearKey = false; this.hasKey = false; this.etag = "";
      this.usedBy = []; this.confirmJobs = false; this.busy = false; this.testing = false;
      this.error = ""; this.result = null;
    },
    openNew() { this.reset(); this.$root.showModal(); },
    openEdit(detail) {
      this.reset();
      this.mode = "edit";
      this.name = detail.name;
      this.type = detail.type;
      this.fields = Object.assign({}, detail.fields);
      this.original = Object.assign({}, detail.fields);
      this.hasKey = detail.hasKey;
      this.etag = detail.etag;
      this.usedBy = detail.usedBy;
      this.$root.showModal();
    },
    async submit() {
      this.error = "";
      this.busy = true;
      const key = this.apiKey === "" ? null : this.apiKey;
      let res;
      if (this.mode === "create") {
        res = await actions.ragRequest("POST", _connUrl(), {
          name: this.name.trim(), type: this.type, fields: this.trimmed(), api_key: key,
        });
      } else {
        res = await actions.ragRequest("PUT", _connUrl(this.name), {
          fields: this.trimmed(), api_key: key, clear_api_key: this.clearKey,
          etag: this.etag, confirm_jobs: this.confirmJobs,
        });
      }
      this.busy = false;
      if (res.handled) return;
      if (!res.ok) { this.error = res.error; return; }
      const name = this.name.trim();
      const created = this.mode === "create";
      this.$root.close();
      showToast("success", fmtHtml(created ? texts.created : texts.saved, { name: name }));
      reloadConnections();
    },
    async test() {
      this.result = null;
      this.testing = true;
      const key = this.apiKey === "" ? null : this.apiKey;
      const res = await actions.ragRequest("POST", _connUrl() + "/test", {
        name: this.mode === "edit" ? this.name : null,
        type: this.type, fields: this.trimmed(), api_key: key,
      });
      this.testing = false;
      if (res.handled) return;
      this.result = res.ok ? res.data : { ok: false, detail: res.error };
    },
  };
}

function deleteConnection() {
  return {
    name: "", etag: "", busy: false, error: "",
    open(detail) {
      this.name = detail.name;
      this.etag = detail.etag;
      this.error = "";
      this.$root.showModal();
    },
    async submit() {
      this.error = "";
      this.busy = true;
      const res = await actions.ragRequest("DELETE", _connUrl(this.name) + "?etag=" + encodeURIComponent(this.etag));
      this.busy = false;
      if (res.handled) return;
      if (!res.ok) { this.error = res.error; return; }
      this.$root.close();
      showToast("success", fmtHtml(texts.deleted, { name: this.name }));
      reloadConnections();
    },
  };
}

function resetConnection() {
  return {
    name: "", busy: false, error: "",
    open(detail) {
      this.name = detail.name;
      this.error = "";
      this.$root.showModal();
    },
    async submit() {
      this.error = "";
      this.busy = true;
      const res = await actions.ragRequest("POST", _connUrl(this.name) + "/reset");
      this.busy = false;
      if (res.handled) return;
      if (!res.ok) { this.error = res.error; return; }
      this.$root.close();
      showToast("success", fmtHtml(texts.reset, { name: this.name }));
      reloadConnections();
    },
  };
}

document.addEventListener("alpine:init", () => {
  window.Alpine.data("connectionRow", connectionRow);
  window.Alpine.data("connectionDialog", connectionDialog);
  window.Alpine.data("deleteConnection", deleteConnection);
  window.Alpine.data("resetConnection", resetConnection);
});
