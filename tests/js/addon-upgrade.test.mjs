// The add-on upgrade (pages/addon-upgrade/component.js) without a browser: the plan request,
// the getters that replace what the CSP build of Alpine does not evaluate in the template, the
// upgrade request with only the entered values, and following the job to its end.
import assert from "node:assert/strict";
import { test } from "node:test";

import { createAddonUpgrade } from "../../src/app/static/js/pages/addon-upgrade/component.js";

const doc = {
  querySelector: (selector) => (selector === "input[name=pk_csrf]" ? { value: "csrf-of-the-session" } : null),
};

const PLAN = {
  revision: "rev-1",
  form: [{ key: "NEW_MAIL", label: "Mail" }],
  changelog: [],
  plan: {
    current: { version: "1.0.0" },
    candidate: { version: "1.1.0" },
    direction: "upgrade",
    blocked: false,
    in_place: false,
    needs_input: ["NEW_MAIL"],
    compat: { warnings: [], reason: "" },
    components: { added: [], removed: [], changed: [] },
    env: { new: [], removed: [], default_changed: [] },
    manifest: {
      addon_api: { from: [1, 2], to: [1, 2] },
      attach: { added: [], removed: [] },
      keycloak: { clients_added: [], mappers_added: {} },
      replace_secrets_added: [],
    },
  },
};

function reply(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function setup(answer, { supported = true } = {}) {
  const sent = [];
  const timers = [];
  const component = createAddonUpgrade(
    { name: "paper less", supported, texts: { upgradeTo: "Upgrade to {version}" } },
    {
      doc,
      fetchImpl: async (url, options) => {
        sent.push({ url, options });
        return answer(url, options);
      },
      authGuard: (r) => r.status === 401,
      clock: { setTimeout: (fn) => timers.push(fn) },
    },
  );
  return { component, sent, timers };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

test("load asks for the plan of the named add-on and fills the getters", async () => {
  const { component, sent } = setup(() => reply(200, PLAN));
  assert.equal(component.loading, true);
  assert.equal(component.applyLabel, "");
  await component.load();
  assert.equal(sent[0].url, "/api/v1/addons/paper%20less/upgrade-plan");
  assert.equal(sent[0].options.headers["X-CSRF-Token"], "csrf-of-the-session");
  assert.equal(component.loading, false);
  assert.equal(component.applyLabel, "Upgrade to 1.1.0");
  assert.deepEqual(component.missing, ["NEW_MAIL"]);
  assert.equal(component.canApply, false);
  assert.equal(component.apiChanged, false);
  assert.ok(!component.hasManifestChanges);
  component.values.NEW_MAIL = "  ops@example.org ";
  assert.equal(component.canApply, true);
  assert.equal(component.show(null), "–");
  assert.equal(component.show(["a", "b"]), "a, b");
});

test("a core without add-on upgrades asks nothing", async () => {
  const { component, sent } = setup(() => reply(200, PLAN), { supported: false });
  await component.load();
  assert.equal(component.loading, false);
  assert.deepEqual(sent, []);
});

test("a refused plan names the field and the message", async () => {
  const { component } = setup(() => reply(422, { detail: { field: "revision", message: "changed" } }));
  await component.load();
  assert.equal(component.error, "revision: changed");
});

test("apply sends the entered values and follows the job to its end", async () => {
  const answers = {
    "/api/v1/addons/paper%20less/upgrade-plan": () => reply(200, PLAN),
    "/api/v1/addons/paper%20less/upgrade": () => reply(202, { job_id: "j-1" }),
    "/api/v1/jobs/j-1": () => reply(200, { status: "running" }),
    "/api/v1/jobs/j-1/log": () => reply(200, { log: "pulling" }),
  };
  const { component, sent, timers } = setup((url) => answers[url]());
  await component.load();
  component.values = { NEW_MAIL: " ops@example.org ", EMPTY: "  " };
  component.backup = false;
  await component.apply();
  const body = JSON.parse(sent[1].options.body);
  assert.deepEqual(body, { env: { NEW_MAIL: "ops@example.org" }, revision: "rev-1", backup: false });
  assert.equal(component.applied, true);
  await settle();
  assert.equal(component.jobStatus, "running");
  assert.equal(component.log, "pulling");
  assert.equal(timers.length, 1);
  answers["/api/v1/jobs/j-1"] = () => reply(200, { status: "succeeded" });
  answers["/api/v1/jobs/j-1/log"] = () => reply(200, { log: "done" });
  await timers[0]();
  assert.equal(component.jobStatus, "succeeded");
  assert.equal(component.log, "done");
  assert.equal(timers.length, 1);
});
