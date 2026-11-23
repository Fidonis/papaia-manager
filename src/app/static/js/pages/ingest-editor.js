// The page module of the job editor (ingest_job_edit.html: /ingest/new, /ingest/jobs/{id}/edit)
// and the Credentials page (ingest_secrets.html), loaded in base.html's block `page_modules`,
// before Alpine starts.
//
// The page tells which one it is, and hands its values and sentences over, in the data island
// `ingest-editor` (partials/_ingest_texts.html); the editor's start values are its `values`.
// Buttons of the page frame name their verb in `data-ingest-editor-action`, and one listener on
// the document runs them. The editor and the dialogs are Alpine components registered here; the
// credential dialog, which both pages include, tells the page about a stored credential with the
// window event `credential-saved`, which the editor and the credentials list listen to.
import { ragDetail } from "../core/actions.js";
import { readIsland } from "../core/islands.js";
import { actions, toaster } from "../core/runtime.js";
import {
  createCredentialDialog,
  createDeleteCredential,
  createJobEditor,
} from "./ingest-editor-components.js";

const island = readIsland("ingest-editor");
const page = island.page;
const values = island.values || {};

const SECRETS = { target: "#secret-list", url: "/partials/ingest/secrets" };

function announce(name, detail) {
  window.dispatchEvent(new CustomEvent(name, { detail: detail }));
}

function reloadSecrets() {
  window.htmx.ajax("GET", SECRETS.url, { target: SECRETS.target, swap: "innerHTML" });
}

const env = {
  texts: island.texts,
  lang: document.documentElement.lang || undefined,
  request: actions.ragRequest,
  toast: (level, html) => toaster.show(level, html),
  navigate: (url) => {
    window.location.href = url;
  },
  here: () => new URL(window.location.href),
  // A link out of a dirty editor asks in the app's own dialog; a reload, a closed tab or the back
  // button still reach the browser's prompt, the last resort.
  watchLeaving: (editor) => {
    document.addEventListener("click", (event) => editor.guardLink(event), true);
    window.addEventListener("beforeunload", (event) => {
      if (editor.blocksLeaving()) {
        event.preventDefault();
        event.returnValue = "";
      }
    });
  },
  askForCredential: () => announce("credential-new", {}),
  credentialSaved: (name) => announce("credential-saved", { name: name }),
  credentialDeleted: () => reloadSecrets(),
};

// -- the verbs of the buttons ---------------------------------------------------------------------

const VERBS = {
  // The header's Save only asks the form to save; the form's own buttons are its component's.
  save: () => announce("ingest-save", { run: false }),
  refresh: () => reloadSecrets(),
  "credential-new": () => announce("credential-new", {}),
};

document.addEventListener("click", (event) => {
  const element = event.target.closest ? event.target.closest("[data-ingest-editor-action]") : null;
  if (!element) return;
  const verb = VERBS[element.dataset.ingestEditorAction];
  if (verb) verb(element);
});

// -- the credentials list -------------------------------------------------------------------------

// A credential stored in the dialog shows up in the list at once.
window.addEventListener("credential-saved", () => {
  if (page === "secrets") reloadSecrets();
});

// htmx does not swap a 4xx answer, so a failed load would leave the spinner up. The panel is the
// page's <template id="ingest-failure">; the reason goes in as text.
document.addEventListener("htmx:responseError", (event) => {
  const detail = event.detail || {};
  if (page !== "secrets" || !detail.target || "#" + detail.target.id !== SECRETS.target) return;
  if (detail.xhr && detail.xhr.status === 401) return;
  const template = document.getElementById("ingest-failure");
  if (!template) return;
  let reason = template.dataset.fallback;
  try {
    reason = ragDetail(JSON.parse(detail.xhr.responseText), detail.xhr.status);
  } catch (_) {
    // the answer carries no detail; the fallback stands
  }
  const panel = template.content.cloneNode(true);
  panel.querySelector("[data-reason]").textContent = reason;
  detail.target.replaceChildren(panel);
});

// -- the components -------------------------------------------------------------------------------

document.addEventListener("alpine:init", () => {
  const Alpine = window.Alpine;
  if (page === "editor") Alpine.data("jobEditor", () => createJobEditor(env, values));
  Alpine.data("credentialDialog", () => createCredentialDialog(env));
  Alpine.data("deleteCredential", () => createDeleteCredential(env));
});
