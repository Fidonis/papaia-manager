// The shared instances of a page: the texts of the data island `mgr-core` (base.html), the
// toaster of the frame's live regions, the three-part report of a refusal and the shared
// actions. Built once per page: main.js and the page modules (../pages/<group>.js) import
// them from here, and the browser evaluates a module once per URL.
import { createActions } from "./actions.js";
import { describeFailure } from "./errors.js";
import { readIsland } from "./islands.js";
import { SESSION_KEY, SESSION_STATUS } from "./session.js";
import { createToaster } from "./toasts.js";

export const core = readIsland("mgr-core");
export const texts = core.texts;
export const toaster = createToaster({ doc: document });

// Tells a failure in three parts. A session end gets one message at a time whose part 2 is a
// link to this page, which signs in again.
export function report(failure) {
  const parts = describeFailure(failure, core.errors);
  if (failure.status === SESSION_STATUS) {
    toaster.showError(parts, { key: SESSION_KEY, href: window.location.href });
  } else {
    toaster.showError(parts);
  }
}

export const actions = createActions({ texts, toaster, report });

// A message of `type` (a tone of toasts.js) whose markup the caller has escaped.
export function showToast(type, html) {
  toaster.show(type, html);
}
