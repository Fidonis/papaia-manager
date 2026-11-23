// The page module of the ingest lists: Jobs, one job, Runs, one run, Leftovers and the job file
// (templates ingest_*.html, loaded in base.html's block `page_modules`, before Alpine starts).
//
// The page tells which one it is, and hands its values and sentences over, in the data island
// `ingest-page` (partials/_ingest_texts.html). Buttons of the page and of the partials htmx
// swaps into it name their verb in `data-ingest-action`, with their values in data-* attributes;
// one listener on the document runs them, so a swapped row needs nothing run for it. Dialogs
// and forms are Alpine components registered here; a row opens a dialog with `$dispatch` and
// what the dialog needs to know.
import { ragDetail } from "../core/actions.js";
import { readIsland } from "../core/islands.js";
import { actions, toaster } from "../core/runtime.js";
import {
  createCatalogDefaults,
  createDeleteJobDialog,
  createDeleteOrphan,
  createDeleteRunsDialog,
  createRawEditor,
  createRunDialog,
  createVerbs,
  rowMatches,
  showWaitingToast,
} from "./ingest-components.js";

const island = readIsland("ingest-page");
const page = island.page;
const values = island.values || {};

// Where the list of this page is loaded into, and from where.
const LISTS = {
  jobs: { target: "#ingest-list", url: "/partials/ingest/jobs" },
  runs: { target: "#ingest-runs", url: "/partials/ingest/runs" },
  run: { target: "#run-body", url: "/partials/ingest/runs/" + encodeURIComponent(values.run_id || "") },
  orphans: { target: "#orphan-list", url: "/partials/ingest/orphans" },
  job: { target: "#job-tab" },
};
const TABS = ["overview", "runs", "files", "config"];

// The dropdown menus of a row stay open while the focus rests in them; close on a choice.
function closeMenu() {
  const active = document.activeElement;
  if (active && active.blur) active.blur();
}

function sessionStore() {
  try {
    return window.sessionStorage;
  } catch (_) {
    return null;
  }
}

function reloadList() {
  const list = LISTS[page];
  if (page === "runs") {
    const form = document.getElementById("runs-filter");
    const query = Object.fromEntries(new FormData(form));
    window.htmx.ajax("GET", list.url, { target: list.target, swap: "innerHTML", values: query });
  } else if (page === "job") {
    window.location.reload();
  } else if (list) {
    window.htmx.ajax("GET", list.url, { target: list.target, swap: "innerHTML" });
  } else {
    window.location.reload();
  }
}

const env = {
  texts: island.texts,
  lang: document.documentElement.lang || undefined,
  request: actions.ragRequest,
  toast: (level, html) => toaster.show(level, html),
  navigate: (url) => {
    window.location.href = url;
  },
  store: {
    setItem(key, value) {
      const storage = sessionStore();
      if (!storage) throw new Error("no session storage");
      storage.setItem(key, value);
    },
  },
  today: () => new Date(),
  closeMenu,
  afterRunsDeleted: reloadList,
  afterOrphanRemoved: reloadList,
};
const verbs = createVerbs(env);

// -- one job: its tabs --------------------------------------------------------------------------

function jobTab(name, query) {
  if (TABS.indexOf(name) === -1) name = "overview";
  for (const tab of document.querySelectorAll("#job-tabs [data-tab]")) {
    tab.classList.toggle("tab-active", tab.dataset.tab === name);
    tab.setAttribute("aria-selected", tab.dataset.tab === name ? "true" : "false");
  }
  history.replaceState(null, "", "#" + name);
  window.htmx.ajax("GET", "/partials/ingest/jobs/" + encodeURIComponent(values.job) + "/" + name + (query || ""),
    { target: "#job-tab", swap: "innerHTML" });
}

// -- the job list: search and chips ---------------------------------------------------------------

const filter = { q: "", group: "all" };

function filterJobRows() {
  let shown = 0;
  for (const row of document.querySelectorAll("#ingest-list tr[data-group]")) {
    const hit = rowMatches({ search: row.dataset.search, group: row.dataset.group }, filter);
    row.style.display = hit ? "" : "none";
    if (hit) shown += 1;
  }
  const none = document.getElementById("ingest-none");
  if (none) none.hidden = shown !== 0;
  for (const chip of document.querySelectorAll("#ingest-list [data-chip]")) {
    chip.classList.toggle("btn-active", chip.dataset.chip === filter.group);
  }
}

// -- the verbs of the buttons ---------------------------------------------------------------------

// What follows a verb: the page's own refresh, or the overview tab of a job.
function afterVerb(element) {
  if (element.dataset.ingestAfter === "overview") return () => jobTab("overview");
  return reloadList;
}

function openDeleteRuns() {
  window.dispatchEvent(new CustomEvent("ingest-delete-runs", { detail: {
    job_id: document.getElementById("runs-job").value,
    jobs: values.job_choices || [],
    choose: true,
    tz: values.tz || "",
  } }));
}

const VERBS = {
  refresh: () => reloadList(),
  "reload-catalog": () => verbs.reloadCatalog(reloadList),
  enable: (el) => verbs.toggle(el.dataset.jobId, true, afterVerb(el)),
  disable: (el) => verbs.toggle(el.dataset.jobId, false, afterVerb(el)),
  resume: (el) => verbs.resume(el.dataset.jobId, afterVerb(el)),
  abort: (el) => verbs.abort(el.dataset.runId, reloadList),
  "delete-runs": () => openDeleteRuns(),
  tab: (el) => jobTab(el.dataset.tabName, el.dataset.tabQuery || ""),
  "files-status": (el) => {
    const select = document.getElementById("files-status");
    if (select) select.value = el.dataset.filesStatus;
  },
  chip: (el) => {
    filter.group = el.dataset.chip;
    filterJobRows();
  },
};

document.addEventListener("click", (event) => {
  const element = event.target.closest ? event.target.closest("[data-ingest-action]") : null;
  if (element) {
    const verb = VERBS[element.dataset.ingestAction];
    if (verb) {
      if (element.tagName === "A" && element.getAttribute("href") === "#files") event.preventDefault();
      closeMenu();
      verb(element);
      return;
    }
  }
  // A link of a row menu that navigates closes the menu too.
  if (event.target.closest && event.target.closest(".dropdown-content a[href]")) closeMenu();
});

// -- what htmx swaps ------------------------------------------------------------------------------

document.addEventListener("input", (event) => {
  if (event.target.id === "ingest-search") {
    filter.q = event.target.value.toLowerCase();
    filterJobRows();
  }
});

document.addEventListener("htmx:afterSettle", (event) => {
  if (page !== "jobs") return;
  const detail = event.detail || {};
  if ((detail.target && detail.target.id === "ingest-list") || (detail.elt && detail.elt.id === "ingest-jobs")) {
    filterJobRows();
  }
});

// While a run works the list replaces itself every few seconds. That would take an open row menu
// away from somebody who is reading it (the swap removes the focus that holds the menu open), so
// a poll waits until the menu is closed; the next one picks the change up.
document.addEventListener("htmx:beforeSwap", (event) => {
  const target = event.detail && event.detail.target;
  const active = document.activeElement;
  if (target && target.id === "ingest-jobs" && active && active.closest &&
      active.closest(".dropdown") && target.contains(active)) {
    event.detail.shouldSwap = false;
  }
});

// htmx does not swap a 4xx answer, so a failed load would leave the spinner up. The panel is the
// page's <template id="ingest-failure">; the reason goes in as text.
document.addEventListener("htmx:responseError", (event) => {
  const detail = event.detail || {};
  const list = LISTS[page];
  if (!list || !detail.target || "#" + detail.target.id !== list.target) return;
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

// The filter forms answer through htmx as they change; Enter in a field must not send them.
document.addEventListener("submit", (event) => {
  if (event.target.hasAttribute && event.target.hasAttribute("data-ingest-no-submit")) event.preventDefault();
});

// -- the runs filter ------------------------------------------------------------------------------

// Keep the filter in the address, so a link to a job's runs and the back button do the obvious thing.
document.addEventListener("change", (event) => {
  const form = event.target.form;
  if (page !== "runs" || !form || form.id !== "runs-filter") return;
  const params = new URLSearchParams(new FormData(form));
  for (const key of Array.from(params.keys())) if (!params.get(key)) params.delete(key);
  history.replaceState(null, "", "/ingest/runs" + (params.toString() ? "?" + params.toString() : ""));
});

// -- the components -------------------------------------------------------------------------------

document.addEventListener("alpine:init", () => {
  const Alpine = window.Alpine;
  Alpine.data("runDialog", () => createRunDialog(env));
  Alpine.data("deleteJobDialog", () => createDeleteJobDialog(env));
  Alpine.data("deleteRunsDialog", () => createDeleteRunsDialog(env));
  Alpine.data("deleteOrphan", () => createDeleteOrphan(env));
  Alpine.data("catalogDefaults", () => createCatalogDefaults(env, values.defaults || {}));
  Alpine.data("rawEditor", () => createRawEditor(env, values.raw || {}));
});

// htmx is a deferred script after this one, so the first tab of a job starts once the page is
// parsed and every deferred script has run.
document.addEventListener("DOMContentLoaded", () => {
  const storage = sessionStore();
  if (storage) showWaitingToast(storage, env.toast);
  if (page === "job") {
    const params = new URLSearchParams(window.location.search);
    const run = params.get("run");
    const hash = window.location.hash.replace("#", "");
    jobTab(hash || "overview", hash === "files" && run ? "?run=" + encodeURIComponent(run) : "");
  }
});
