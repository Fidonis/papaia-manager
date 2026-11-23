// Services (services.html), T-UI-03: the whole-stack verbs in the header, the selection of
// service groups with its bulk bar and dialog, the add-on verbs per row and the strip of the
// stack runner. They are Alpine components this module registers when Alpine starts:
//
//   stackControls   the header's "Stack" menu and its dialog (partials/_group_controls.html);
//   serviceGroups   the selection scope on #service-controls: it outlives the 15 s swap of
//                   #service-list, which hands it the current group map through
//                   setGroups() (x-init of the swapped-in partials/service_list.html);
//   serviceAddon    start, stop and restart of one add-on project (a row of the list);
//   stackRunner     "Dismiss" on the strip of a finished stack action
//                   (partials/stack_runner.html).
//
// The sentences come from the data island `services-texts`; the template's expressions call
// only what a component offers. Everything the components talk to comes in as an argument;
// the bottom of this file wires up the page.
import { stackBody } from "../core/actions.js";
import { csrfToken } from "../core/csrf.js";
import { fmtPlural } from "../core/format.js";
import { readIsland } from "../core/islands.js";
import { pageCore } from "../core/page.js";

// Profiles others depend on: Keycloak issues the tokens, nginx is the public ingress and
// LiteLLM backs LibreChat. The dialog warns before they stop.
const SHARED_PROFILES = ["keycloak", "nginx", "litellm"];

export function stackControls({ texts, actions }) {
  return {
    stackPending: "",
    stackCleanUp: false,
    get stackTitle() {
      return { start: texts.stackStart, stop: texts.stackStop, restart: texts.stackRestart }[this.stackPending] || "";
    },
    askStack(action) {
      this.stackPending = action;
      this.stackCleanUp = false;
      this.$refs.stackModal.showModal();
    },
    runStack() {
      this.$refs.stackModal.close();
      return actions.stackAction(this.stackPending, stackBody(this.stackPending, this.stackCleanUp));
    },
  };
}

export function serviceGroups({ texts, actions, lang }) {
  const plural = (count, pair) => fmtPlural(count, pair[0], pair[1], undefined, false, lang);
  return {
    sel: [],
    groups: {},
    busy: false,
    pending: "",
    cleanUp: false,
    toggle(name) {
      this.sel = this.sel.includes(name)
        ? this.sel.filter((g) => g !== name)
        : this.sel.concat([name]);
    },
    selected(name) {
      return this.sel.includes(name);
    },
    clear() {
      this.sel = [];
    },
    prune() {
      this.sel = this.sel.filter((g) => g in this.groups);
    },
    // The group map of the list just swapped in (`el` carries it as data-group-map). A group
    // that left the deployment must not stay selected and be sent on the next action.
    setGroups(el) {
      this.groups = JSON.parse(el.dataset.groupMap);
      this.prune();
    },
    get modules() {
      return this.sel.reduce((n, g) => n + (this.groups[g] || []).length, 0);
    },
    members(group) {
      return (this.groups[group] || []).join(" · ");
    },
    get touchesShared() {
      return this.sel.some((g) => SHARED_PROFILES.includes(g));
    },
    get selectionText() {
      return plural(this.sel.length, texts.groupsSelected) + " · " + plural(this.modules, texts.modules);
    },
    get confirmTitle() {
      const pair = { start: texts.startGroups, stop: texts.stopGroups, restart: texts.restartGroups }[this.pending];
      return pair ? plural(this.sel.length, pair) : "";
    },
    ask(action) {
      this.pending = action;
      this.cleanUp = false;
      this.$refs.confirmModal.showModal();
    },
    run() {
      this.$refs.confirmModal.close();
      this.busy = true;
      return actions.stackGroupAction(this.pending, this.sel, stackBody(this.pending, this.cleanUp))
        .then((ok) => {
          this.busy = false;
          if (ok) this.sel = [];
        });
    },
  };
}

export function serviceAddon({ actions }) {
  return {
    busy: false,
    verb: "",
    run(name, verb) {
      this.busy = true;
      this.verb = verb;
      return actions.serviceAddonAction(name, verb, {}).then((ok) => {
        if (!ok) {
          this.busy = false;
          this.verb = "";
        }
      });
    },
  };
}

export function stackRunner({ actions, fetchImpl, doc }) {
  return {
    dismiss() {
      return fetchImpl("/api/v1/stack/runner/clear", { method: "POST", headers: { "X-CSRF-Token": csrfToken(doc) } })
        .then((r) => {
          if (!actions.authGuard(r)) actions.refreshStackRunner();
        });
    },
  };
}

// The page: the components are registered when Alpine starts, which is after this module ran
// (base.html loads page modules before Alpine).
if (globalThis.document) {
  document.addEventListener("alpine:init", () => {
    const { actions } = pageCore();
    const texts = readIsland("services-texts");
    window.Alpine.data("stackControls", () => stackControls({ texts, actions }));
    window.Alpine.data("serviceGroups", () => serviceGroups({ texts, actions }));
    window.Alpine.data("serviceAddon", () => serviceAddon({ actions }));
    window.Alpine.data("stackRunner", () =>
      stackRunner({ actions, fetchImpl: (...args) => window.fetch(...args), doc: document }));
  });
}
