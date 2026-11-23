// Unit tests of core/announce.js: a fragment htmx swaps in states what it announces in data
// attributes, and the module tells the page as a window event, without inline script.
import assert from "node:assert/strict";
import { test } from "node:test";

import { announceSettled, installAnnouncements } from "../../src/app/static/js/core/announce.js";

function element(dataset, children = []) {
  return {
    dataset,
    matches(selector) {
      assert.equal(selector, "[data-announce]");
      return Object.prototype.hasOwnProperty.call(dataset, "announce");
    },
    querySelectorAll(selector) {
      assert.equal(selector, "[data-announce]");
      return children.filter((c) => c.matches(selector));
    },
  };
}

function recorder() {
  const events = [];
  return {
    events,
    dispatchEvent(event) {
      events.push([event.type, event.detail]);
      return true;
    },
  };
}

test("a settled fragment that announces itself is told as a window event", () => {
  const win = recorder();
  announceSettled(element({ announce: "papaia-config-job", active: "true" }), win);
  assert.deepEqual(win.events, [["papaia-config-job", { active: true }]]);
});

test("anything but the word true is not active", () => {
  const win = recorder();
  for (const active of ["false", "", "TRUE", undefined]) {
    announceSettled(element({ announce: "papaia-config-job", active }), win);
  }
  assert.deepEqual(win.events.map(([, d]) => d.active), [false, false, false, false]);
});

test("announcing fragments inside a settled element are told as well", () => {
  const win = recorder();
  const inner = element({ announce: "papaia-config-job", active: "false" });
  announceSettled(element({}, [inner]), win);
  assert.deepEqual(win.events, [["papaia-config-job", { active: false }]]);
});

test("only papaia event names are announced", () => {
  const win = recorder();
  for (const name of ["click", "papaia-", "papaia-Config", "papaia-config job", "x-papaia-a"]) {
    announceSettled(element({ announce: name, active: "true" }), win);
  }
  assert.deepEqual(win.events, []);
});

test("a settle without an element is ignored", () => {
  const win = recorder();
  announceSettled(null, win);
  announceSettled({}, win);
  assert.deepEqual(win.events, []);
});

test("the module listens to htmx:afterSettle on the document", () => {
  const win = recorder();
  const listeners = {};
  const doc = { addEventListener: (type, fn) => { listeners[type] = fn; } };
  installAnnouncements(doc, win);
  listeners["htmx:afterSettle"]({ target: element({ announce: "papaia-config-job", active: "true" }) });
  assert.deepEqual(win.events, [["papaia-config-job", { active: true }]]);
});
