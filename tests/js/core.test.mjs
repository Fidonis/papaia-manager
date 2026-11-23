// Unit tests of the shared page modules under src/app/static/js/core/ (node --test).
//
// The modules take the document they work on as an argument, so these tests hand them a small
// stand-in instead of a browser.
import assert from "node:assert/strict";
import { test } from "node:test";

import { readIsland } from "../../src/app/static/js/core/islands.js";
import { installPollGuard, shouldSkipPoll } from "../../src/app/static/js/core/poll.js";

function island(attrs, text) {
  return {
    tagName: "SCRIPT",
    textContent: text,
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(attrs, name) ? attrs[name] : null;
    },
  };
}

function documentWith(elements) {
  return {
    getElementById(id) {
      return Object.prototype.hasOwnProperty.call(elements, id) ? elements[id] : null;
    },
  };
}

test("an island yields the value the server wrote", () => {
  const value = { name: "</script><script>alert(1)</script>", list: [1, null, true] };
  const doc = documentWith({
    data: island({ type: "application/json" }, JSON.stringify(value)),
  });
  assert.deepEqual(readIsland("data", doc), value);
});

test("a missing island is an error, not undefined", () => {
  assert.throws(() => readIsland("absent", documentWith({})), /no data island "absent"/);
});

test("only a JSON script element is an island", () => {
  const doc = documentWith({
    code: island({}, "1"),
    module: island({ type: "module" }, "1"),
    div: { tagName: "DIV", textContent: "1", getAttribute: () => "application/json" },
  });
  for (const id of ["code", "module", "div"]) {
    assert.throws(() => readIsland(id, doc), /not a data island/);
  }
});

test("malformed island content is reported with its id", () => {
  const doc = documentWith({ bad: island({ type: "application/json" }, "{oops") });
  assert.throws(() => readIsland("bad", doc), /data island "bad"/);
});

function pollElement(marked) {
  return {
    closest(selector) {
      assert.equal(selector, "[data-poll-visible]");
      return marked ? this : null;
    },
  };
}

test("a marked poll is skipped while the page is hidden", () => {
  assert.equal(shouldSkipPoll({ visibilityState: "hidden" }, pollElement(true)), true);
  assert.equal(shouldSkipPoll({ visibilityState: "visible" }, pollElement(true)), false);
});

test("requests of unmarked elements always go out", () => {
  assert.equal(shouldSkipPoll({ visibilityState: "hidden" }, pollElement(false)), false);
  assert.equal(shouldSkipPoll({ visibilityState: "hidden" }, null), false);
  assert.equal(shouldSkipPoll({ visibilityState: "hidden" }, {}), false);
});

test("the guard cancels the htmx request of a hidden poll", () => {
  const listeners = {};
  const doc = {
    visibilityState: "hidden",
    addEventListener(name, listener) {
      listeners[name] = listener;
    },
  };
  installPollGuard(doc);
  assert.deepEqual(Object.keys(listeners), ["htmx:beforeRequest"]);

  let cancelled = 0;
  const event = (elt) => ({ detail: { elt }, preventDefault: () => (cancelled += 1) });
  listeners["htmx:beforeRequest"](event(pollElement(true)));
  assert.equal(cancelled, 1);
  listeners["htmx:beforeRequest"](event(pollElement(false)));
  assert.equal(cancelled, 1);
  doc.visibilityState = "visible";
  listeners["htmx:beforeRequest"](event(pollElement(true)));
  assert.equal(cancelled, 1);
  listeners["htmx:beforeRequest"]({ preventDefault: () => (cancelled += 1) });
  assert.equal(cancelled, 1);
});
