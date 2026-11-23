// Toast lifetimes (core/toasts.js, PLT-61-A10) with a fake clock: success messages stand at
// least 6 s and as long as the pointer rests on them or the focus is inside; errors stay.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  lifetime,
  scheduleRemoval,
  SUCCESS_MIN_MS,
  track,
} from "../../src/app/static/js/core/toasts.js";

function fakeClock() {
  let now = 0;
  let next = 1;
  const timers = new Map();
  return {
    setTimeout(fn, ms) {
      const id = next++;
      timers.set(id, { at: now + ms, fn });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    advance(ms) {
      const until = now + ms;
      for (;;) {
        const due = [...timers.entries()]
          .filter(([, t]) => t.at <= until)
          .sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        timers.delete(due[0]);
        now = due[1].at;
        due[1].fn();
      }
      now = until;
    },
  };
}

function fakeToast() {
  const listeners = {};
  const inside = { name: "close button" };
  return {
    removed: false,
    inside,
    addEventListener(name, fn) {
      (listeners[name] ||= []).push(fn);
    },
    fire(name, event = {}) {
      for (const fn of listeners[name] || []) fn(event);
    },
    contains(node) {
      return node === inside;
    },
    remove() {
      this.removed = true;
    },
  };
}

test("a success message stands at least 6 s", () => {
  assert.equal(SUCCESS_MIN_MS, 6000);
  const clock = fakeClock();
  const toast = fakeToast();
  track(toast, "success", clock);
  clock.advance(5999);
  assert.equal(toast.removed, false);
  clock.advance(1);
  assert.equal(toast.removed, true);
});

test("info and warning messages follow the success rule", () => {
  for (const tone of ["info", "warning"]) {
    assert.equal(lifetime(tone), SUCCESS_MIN_MS);
    const clock = fakeClock();
    const toast = fakeToast();
    track(toast, tone, clock);
    clock.advance(5999);
    assert.equal(toast.removed, false, tone);
    clock.advance(1);
    assert.equal(toast.removed, true, tone);
  }
});

test("a message stays while the pointer rests on it", () => {
  const clock = fakeClock();
  const toast = fakeToast();
  track(toast, "success", clock);
  clock.advance(1000);
  toast.fire("pointerenter");
  clock.advance(60_000);
  assert.equal(toast.removed, false);
  toast.fire("pointerleave");
  assert.equal(toast.removed, true);
});

test("leaving early does not shorten the 6 s", () => {
  const clock = fakeClock();
  const toast = fakeToast();
  track(toast, "success", clock);
  toast.fire("pointerenter");
  clock.advance(2000);
  toast.fire("pointerleave");
  assert.equal(toast.removed, false);
  clock.advance(3999);
  assert.equal(toast.removed, false);
  clock.advance(1);
  assert.equal(toast.removed, true);
});

test("a message stays while the focus is inside", () => {
  const clock = fakeClock();
  const toast = fakeToast();
  track(toast, "success", clock);
  toast.fire("focusin");
  clock.advance(60_000);
  assert.equal(toast.removed, false);
  // Focus moving between elements of the message keeps it.
  toast.fire("focusout", { relatedTarget: toast.inside });
  assert.equal(toast.removed, false);
  toast.fire("focusout", { relatedTarget: null });
  assert.equal(toast.removed, true);
});

test("pointer and focus both have to leave", () => {
  const clock = fakeClock();
  const toast = fakeToast();
  track(toast, "success", clock);
  toast.fire("pointerenter");
  toast.fire("focusin");
  clock.advance(7000);
  toast.fire("pointerleave");
  assert.equal(toast.removed, false);
  toast.fire("focusout", { relatedTarget: null });
  assert.equal(toast.removed, true);
});

test("an error stays until it is closed", () => {
  assert.equal(lifetime("error"), null);
  const clock = fakeClock();
  const toast = fakeToast();
  assert.equal(track(toast, "error", clock), null);
  clock.advance(24 * 60 * 60 * 1000);
  toast.fire("pointerenter");
  toast.fire("pointerleave");
  toast.fire("focusin");
  toast.fire("focusout", { relatedTarget: null });
  assert.equal(toast.removed, false);
});

test("a scheduled removal can be cancelled", () => {
  const clock = fakeClock();
  const toast = fakeToast();
  const cancel = scheduleRemoval(toast, SUCCESS_MIN_MS, clock);
  cancel();
  clock.advance(60_000);
  assert.equal(toast.removed, false);
});
