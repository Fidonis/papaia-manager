// Modules (pages/modules.js): the delegation overview loads counts, revokes after a question
// and tells the outcome. Stand-ins replace fetch, the toaster and the question.
import assert from "node:assert/strict";
import { test } from "node:test";

import { delegationOverview } from "../../src/app/static/js/pages/modules.js";

const TEXTS = {
  confirm: "End every delegation of {person} at {module}?",
  revoked: "The delegations of {person} at {module} are revoked.",
  failed: "The overview could not be loaded: {detail}",
};

function setup(answers, { answer = true, guard = false } = {}) {
  const sent = [];
  const shown = [];
  const asked = [];
  const overview = delegationOverview({
    texts: TEXTS,
    coreTexts: { requestRefused: "The request was refused: {detail}" },
    actions: { authGuard: () => guard },
    fetchImpl: (url, init) => {
      sent.push([url, init]);
      const [status, body] = answers.shift();
      return Promise.resolve({ ok: status < 300, status, json: () => Promise.resolve(body) });
    },
    show: (tone, html) => shown.push([tone, html]),
    confirm: (t) => { asked.push(t); return answer; },
    doc: { querySelector: () => ({ value: "csrf-1" }) },
  });
  return { overview, sent, shown, asked };
}

const settle = () => new Promise((r) => setImmediate(r));

test("the overview loads the counts, and a failure is said in the page", async () => {
  const ok = setup([[200, { modules: [{ module: "chat" }] }]]);
  await ok.overview.load();
  assert.deepEqual(ok.overview.modules, [{ module: "chat" }]);
  const refused = setup([[503, { detail: "down" }]]);
  await refused.overview.load();
  assert.equal(refused.overview.error, "The overview could not be loaded: down");
});

test("revoke asks with the person and module, deletes and reloads", async () => {
  const { overview, sent, shown, asked } = setup([[204, null], [200, { modules: [] }]]);
  await overview.revoke({ module: "chat", title: "Chat <b>" }, { user_id: "u 1", username: "" });
  await settle();
  assert.deepEqual(asked, ["End every delegation of u 1 at Chat <b>?"]);
  assert.equal(sent[0][0], "/api/v1/delegations/chat/persons/u%201");
  assert.equal(sent[0][1].method, "DELETE");
  assert.deepEqual(sent[0][1].headers, { "X-CSRF-Token": "csrf-1" });
  assert.deepEqual(shown, [["success", "The delegations of u 1 at Chat &lt;b&gt; are revoked."]]);
  assert.equal(sent[1][0], "/api/v1/delegations");
});

test("a declined question sends nothing, a refusal is told escaped", async () => {
  const declined = setup([], { answer: false });
  await declined.overview.revoke({ module: "chat", title: "Chat" }, { user_id: "u1", username: "ann" });
  assert.deepEqual(declined.sent, []);
  const refused = setup([[403, { detail: "<no>" }], [200, { modules: [] }]]);
  await refused.overview.revoke({ module: "chat", title: "Chat" }, { user_id: "u1", username: "ann" });
  await settle();
  assert.deepEqual(refused.shown, [["error", "The request was refused: &lt;no&gt;"]]);
});
