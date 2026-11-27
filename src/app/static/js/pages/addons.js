// Add-ons (addons.html) and an add-on's page (addon_detail.html), T-UI-03.
//
// Both pages load their body through htmx (partials/addon_gallery.html,
// partials/addon_detail_content.html); the controls in it are Alpine components this module
// registers when Alpine starts, so the swapped-in markup carries no script and its
// expressions only call what a component offers:
//
//   catalogRefresh  the "Refresh catalogs" button in the header of the add-ons page;
//   addonControls   the verbs of one add-on (partials/_addon_controls.html): start, stop,
//                   install with its form, remove and uninstall after their dialogs, the
//                   maintenance actions, the rollback and the configuration editor.
//
// The macros say where a verb runs (`detail` on the add-on's page, `gallery` on a gallery
// card); the component maps it to the action of core/actions.js that redraws that view and
// refuses any other value. Everything the components talk to comes in as an
// argument; the bottom of this file wires up the page.
import { describeFailure } from "../core/errors.js";
import { pageCore } from "../core/page.js";

// The values of the install form worth sending: a secret or marker field only when filled,
// any other field only when it differs from the example the add-on ships.
export function installEnv(inputs) {
  const env = {};
  inputs.forEach((el) => {
    const val = el.value.trim();
    const omit = el.dataset.omitIfEmpty === "1";
    const send = omit ? val !== "" : val !== (el.dataset.example || "");
    if (send) env[el.dataset.envKey] = val;
  });
  return env;
}

// The values of the configuration editor worth sending: a blank secret keeps its value, any
// other field is sent when it has a value or had one.
export function configEnv(inputs) {
  const env = {};
  inputs.forEach((el) => {
    const val = el.value.trim();
    const send = el.dataset.isSecret === "1" ? val !== "" : (val !== "" || el.dataset.initial !== "");
    if (send) env[el.dataset.envKey] = val;
  });
  return env;
}

// Whether an input of the configuration editor holds a change.
export function inputChanged(el) {
  if (el.dataset.isSecret === "1") return el.value.trim() !== "";
  return el.value !== (el.dataset.initial ?? "");
}

// Fetches every git catalog again, then redraws the gallery: a version a catalog published
// since the last refresh only shows up as an upgrade after this. catalogAction
// (core/actions.js) tells each outcome; a refusal in three parts with its reference.
export function refreshAllCatalogs({ actions, fetchImpl, htmx }) {
  return fetchImpl("/api/v1/catalogs")
    .then((r) => (actions.authGuard(r) || !r.ok ? [] : r.json()))
    .then((catalogs) => Promise.all(
      catalogs.filter((c) => c.type === "git" && c.enabled).map((c) => actions.catalogAction(c.name, "refresh")),
    ))
    .then(() => htmx().ajax("GET", "/partials/addons", { target: "#addon-gallery", swap: "innerHTML" }));
}

export function catalogRefresh(deps) {
  return {
    busy: false,
    refreshAll() {
      this.busy = true;
      return refreshAllCatalogs(deps).finally(() => {
        this.busy = false;
      });
    },
  };
}

export function addonControls({ actions, htmx, describe }) {
  // The views a macro may name (its `fn` argument) and the action that redraws each.
  const VERB_ACTIONS = {
    detail: actions.addonAction,
    gallery: actions.galleryAction,
  };
  function verbAction(fn) {
    if (!Object.prototype.hasOwnProperty.call(VERB_ACTIONS, fn)) throw new Error(`unknown add-on view ${fn}`);
    return VERB_ACTIONS[fn];
  }

  return {
    busy: false,
    activeVerb: "",
    // The rollback dialog: "code" or "data".
    mode: "",
    // A refused maintenance action, in three parts.
    refused: "",
    refusedTodo: "",
    refusedReference: "",
    // The configuration editor.
    editMode: false,
    dirty: false,

    // A verb without a dialog (start, stop) or after its dialog (remove, uninstall). The
    // buttons stay locked once the job runs: the swap that follows replaces them.
    run(fn, name, verb, body) {
      this.busy = true;
      this.activeVerb = verb;
      return verbAction(fn)(name, verb, body || {}).then((ok) => {
        if (!ok) {
          this.busy = false;
          this.activeVerb = "";
        }
      });
    },

    // A verb confirmed in the dialog `ref` (removeModal, uninstallModal).
    confirmRun(ref, fn, name, verb) {
      this.$refs[ref].close();
      return this.run(fn, name, verb);
    },

    // The install form (`form`): the fields worth sending and whether to start afterwards.
    install(form, fn, name, catalog) {
      const env = installEnv(form.querySelectorAll("[data-env-key]"));
      const start = form.querySelector("[name=start]").checked;
      this.$refs.installModal.close();
      return this.run(fn, name, "install", { catalog, env, start });
    },

    // A maintenance action the add-on declares. A destructive one runs only from its dialog
    // (confirmMaintain), which is what sends `confirmed`; the browser's confirm() is not used,
    // since a browser can switch it off or answer it on its own. A refusal is shown in the
    // section in three parts (`refused`, `refusedTodo`, `refusedReference`).
    maintain(name, action, confirmed) {
      this.busy = true;
      this.activeVerb = action;
      this.clearRefusal();
      return actions.addonMaintenanceAction(name, action, confirmed, (failure) => this.showRefusal(failure))
        .then((ok) => {
          if (!ok) {
            this.busy = false;
            this.activeVerb = "";
          }
        });
    },

    // The run button of the confirmation dialog `ref` of a destructive action.
    confirmMaintain(ref, name, action) {
      this.$refs[ref].close();
      return this.maintain(name, action, true);
    },

    // The panel was redrawn while an action runs (`data-running-action`): keep its buttons
    // locked until the redraw at the end of the job.
    resumeAction(el) {
      const action = el.dataset.runningAction || "";
      if (!action) return;
      this.busy = true;
      this.activeVerb = action;
    },

    clearRefusal() {
      this.refused = "";
      this.refusedTodo = "";
      this.refusedReference = "";
    },

    showRefusal(failure) {
      const parts = describe(failure);
      this.refused = parts.consequence ? parts.what + " " + parts.consequence : parts.what;
      this.refusedTodo = parts.todo;
      this.refusedReference = parts.referenceLabel + ": " + parts.correlationId;
    },

    openRollback() {
      this.mode = "";
      this.$refs.rollbackModal.showModal();
    },

    rollback(name) {
      this.$refs.rollbackModal.close();
      this.busy = true;
      return actions.addonAction(name, "rollback", { restore_data: this.mode === "data" }).then((ok) => {
        if (!ok) this.busy = false;
      });
    },

    // Every input of the editor marks it changed (`input` bubbles up to the card).
    noteInput(event) {
      const el = event.target;
      if (!el || !el.dataset || el.dataset.envKey === undefined) return;
      this.dirty = this.dirty || inputChanged(el);
    },

    cancelEdit() {
      this.$el.querySelectorAll("[data-env-key]").forEach((el) => {
        el.value = el.dataset.isSecret === "1" ? "" : (el.dataset.initial ?? "");
      });
      this.editMode = false;
      this.dirty = false;
    },

    // Saves the configuration, with or without a restart, and redraws the panel when the job
    // has ended.
    saveConfig(name, restart) {
      const env = configEnv(this.$el.querySelectorAll("[data-env-key]"));
      this.$refs.saveConfigModal.close();
      this.busy = true;
      return actions.addonPost(name, "save-config", { env, restart }).then((data) => {
        if (!data) {
          this.busy = false;
          return;
        }
        actions.pollJob(data.job_id, () =>
          htmx().ajax("GET", "/partials/addons/" + name, { target: "#addon-detail", swap: "innerHTML" }));
      });
    },
  };
}

// The page: the components are registered when Alpine starts, which is after this module ran
// (base.html loads page modules before Alpine).
if (globalThis.document) {
  document.addEventListener("alpine:init", () => {
    const { actions, core } = pageCore();
    const htmx = () => window.htmx;
    window.Alpine.data("catalogRefresh", () =>
      catalogRefresh({ actions, fetchImpl: (...args) => window.fetch(...args), htmx }));
    window.Alpine.data("addonControls", () =>
      addonControls({ actions, htmx, describe: (failure) => describeFailure(failure, core.errors) }));
  });
}
