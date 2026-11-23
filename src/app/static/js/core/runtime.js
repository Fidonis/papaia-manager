// The shared instances of a page: the texts of the data island `mgr-core` (base.html), the
// toaster of the frame's live regions, the three-part report of a refusal and the shared
// actions. main.js and the page modules that run on import (../pages/<group>.js) import them
// from here; they are the instances of page.js, so a page module that takes them from
// pageCore() on first use gets the same ones.
import { pageCore } from "./page.js";

export const { core, texts, toaster, report, actions } = pageCore(document, window);

// A message of `type` (a tone of toasts.js) whose markup the caller has escaped.
export function showToast(type, html) {
  toaster.show(type, html);
}
