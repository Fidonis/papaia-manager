// The shared actions (core/actions.js) against a stand-in fetch: every request carries the
// CSRF token and a correlation id; a refusal is reported once with the id the server answered
// with; a request without an answer keeps the id it was sent with; a session end stops.
import assert from "node:assert/strict";
import { test } from "node:test";

import { createActions, ragDetail, stackBody } from "../../src/app/static/js/core/actions.js";
import { CORRELATION_HEADER } from "../../src/app/static/js/core/errors.js";

const doc = {
  querySelector(selector) {
    if (selector === "input[name=pk_csrf]") return { value: "csrf-of-the-session" };
    return {};
  },
};

function reply(status, body, id) {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (name) => (name === CORRELATION_HEADER ? id : null) },
    json: async () => body,
  };
}

function setup(answer) {
  const sent = [];
  const reported = [];
  const shown = [];
  let ids = 0;
  const actions = createActions({
    texts: {
      stackActionStarted: "Started.",
      catalogDeleted: 'Catalog "{name}" deleted.',
      catalogRefreshed: 'Catalog "{name}" refreshed.',
    },
    toaster: { show: (type, html) => shown.push([type, html]) },
    report: (failure) => reported.push(failure),
    fetchImpl: async (url, options) => {
      sent.push({ url, options });
      return answer(url, options);
    },
    htmx: () => ({ ajax: () => {} }),
    doc,
    clock: { setTimeout: () => 0 },
    newId: () => `0190f3a2-0000-7000-8000-00000000000${++ids}`,
  });
  return { actions, sent, reported, shown };
}

test("a write carries the CSRF token and a correlation id of its own", async () => {
  const { actions, sent, shown } = setup(() => reply(200, { ok: true }, "x"));
  assert.equal(await actions.stackAction("restart", { clean_up: false }), true);
  const { url, options } = sent[0];
  assert.equal(url, "/api/v1/stack/restart");
  assert.equal(options.method, "POST");
  assert.equal(options.headers["X-CSRF-Token"], "csrf-of-the-session");
  assert.equal(options.headers[CORRELATION_HEADER], "0190f3a2-0000-7000-8000-000000000001");
  assert.equal(options.body, JSON.stringify({ clean_up: false }));
  assert.deepEqual(shown, [["info", "Started."]]);
});

test("a refusal is reported once, with the id the server logged", async () => {
  const { actions, reported, shown } = setup(() =>
    reply(403, { detail: "forbidden" }, "0190f3a2-0000-7000-8000-0000000000ff"),
  );
  assert.equal(await actions.catalogDelete("papers"), false);
  assert.equal(reported.length, 1);
  assert.equal(reported[0].status, 403);
  assert.equal(reported[0].correlationId, "0190f3a2-0000-7000-8000-0000000000ff");
  assert.deepEqual(shown, []);
});

test("a request without an answer is reported as a stopped connection", async () => {
  const { actions, reported } = setup(() => {
    throw new TypeError("Failed to fetch");
  });
  assert.equal(await actions.addonAction("n8n", "start"), false);
  assert.deepEqual(reported, [
    { status: 0, body: null, network: true, correlationId: "0190f3a2-0000-7000-8000-000000000001" },
  ]);
});

test("the knowledge pages keep their error text; a session end is handled for them", async () => {
  let status = 409;
  const { actions, reported } = setup(() => reply(status, { detail: "The name is taken." }, "id-1"));
  assert.deepEqual(await actions.ragRequest("POST", "/api/v1/collections", { name: "a" }), {
    ok: false,
    error: "The name is taken.",
  });
  assert.equal(reported.length, 0);
  status = 401;
  assert.deepEqual(await actions.ragRequest("GET", "/api/v1/collections"), { ok: false, handled: true });
  assert.equal(reported.length, 1);
  assert.equal(reported[0].status, 401);
});

test("the session guard tells a raw response apart", () => {
  const { actions, reported } = setup(() => reply(200, {}, "x"));
  assert.equal(actions.authGuard(reply(200, {}, "a")), false);
  assert.equal(actions.authGuard(reply(401, {}, "0190f3a2-0000-7000-8000-0000000000aa")), true);
  assert.equal(reported[0].correlationId, "0190f3a2-0000-7000-8000-0000000000aa");
});

test("stack body and refusal details", () => {
  assert.deepEqual(stackBody("start", true), {});
  assert.deepEqual(stackBody("stop", true), { clean_up: true });
  assert.equal(ragDetail({ detail: { message: "Refused." } }, 400), "Refused.");
  assert.equal(ragDetail({ detail: [{ msg: "a" }, { msg: "b" }] }, 422), "a; b");
  assert.equal(ragDetail(null, 500), "500");
});

test("a catalog refresh names the catalog in the path once, encoded, and tells the outcome", async () => {
  const { actions, sent, reported, shown } = setup((url) =>
    url.startsWith("/api/v1/jobs/")
      ? reply(200, { id: "j-1", status: "succeeded" }, "x")
      : reply(202, { job_id: "j-1" }, "x"),
  );
  assert.equal(await actions.catalogAction("team a/b", "refresh"), true);
  assert.equal(sent[0].url, "/api/v1/catalogs/team%20a%2Fb/refresh");
  assert.equal(sent[0].options.method, "POST");
  assert.equal(reported.length, 0);
  assert.deepEqual(shown, [["success", 'Catalog "team a/b" refreshed.']]);
});

test("a refused catalog refresh is reported in three parts, not as a text of its own", async () => {
  const { actions, reported, shown } = setup(() =>
    reply(403, { detail: "forbidden" }, "0190f3a2-0000-7000-8000-0000000000fe"),
  );
  assert.equal(await actions.catalogAction("papers", "refresh"), false);
  assert.equal(reported.length, 1);
  assert.equal(reported[0].status, 403);
  assert.equal(reported[0].correlationId, "0190f3a2-0000-7000-8000-0000000000fe");
  assert.deepEqual(shown, []);
});

// A maintenance action (partials/addon_detail_content.html): the confirmation goes with the
// request, the panel is redrawn at once (it shows the run as running) and again when the job
// ends, and a refusal goes to the section that asked when it has its own place for it.
function maintenance(answer) {
  const sent = [];
  const reported = [];
  const shown = [];
  const swaps = [];
  const actions = createActions({
    texts: { maintenanceActionFailed: "{action} failed", maintenanceActionFinished: "{action} finished" },
    toaster: { show: (type, html) => shown.push([type, html]) },
    report: (failure) => reported.push(failure),
    fetchImpl: async (url, options) => {
      sent.push({ url, options });
      return answer(url, options);
    },
    htmx: () => ({ ajax: (...args) => swaps.push(args) }),
    doc,
    clock: { setTimeout: () => 0 },
    newId: () => "0190f3a2-0000-7000-8000-000000000001",
  });
  return { actions, sent, reported, shown, swaps };
}

test("a maintenance action sends whether it was confirmed and redraws the panel twice", async () => {
  const { actions, sent, reported, shown, swaps } = maintenance((url) =>
    url.startsWith("/api/v1/jobs/")
      ? reply(200, { id: "j-9", status: "failed" }, "x")
      : reply(202, { job_id: "j-9" }, "x"),
  );
  assert.equal(await actions.addonMaintenanceAction("wiki", "purge", true, () => {}), true);
  assert.equal(sent[0].url, "/api/v1/addons/wiki/actions/purge");
  assert.equal(sent[0].options.body, JSON.stringify({ confirmed: true }));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(swaps.length, 2);
  assert.deepEqual(swaps[0], ["GET", "/partials/addons/wiki", { target: "#addon-detail", swap: "innerHTML" }]);
  assert.deepEqual(shown, [["error", "purge failed"]]);
  assert.equal(reported.length, 0);

  const plain = maintenance(() => reply(202, { job_id: "j-1" }, "x"));
  await plain.actions.addonMaintenanceAction("wiki", "check");
  assert.equal(plain.sent[0].options.body, JSON.stringify({ confirmed: false }));
});

test("a refused maintenance action goes to the section; a session end is reported", async () => {
  const refused = [];
  const { actions, reported, swaps } = maintenance(() =>
    reply(422, { detail: "Confirm it first." }, "0190f3a2-0000-7000-8000-0000000000fd"),
  );
  assert.equal(await actions.addonMaintenanceAction("wiki", "purge", false, (f) => refused.push(f)), false);
  assert.equal(refused.length, 1);
  assert.equal(refused[0].status, 422);
  assert.equal(refused[0].correlationId, "0190f3a2-0000-7000-8000-0000000000fd");
  assert.equal(reported.length, 0);
  assert.equal(swaps.length, 0);

  const ended = maintenance(() => reply(401, null, "x"));
  const none = [];
  assert.equal(await ended.actions.addonMaintenanceAction("wiki", "purge", true, (f) => none.push(f)), false);
  assert.equal(none.length, 0);
  assert.equal(ended.reported.length, 1);

  const without = maintenance(() => reply(409, { detail: "Locked." }, "x"));
  assert.equal(await without.actions.addonMaintenanceAction("wiki", "check"), false);
  assert.equal(without.reported.length, 1);
});
