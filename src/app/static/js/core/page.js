// The shared instances of one page: its texts (data island `mgr-core` of base.html), the
// toaster, the three-part report of a refusal and the shared actions. runtime.js exports them
// for main.js and the page modules that run on import; a page module a unit test imports takes
// them from pageCore() on first use instead, so it never reads a page. Either way a page has
// one of each.
import { createActions } from "./actions.js";
import { describeFailure } from "./errors.js";
import { readIsland } from "./islands.js";
import { SESSION_KEY, SESSION_STATUS } from "./session.js";
import { createToaster } from "./toasts.js";

let shared = null;

export function pageCore(doc = globalThis.document, win = globalThis.window) {
  if (shared) return shared;
  const core = readIsland("mgr-core", doc);
  const texts = core.texts;
  const toaster = createToaster({ doc });

  // Tells a failure in three parts. A session end gets one message at a time whose part 2 is
  // a link to this page, which signs in again.
  function report(failure) {
    const parts = describeFailure(failure, core.errors);
    if (failure.status === SESSION_STATUS) {
      toaster.showError(parts, { key: SESSION_KEY, href: win.location.href });
    } else {
      toaster.showError(parts);
    }
  }

  const actions = createActions({ texts, toaster, report });
  shared = { core, texts, toaster, report, actions };
  return shared;
}
