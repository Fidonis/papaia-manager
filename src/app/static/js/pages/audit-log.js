// The Audit log page (audit.html, partials/audit_list.html): the filter choices the list hands
// over, the export links that follow the filters on screen, and the prune dialog. The dialog
// stays plain DOM, no Alpine on the <dialog> itself: an x-effect reading $refs of its own
// <dialog> once stopped x-show for the whole page. Built by createAuditPage() from what it
// talks to, so the tests can hand in a stand-in document; pages/audit.js wires it to the page.
import { escapeHtml, fmt, fmtHtml, fmtPlural } from "../core/format.js";

const EXPORT = "/api/v1/audit/export?format=";

// The filters as a query; empty fields are noise on a shared or export link.
export function filterQuery(entries, extra) {
  const params = new URLSearchParams(entries);
  Object.keys(extra || {}).forEach(function (k) { params.set(k, extra[k]); });
  Array.from(params.keys()).forEach(function (k) { if (!params.get(k)) params.delete(k); });
  return params;
}

// The day `days` days before `now`, as the date field wants it.
export function presetDate(now, days) {
  const d = new Date(now.getTime());
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

async function apiError(r) {
  try {
    const e = await r.json();
    return e.detail || JSON.stringify(e);
  } catch (_) {
    return String(r.status);
  }
}

// doc: the document; texts: the sentences of the island `audit-page`; filters: the filter values
// the page was opened with; formEntries(): the fields of the filter form; fetchImpl, csrf: the
// request and its token; toast(type, html); swap(url): reload the list; now(); lang.
export function createAuditPage({ doc, texts, filters, formEntries, fetchImpl, csrf, toast, swap, now, lang }) {
  const el = (id) => doc.getElementById(id);

  function prune(before, dryRun) {
    return fetchImpl("/api/v1/audit/prune", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify({ before: before, dry_run: dryRun }),
    });
  }

  // Filter selects have no options server-rendered into audit.html: the choices come from the
  // log itself (facets), which only the partial computes. The first list fills them in and
  // marks the value the page was opened with as selected -- so a deep link's filter is
  // visible in the form, not only applied to the list.
  function fillFacets(facets) {
    [["audit-filter-user", facets.user, "user"],
     ["audit-filter-action", facets.action, "action"],
     ["audit-filter-result", facets.result, "result"]].forEach(function (spec) {
      const select = el(spec[0]);
      const values = spec[1] || [];
      const selected = select.value || filters[spec[2]];
      if (select.options.length > 1) return; // already filled once
      values.forEach(function (value) {
        const opt = doc.createElement("option");
        opt.value = value;
        opt.textContent = value;
        if (value === selected) opt.selected = true;
        select.appendChild(opt);
      });
    });
  }

  // Kept in sync with the filter form so Export always respects what is on screen, not just
  // what was true when the page loaded.
  function syncExportLinks() {
    const qs = filterQuery(formEntries()).toString();
    el("audit-export-csv").href = EXPORT + "csv" + (qs ? "&" + qs : "");
    el("audit-export-jsonl").href = EXPORT + "jsonl" + (qs ? "&" + qs : "");
  }

  function pruneOpened() {
    el("audit-prune-date").value = "";
    el("audit-prune-preview").textContent = "";
    el("audit-prune-export-link").textContent = "";
    el("audit-prune-go").disabled = true;
  }

  async function prunePreview() {
    const date = el("audit-prune-date").value;
    const preview = el("audit-prune-preview");
    const link = el("audit-prune-export-link");
    const go = el("audit-prune-go");
    if (!date) {
      preview.textContent = "";
      link.textContent = "";
      go.disabled = true;
      return;
    }
    const r = await prune(date, true);
    if (!r.ok) {
      preview.textContent = texts.previewFailed + " " + (await apiError(r));
      go.disabled = true;
      return;
    }
    const data = await r.json();
    if (data.removed === 0) {
      preview.textContent = texts.noneOlder;
      link.textContent = "";
      go.disabled = true;
      return;
    }
    preview.textContent = fmt(texts.willDelete,
      { removed: data.removed, total: data.removed + data.kept, oldest: data.oldest_removed, newest: data.newest_removed });
    link.innerHTML = fmtHtml(texts.exportFirst, { before: encodeURIComponent(date) });
    go.disabled = false;
  }

  function prunePreset(days) {
    el("audit-prune-date").value = presetDate(now(), days);
    return prunePreview();
  }

  async function pruneConfirm() {
    const date = el("audit-prune-date").value;
    const go = el("audit-prune-go");
    go.disabled = true;
    const r = await prune(date, false);
    if (!r.ok) {
      toast("error", escapeHtml(texts.deleteFailed + " " + (await apiError(r))));
      go.disabled = false;
      return;
    }
    const data = await r.json();
    el("audit-prune-modal").close();
    toast("success", fmtPlural(data.removed, texts.deletedOne, texts.deletedOther, {}, true, lang));
    swap("/partials/audit?" + filterQuery(formEntries()).toString());
  }

  return { fillFacets, syncExportLinks, pruneOpened, prunePreview, prunePreset, pruneConfirm };
}
