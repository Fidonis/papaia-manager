// The Audit log page (pages/audit-log.js) on a stand-in document: the filter choices the list
// hands over, the export links that follow the filters, and the prune dialog's preview and
// delete.
import assert from "node:assert/strict";
import { test } from "node:test";

import { createAuditPage, filterQuery, presetDate } from "../../src/app/static/js/pages/audit-log.js";

const TEXTS = {
  previewFailed: "Could not preview:",
  noneOlder: "No entries are older than this date.",
  willDelete: "{removed} of {total} entries (oldest {oldest}, newest {newest}) will be permanently deleted.",
  exportFirst: '<a class="link" href="/api/v1/audit/export?format=csv&before={before}">Export them first</a>',
  deleteFailed: "Delete failed:",
  deletedOne: "{count} entry deleted.",
  deletedOther: "{count} entries deleted.",
};

function element(id, extra = {}) {
  return { id, value: "", textContent: "", innerHTML: "", disabled: false, href: "", ...extra };
}

function select(id, value = "") {
  const options = [{ value: "" }];
  return element(id, {
    value,
    options,
    appendChild(option) {
      options.push(option);
    },
  });
}

function setup(answer = async () => ({ ok: true, status: 200, json: async () => ({}) })) {
  const elements = {};
  for (const id of ["audit-filter-user", "audit-filter-action", "audit-filter-result"]) {
    elements[id] = select(id);
  }
  for (const id of ["audit-export-csv", "audit-export-jsonl", "audit-prune-date",
    "audit-prune-preview", "audit-prune-export-link", "audit-prune-go"]) {
    elements[id] = element(id);
  }  // prettier-ignore
  let closed = 0;
  elements["audit-prune-modal"] = element("audit-prune-modal", { close: () => (closed += 1) });
  const doc = {
    getElementById: (id) => elements[id],
    createElement: () => ({ value: "", textContent: "", selected: false }),
  };
  const sent = [];
  const shown = [];
  const swaps = [];
  let fields = [];
  const page = createAuditPage({
    doc,
    texts: TEXTS,
    filters: { user: "bob", action: "", result: "" },
    formEntries: () => fields,
    fetchImpl: async (url, options) => {
      sent.push({ url, options, body: JSON.parse(options.body) });
      return answer(url, options);
    },
    csrf: () => "csrf-of-the-session",
    toast: (type, html) => shown.push([type, html]),
    swap: (url) => swaps.push(url),
    now: () => new Date("2026-11-23T12:00:00Z"),
    lang: "en",
  });
  return {
    page, elements, sent, shown, swaps,
    setFields: (value) => { fields = value; },
    closed: () => closed,
  };  // prettier-ignore
}

const ok = (body) => async () => ({ ok: true, status: 200, json: async () => body });

test("empty filter fields stay out of a query", () => {
  const params = filterQuery([["user", "bob"], ["action", ""], ["target", "n8n"]], { offset: "50" });
  assert.equal(params.toString(), "user=bob&target=n8n&offset=50");
});

test("a preset is a date that many days back, in UTC", () => {
  assert.equal(presetDate(new Date("2026-11-23T00:30:00Z"), 30), "2026-10-24");
});

test("the filter choices are filled once, with the value of the link selected", () => {
  const { page, elements } = setup();
  page.fillFacets({ user: ["alice", "bob"], action: ["install"], result: [] });
  const users = elements["audit-filter-user"].options;
  assert.deepEqual(users.map((o) => o.value), ["", "alice", "bob"]);
  assert.deepEqual(users.map((o) => o.textContent), [undefined, "alice", "bob"]);
  assert.equal(users[2].selected, true);
  assert.equal(users[1].selected, false);
  page.fillFacets({ user: ["carla"], action: [], result: [] });
  assert.equal(elements["audit-filter-user"].options.length, 3);
});

test("the export links follow the filters on screen", () => {
  const { page, elements, setFields } = setup();
  page.syncExportLinks();
  assert.equal(elements["audit-export-csv"].href, "/api/v1/audit/export?format=csv");
  setFields([["user", "bob"], ["since", "2026-01-01"], ["target", ""]]);
  page.syncExportLinks();
  assert.equal(elements["audit-export-csv"].href, "/api/v1/audit/export?format=csv&user=bob&since=2026-01-01");
  assert.equal(elements["audit-export-jsonl"].href, "/api/v1/audit/export?format=jsonl&user=bob&since=2026-01-01");
});

test("a preview tells how many entries go and offers the export first", async () => {
  const { page, elements, sent } = setup(ok({ removed: 3, kept: 7, oldest_removed: "a", newest_removed: "b" }));
  await page.prunePreset(30);
  assert.equal(elements["audit-prune-date"].value, "2026-10-24");
  assert.deepEqual(sent[0].body, { before: "2026-10-24", dry_run: true });
  assert.equal(sent[0].options.headers["X-CSRF-Token"], "csrf-of-the-session");
  assert.equal(elements["audit-prune-preview"].textContent,
    "3 of 10 entries (oldest a, newest b) will be permanently deleted.");
  assert.equal(elements["audit-prune-export-link"].innerHTML,
    '<a class="link" href="/api/v1/audit/export?format=csv&before=2026-10-24">Export them first</a>');
  assert.equal(elements["audit-prune-go"].disabled, false);
});

test("nothing older keeps Delete off", async () => {
  const { page, elements } = setup(ok({ removed: 0, kept: 7 }));
  elements["audit-prune-date"].value = "2020-01-01";
  await page.prunePreview();
  assert.equal(elements["audit-prune-preview"].textContent, "No entries are older than this date.");
  assert.equal(elements["audit-prune-go"].disabled, true);
});

test("a refused preview says why", async () => {
  const { page, elements } = setup(async () => ({ ok: false, status: 422, json: async () => ({ detail: "bad date" }) }));
  elements["audit-prune-date"].value = "2020-01-01";
  await page.prunePreview();
  assert.equal(elements["audit-prune-preview"].textContent, "Could not preview: bad date");
  assert.equal(elements["audit-prune-go"].disabled, true);
});

test("opening the dialog clears it", () => {
  const { page, elements } = setup();
  elements["audit-prune-date"].value = "2020-01-01";
  elements["audit-prune-preview"].textContent = "old";
  elements["audit-prune-go"].disabled = false;
  page.pruneOpened();
  assert.equal(elements["audit-prune-date"].value, "");
  assert.equal(elements["audit-prune-preview"].textContent, "");
  assert.equal(elements["audit-prune-go"].disabled, true);
});

test("a delete closes the dialog, tells the count and reloads the filtered list", async () => {
  const { page, elements, sent, shown, swaps, setFields, closed } = setup(ok({ removed: 1 }));
  setFields([["user", "bob"]]);
  elements["audit-prune-date"].value = "2020-01-01";
  await page.pruneConfirm();
  assert.deepEqual(sent[0].body, { before: "2020-01-01", dry_run: false });
  assert.equal(closed(), 1);
  assert.deepEqual(shown, [["success", "1 entry deleted."]]);
  assert.deepEqual(swaps, ["/partials/audit?user=bob"]);
});

test("a refused delete keeps the dialog and offers Delete again", async () => {
  const { page, elements, shown, closed } = setup(async () => ({ ok: false, status: 500, json: async () => ({ detail: "<disk> full" }) }));
  elements["audit-prune-date"].value = "2020-01-01";
  await page.pruneConfirm();
  assert.equal(closed(), 0);
  assert.deepEqual(shown, [["error", "Delete failed: &lt;disk&gt; full"]]);
  assert.equal(elements["audit-prune-go"].disabled, false);
});
