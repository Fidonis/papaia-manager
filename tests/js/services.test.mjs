// Services (pages/services.js): the whole-stack dialog, the selection of service groups with
// its sentences, the add-on verbs per row and the stack runner's "Dismiss". Stand-ins replace
// the shared actions and the dialogs.
import assert from "node:assert/strict";
import { test } from "node:test";

import { serviceAddon, serviceGroups, stackControls, stackRunner } from "../../src/app/static/js/pages/services.js";

const TEXTS = {
  groupsSelected: ["{count} group selected", "{count} groups selected"],
  modules: ["{count} module", "{count} modules"],
  startGroups: ["Start {count} service group?", "Start {count} service groups?"],
  stopGroups: ["Stop {count} service group?", "Stop {count} service groups?"],
  restartGroups: ["Restart {count} service group?", "Restart {count} service groups?"],
  stackStart: "Start the whole stack?",
  stackStop: "Stop the whole stack?",
  stackRestart: "Restart the whole stack?",
};

function dialog() {
  return { opened: 0, closed: 0, showModal() { this.opened += 1; }, close() { this.closed += 1; } };
}

test("the stack dialog names the verb and sends clean-up only where something stops", async () => {
  const sent = [];
  const stack = stackControls({ texts: TEXTS, actions: { stackAction: (...a) => sent.push(a) } });
  stack.$refs = { stackModal: dialog() };
  assert.equal(stack.stackTitle, "");
  stack.askStack("stop");
  assert.equal(stack.stackTitle, "Stop the whole stack?");
  assert.equal(stack.$refs.stackModal.opened, 1);
  stack.stackCleanUp = true;
  stack.runStack();
  stack.askStack("start");
  assert.equal(stack.stackCleanUp, false);
  stack.runStack();
  assert.equal(stack.$refs.stackModal.closed, 2);
  assert.deepEqual(sent, [["stop", { clean_up: true }], ["start", {}]]);
});

test("the selection toggles, counts, names and is pruned against the swapped-in map", () => {
  const groups = serviceGroups({ texts: TEXTS, actions: {}, lang: "en" });
  groups.setGroups({ dataset: { groupMap: '{"rag": ["qdrant", "ingest"], "nginx": ["nginx"]}' } });
  groups.toggle("rag");
  assert.equal(groups.selected("rag"), true);
  assert.equal(groups.selectionText, "1 group selected · 2 modules");
  groups.toggle("nginx");
  assert.equal(groups.selectionText, "2 groups selected · 3 modules");
  assert.equal(groups.members("rag"), "qdrant · ingest");
  assert.equal(groups.members("gone"), "");
  assert.equal(groups.touchesShared, true);
  groups.pending = "restart";
  assert.equal(groups.confirmTitle, "Restart 2 service groups?");
  groups.pending = "";
  assert.equal(groups.confirmTitle, "");
  groups.setGroups({ dataset: { groupMap: '{"rag": ["qdrant"]}' } });
  assert.deepEqual(groups.sel, ["rag"]);
  assert.equal(groups.touchesShared, false);
  groups.toggle("rag");
  assert.deepEqual(groups.sel, []);
  groups.toggle("rag");
  groups.clear();
  assert.deepEqual(groups.sel, []);
});

test("a group action sends the selection and clears it once accepted", async () => {
  const sent = [];
  let answer = false;
  const groups = serviceGroups({
    texts: TEXTS,
    actions: { stackGroupAction: (...a) => { sent.push(a); return Promise.resolve(answer); } },
    lang: "en",
  });
  groups.$refs = { confirmModal: dialog() };
  groups.groups = { rag: ["qdrant"] };
  groups.toggle("rag");
  groups.ask("restart");
  assert.equal(groups.$refs.confirmModal.opened, 1);
  groups.cleanUp = true;
  const refused = groups.run();
  assert.equal(groups.busy, true);
  await refused;
  assert.equal(groups.busy, false);
  assert.deepEqual(groups.sel, ["rag"]);
  answer = true;
  await groups.run();
  assert.deepEqual(groups.sel, []);
  assert.deepEqual(sent[0], ["restart", ["rag"], { clean_up: true }]);
});

test("an add-on verb of a row locks the row until refused", async () => {
  let answer = false;
  const sent = [];
  const row = serviceAddon({ actions: { serviceAddonAction: (...a) => { sent.push(a); return Promise.resolve(answer); } } });
  await row.run("wiki", "restart");
  assert.deepEqual(sent, [["wiki", "restart", {}]]);
  assert.equal(row.busy, false);
  answer = true;
  await row.run("wiki", "stop");
  assert.equal(row.busy, true);
  assert.equal(row.verb, "stop");
});

test("dismiss clears the runner with the CSRF token and redraws the strip", async () => {
  const requests = [];
  let refreshed = 0;
  let guarded = false;
  const doc = { querySelector: (sel) => (sel === "input[name=pk_csrf]" ? { value: "csrf-1" } : null) };
  const runner = stackRunner({
    actions: { authGuard: () => guarded, refreshStackRunner: () => { refreshed += 1; } },
    fetchImpl: (url, init) => { requests.push([url, init]); return Promise.resolve({ ok: true }); },
    doc,
  });
  await runner.dismiss();
  assert.equal(requests[0][0], "/api/v1/stack/runner/clear");
  assert.equal(requests[0][1].method, "POST");
  assert.deepEqual(requests[0][1].headers, { "X-CSRF-Token": "csrf-1" });
  assert.equal(refreshed, 1);
  guarded = true;
  await runner.dismiss();
  assert.equal(refreshed, 1);
});
