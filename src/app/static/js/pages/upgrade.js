// Page module of the release upgrade (upgrade.html, T-UI-03). It runs before Alpine (base.html,
// block `page_modules`) and registers the header cluster `upgradeActions` when Alpine starts.
// Texts come from the data island `mgr-upgrade`; the partials htmx swaps in announce their
// state in the island `upgrade-state` and in data-* attributes, and name their buttons in
// `data-upgrade-action`. The page carries no inline script and no event attribute.
import { readIsland } from "../core/islands.js";
import { actions, showToast } from "../core/runtime.js";
import { createUpgradeActions, createUpgradePage } from "./upgrade/component.js";

const T = readIsland("mgr-upgrade").texts;

const page = createUpgradePage(T, {
  doc: document,
  fetchImpl: (url, options) => window.fetch(url, options),
  htmx: () => window.htmx,
  toast: showToast,
  authGuard: actions.authGuard,
  reload: () => window.location.reload(),
});

document.addEventListener("alpine:init", () => {
  window.Alpine.data("upgradeActions", () => createUpgradeActions(T, page));
});
document.addEventListener("click", page.handleClick);
document.addEventListener("change", page.handleChange);
document.addEventListener("htmx:afterSettle", (event) => page.announce(event.target, window));

// Opening the page is what means "look for upgrades". The cached result is
// rendered declaratively, so the page is never empty while this runs.
//
// DOMContentLoaded rather than the module body: htmx and Alpine are deferred
// bundles that run after this module, and deferred scripts all execute before
// this event -- so this is the earliest point where `htmx` is defined for the
// refresh that follows.
document.addEventListener("DOMContentLoaded", () => {
  page.runCheck(null);
});
