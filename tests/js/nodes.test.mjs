// Nodes (pages/nodes.js) against a stand-in document: "Add a node" and the buttons of a node's
// page (pin a host key, send the bundle, remove the node) in place of the former on* attributes.
import assert from "node:assert/strict";
import { test } from "node:test";

import { installNodesPage } from "../../src/app/static/js/pages/nodes.js";

const TEXTS = { addFailed: "Adding the node failed:", failed: "That did not work:", removeQuestion: "Remove this node?" };

function element(props = {}) {
  const listeners = {};
  return {
    dataset: {},
    addEventListener(name, l) { (listeners[name] ||= []).push(l); },
    fire(name, event = {}) { return Promise.all((listeners[name] || []).map((l) => l(event))); },
    ...props,
  };
}

function setup({ form = null, buttons = [], status = 200, body = {}, answer = true, name = "edge" } = {}) {
  const sent = [];
  const out = { alerts: [], asked: [], went: [], reloads: 0 };
  const doc = {
    getElementById: (id) => (id === "add-node" ? form : null),
    querySelector: () => ({ value: "csrf-1" }),
    querySelectorAll: () => buttons,
  };
  installNodesPage(doc, {
    init: { name, texts: TEXTS },
    fetchImpl: (url, init) => {
      sent.push([url, init.method, init.body === undefined ? undefined : JSON.parse(init.body), init.headers["X-CSRF-Token"]]);
      return Promise.resolve({ ok: status < 300, status, json: () => Promise.resolve(body) });
    },
    alert: (t) => out.alerts.push(t),
    confirm: (t) => { out.asked.push(t); return answer; },
    navigate: (url) => out.went.push(url),
    reload: () => { out.reloads += 1; },
  });
  return { sent, out };
}

const field = (value) => ({ value });

test("a node is added and its page opened", async () => {
  const form = element({ elements: { name: field("gpu"), address: field("10.0.3.10"), ssh_user: field(""), ssh_port: field("") } });
  const { sent, out } = setup({ form, status: 201, body: { name: "gpu" }, name: "" });
  let prevented = false;
  await form.fire("submit", { preventDefault: () => { prevented = true; }, target: form });
  assert.ok(prevented);
  assert.deepEqual(sent, [["/api/v1/nodes", "POST", { name: "gpu", address: "10.0.3.10", ssh_user: "", ssh_port: 0 }, "csrf-1"]]);
  assert.deepEqual(out.went, ["/nodes/gpu"]);
});

test("a refused node says why and stays", async () => {
  const form = element({ elements: { name: field("gpu"), address: field("x"), ssh_user: field("root"), ssh_port: field("2222") } });
  const { sent, out } = setup({ form, status: 422, body: { detail: "bad address" }, name: "" });
  await form.fire("submit", { preventDefault() {}, target: form });
  assert.equal(sent[0][2].ssh_port, 2222);
  assert.deepEqual(out.alerts, ["Adding the node failed: bad address"]);
  assert.deepEqual(out.went, []);
});

test("the buttons of a node's page pin, send and remove", async () => {
  const pin = element({ dataset: { nodeAction: "pinKey", fingerprint: "SHA256:abc" } });
  const bundle = element({ dataset: { nodeAction: "sendBundle" } });
  const remove = element({ dataset: { nodeAction: "removeNode" } });
  const { sent, out } = setup({ buttons: [pin, bundle, remove], body: { notes: ["Remove the key by hand."] } });
  await pin.fire("click");
  await bundle.fire("click");
  await remove.fire("click");
  assert.deepEqual(sent.map(([u, m, b]) => [u, m, b]), [
    ["/api/v1/nodes/edge/pin", "POST", { fingerprint: "SHA256:abc" }],
    ["/api/v1/nodes/edge/bundle", "POST", undefined],
    ["/api/v1/nodes/edge", "DELETE", undefined],
  ]);
  assert.equal(out.reloads, 2);
  assert.deepEqual(out.asked, ["Remove this node?"]);
  assert.deepEqual(out.alerts, ["Remove the key by hand."]);
  assert.deepEqual(out.went, ["/nodes"]);
});

test("remove asks first, and a refusal is told without a reload", async () => {
  const remove = element({ dataset: { nodeAction: "removeNode" } });
  const declined = setup({ buttons: [remove], answer: false });
  await remove.fire("click");
  assert.deepEqual(declined.sent, []);
  const bundle = element({ dataset: { nodeAction: "sendBundle" } });
  const refused = setup({ buttons: [bundle], status: 409, body: { detail: "busy" } });
  await bundle.fire("click");
  assert.deepEqual(refused.out.alerts, ["That did not work: busy"]);
  assert.equal(refused.out.reloads, 0);
});

test("an unknown action name is refused", () => {
  assert.throws(() => setup({ buttons: [element({ dataset: { nodeAction: "eval" } })] }), /unknown node action/);
});
