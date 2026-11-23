// Modules (modules.html), T-UI-03: the overview of the delegations people granted to the
// modules, as the Alpine component `delegationOverview`. Only counts are loaded, never
// contents, purposes or tokens; "Revoke" ends every delegation of one person at one module
// after a question. The sentences come from the data island `delegation-texts`. Everything
// the component talks to comes in as an argument; the bottom of this file wires up the page.
import { csrfToken } from "../core/csrf.js";
import { fmt, fmtHtml } from "../core/format.js";
import { readIsland } from "../core/islands.js";
import { pageCore } from "../core/page.js";

export function delegationOverview({ texts, coreTexts, actions, fetchImpl, show, confirm, doc }) {
  const T = texts;
  return {
    modules: [],
    error: "",
    load() {
      return fetchImpl("/api/v1/delegations")
        .then((r) => {
          if (actions.authGuard(r)) return null;
          return r.json().then((body) => (r.ok ? body : Promise.reject(body.detail || r.status)));
        })
        .then((body) => { if (body) { this.modules = body.modules; this.error = ""; } })
        .catch((detail) => { this.error = fmt(T.failed, { detail: String(detail) }); });
    },
    revoke(m, p) {
      const values = { person: p.username || p.user_id, module: m.title };
      if (!confirm(fmt(T.confirm, values))) return Promise.resolve();
      return fetchImpl("/api/v1/delegations/" + encodeURIComponent(m.module) + "/persons/" + encodeURIComponent(p.user_id), {
        method: "DELETE",
        headers: { "X-CSRF-Token": csrfToken(doc) },
      }).then((r) => {
        if (actions.authGuard(r)) return;
        if (r.ok) {
          show("success", fmtHtml(T.revoked, values));
        } else {
          r.json().then((body) => show("error", fmtHtml(coreTexts.requestRefused, { detail: body.detail || r.status })));
        }
        this.load();
      });
    },
  };
}

// The page: the component is registered when Alpine starts, which is after this module ran
// (base.html loads page modules before Alpine).
if (globalThis.document) {
  document.addEventListener("alpine:init", () => {
    const { actions, texts, toaster } = pageCore();
    window.Alpine.data("delegationOverview", () =>
      delegationOverview({
        texts: readIsland("delegation-texts"),
        coreTexts: texts,
        actions,
        fetchImpl: (...args) => window.fetch(...args),
        show: toaster.show,
        confirm: (text) => window.confirm(text),
        doc: document,
      }));
  });
}
