// Audit log (audit.html and the list it swaps in, partials/audit_list.html). The page carries
// no inline script and no event attribute (T-UI-03): the filters the page was opened with and
// its sentences come from the data island `audit-page`, the filter choices from the island
// `audit-facets` of each list, and the buttons of the prune dialog name what they do in
// data-* attributes, which the listeners below dispatch.
import { csrfToken } from "../core/csrf.js";
import { readIsland } from "../core/islands.js";
import { toaster } from "../core/runtime.js";
import { createAuditPage } from "./audit-log.js";

const island = readIsland("audit-page");
const form = document.getElementById("audit-filters");
const modal = document.getElementById("audit-prune-modal");

const page = createAuditPage({
  doc: document,
  texts: island.texts,
  filters: island.filters,
  formEntries: () => new FormData(form),
  fetchImpl: (...args) => fetch(...args),
  csrf: () => csrfToken(),
  toast: (type, html) => toaster.show(type, html),
  swap: (url) => window.htmx.ajax("GET", url, { target: "#audit-list", swap: "innerHTML" }),
  now: () => new Date(),
  lang: document.documentElement.lang || undefined,
});

// Every list brings the filter choices of the log; the first one fills the selects.
document.body.addEventListener("htmx:afterSwap", (event) => {
  const target = event.detail && event.detail.target;
  if (!target || target.id !== "audit-list") return;
  if (document.getElementById("audit-facets")) page.fillFacets(readIsland("audit-facets"));
});

// Filtering goes through htmx; the form itself never submits.
form.addEventListener("submit", (event) => event.preventDefault());
form.addEventListener("change", page.syncExportLinks);
page.syncExportLinks();

document.querySelector("[data-audit-reset]").addEventListener("click", () => {
  window.location.assign("/audit");
});

document.getElementById("audit-prune-open").addEventListener("click", () => {
  modal.showModal();
  page.pruneOpened();
});
document.getElementById("audit-prune-date").addEventListener("input", page.prunePreview);
modal.addEventListener("click", (event) => {
  const preset = event.target.closest("[data-audit-prune-days]");
  if (preset) page.prunePreset(Number(preset.dataset.auditPruneDays));
  if (event.target.closest("[data-audit-prune-cancel]")) modal.close();
});
document.getElementById("audit-prune-go").addEventListener("click", page.pruneConfirm);
