// Page module of the add-on upgrade (addon_upgrade.html, T-UI-03). It runs before Alpine
// (base.html, block `page_modules`) and registers the component `addonUpgrade` when Alpine
// starts; the component reads the add-on's name and its texts from the data island
// `mgr-addon-upgrade`. The page carries no inline script.
import { readIsland } from "../core/islands.js";
import { actions } from "../core/runtime.js";
import { createAddonUpgrade } from "./addon-upgrade/component.js";

document.addEventListener("alpine:init", () => {
  window.Alpine.data("addonUpgrade", () =>
    createAddonUpgrade(readIsland("mgr-addon-upgrade"), {
      doc: document,
      fetchImpl: (url, options) => window.fetch(url, options),
      authGuard: actions.authGuard,
      clock: window,
    }),
  );
});
