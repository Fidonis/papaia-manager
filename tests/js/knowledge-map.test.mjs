// The knowledge map's component (pages/knowledge-map/component.js) without a browser: the
// requests it sends, and the methods that took over what the CSP build of Alpine does not
// evaluate in the template (statements, arrow functions, Number, Math, Object, Boolean).
import assert from "node:assert/strict";
import { test } from "node:test";

import { createKnowledgeMap } from "../../src/app/static/js/pages/knowledge-map/component.js";

const T = {
  kind_source: "Source",
  kind_mcp: "Server",
  count_source: ["{n} source", "{n} sources"],
  count_mcp: ["{n} server", "{n} servers"],
  all_roles: "all roles",
  not_set: "not set",
  error: "Something went wrong: {detail}",
  unreachable: "The map server can't be asked: {detail}",
  saved: "Saved.",
  job_running: "Rendering …",
  subjects: { employees: "employees", customers: "customers" },
  tiers: { local: "local" },
  parts: { all: "all", development: "development", holdout: "held out" },
  quality: {
    name: "has a name",
    description: "the description says what is in it",
    hand_over: "says when to hand over",
    address: "has an address",
    topic: "has at least one topic",
    examples: "has three example questions or more",
    place: "names the place",
    access: "has an access path",
    probe: "the probe finds it for every example question",
  },
};

const doc = {
  getElementById: () => null,
  documentElement: { lang: "en" },
  querySelector(selector) {
    return selector === "input[name=pk_csrf]" ? { value: "csrf-of-the-session" } : null;
  },
};

function reply(status, body) {
  return { ok: status >= 200 && status < 300, status, statusText: "status", json: async () => body };
}

function setup(answer = () => reply(200, {})) {
  const sent = [];
  const map = createKnowledgeMap(T, {
    doc,
    fetchImpl: async (url, options) => {
      sent.push({ url, options });
      return answer(url, options);
    },
  });
  map.$nextTick = (fn) => fn();
  return { map, sent };
}

test("a request carries the CSRF token of the session and a JSON body", async () => {
  const { map, sent } = setup(() => reply(200, { abstention: true }));
  map.sim.need = "open invoices";
  await map.simulate();
  assert.equal(sent.length, 1);
  assert.equal(sent[0].url, "/api/v1/knowledge-map/simulate");
  assert.equal(sent[0].options.method, "POST");
  assert.equal(sent[0].options.headers["X-CSRF-Token"], "csrf-of-the-session");
  assert.deepEqual(JSON.parse(sent[0].options.body), { need: "open invoices", roles: null });
  assert.deepEqual(map.sim.result, { abstention: true });
  assert.equal(map.sim.busy, false);
});

test("a refusal shows the server's reason", async () => {
  const { map } = setup(() => reply(503, { detail: "map server down" }));
  await map.simulate();
  assert.equal(map.message, "The map server can't be asked: map server down");
  assert.equal(map.messageClass, "alert-error");
});

test("a tab that loads on opening shows and loads in one call", async () => {
  const { map, sent } = setup((url) =>
    reply(200, url.endsWith("/eval/history") ? { runs: [{ hit_at_1: 1 }] } : { suggestions: [] }),
  );
  map.showUsage();
  assert.equal(map.tab, "usage");
  map.showSuggestions();
  assert.equal(map.tab, "suggestions");
  map.showGovernance();
  assert.equal(map.tab, "governance");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(
    sent.map((s) => s.url),
    [
      "/api/v1/knowledge-map/stats?days=14",
      "/api/v1/knowledge-map/suggestions?days=28",
      "/api/v1/knowledge-map/governance",
      "/api/v1/knowledge-map/eval/history",
    ],
  );
});

test("a row of the entries opens it on the map", () => {
  const { map } = setup();
  const entry = { id: "wiki.main", kind: "source" };
  map.tab = "entries";
  map.showEntry(entry);
  assert.equal(map.selected, entry);
  assert.equal(map.tab, "map");
});

test("the values the template used to compute itself", () => {
  const { map } = setup();
  assert.equal(map.confidenceText({ confidence: 0.8 }), "0.80");
  assert.equal(map.confidenceText({ confidence: "0.456" }), "0.46");
  assert.equal(map.pct(0.25), "25 %");
  assert.equal(map.pct(0), "0 %");
  assert.equal(map.subjectsText({ data_subjects: ["employees", "customers", "robots"] }),
    "employees, customers, robots");
  assert.equal(map.subjectsText({ data_subjects: [] }), "");
  assert.equal(map.hasProposal({ open_proposal: {} }), false);
  assert.equal(map.hasProposal({ open_proposal: { classification: "internal" } }), true);
  assert.equal(map.exceptionNote({ reason: "pilot", accepted_by_role: null, risk_ref: "R-7" }),
    "pilot · R-7");
  assert.equal(map.exceptionNote({ reason: "", accepted_by_role: null, risk_ref: null }), "");
});

test("the gold-set history shows the last eight runs, newest first", () => {
  const { map } = setup();
  map.history = Array.from({ length: 10 }, (_, i) => ({ at: String(i) }));
  assert.deepEqual(map.recentRuns().map((r) => r.at), ["9", "8", "7", "6", "5", "4", "3", "2"]);
  assert.equal(map.history.length, 10, "the history itself is left as it is");
});

test("discarding a draft brings back the saved file", () => {
  const { map } = setup();
  map.file = { key: "sources", content: "sources: []\n" };
  map.draft = "sources: [broken";
  map.problems = ["line 1: broken"];
  map.discardDraft();
  assert.equal(map.draft, "sources: []\n");
  assert.deepEqual(map.problems, []);
});

test("confirming a suggested tier saves exactly that tier", async () => {
  const { map, sent } = setup(() => reply(202, { job_id: "j-1" }));
  map.follow = async () => true;
  map.gov.data = { revision: "r-1" };
  const model = { name: "eu/large", tier: "", suggested_tier: "local", choice: "" };
  await map.confirmTier(model);
  assert.equal(model.choice, "local");
  assert.equal(sent[0].url, "/api/v1/knowledge-map/governance/models/eu/large");
  assert.equal(sent[0].options.method, "PUT");
  assert.deepEqual(JSON.parse(sent[0].options.body), { revision: "r-1", tier: "local" });
});

test("the texts the server used to write into the script come from the island", () => {
  const { map } = setup();
  assert.equal(map.partLabel("holdout"), "held out");
  map.ed = { f: { ...map.blankForm(), system: "Calendar" }, probe: null };
  assert.deepEqual(map.qualityChecks().map((c) => [c.label, c.ok]), [
    ["has a name", true],
    ["the description says what is in it", false],
    ["has at least one topic", false],
    ["has three example questions or more", false],
    ["has an access path", false],
    ["the probe finds it for every example question", false],
  ]);
  map.ed.f = { ...map.blankForm(), kind: "agent", delegate_when: "x", access: [{ via: "a2a", target: "https://a.test" }] };
  assert.deepEqual(map.qualityChecks().map((c) => c.label),
    ["has a name", "the description says what is in it", "says when to hand over", "has an address"]);
  map.ed.f = { ...map.blankForm(), kind: "reference", location: "binder" };
  assert.ok(map.qualityChecks().some((c) => c.label === "names the place" && c.ok));
});

test("the summary counts the entries per kind in singular and plural", () => {
  const { map } = setup();
  map.entries = [{ kind: "source" }, { kind: "source" }, { kind: "mcp" }, { kind: "agent" }];
  assert.equal(map.kindSummary(), "2 sources · 1 server · 1 agent");
});
