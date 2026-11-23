// The core modules every page loads (base.html), before htmx and Alpine. They put no name on
// `window`: the pages run under the enforced content security policy and reach nothing but
// what their components offer. Page modules live under ../pages/<group>.js and import what
// they need from here, e.g. readIsland from ./islands.js.
//
// The data island `mgr-core` (base.html) carries the texts in the language of the page:
// `texts`, the sentences of the shared actions, and `errors`, the kit's three-part texts per
// error class (app/client_texts.py). runtime.js builds the instances main.js and the page
// modules share from it.
import { installAnnouncements } from "./announce.js";
import { localizeCheckedAt } from "./format.js";
import { installFloatMenus } from "./menus.js";
import { installPollGuard } from "./poll.js";
import { report, texts } from "./runtime.js";
import { installSessionGuard } from "./session.js";

installFloatMenus(window, document);
installSessionGuard(document, report);
installPollGuard(document);
installAnnouncements(document, window);
document.addEventListener("htmx:afterSettle", (event) => {
  localizeCheckedAt(event.detail && event.detail.elt ? event.detail.elt : document, texts.justNow);
});
