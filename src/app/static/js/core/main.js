// The core modules every page loads (base.html), before htmx and Alpine, so the names of
// legacy.js exist when Alpine evaluates the first expression. Page modules live under
// ../pages/<group>.js and import what they need from here, e.g. readIsland from ./islands.js.
//
// The data island `mgr-core` (base.html) carries the texts in the language of the page:
// `texts`, the sentences of the shared actions, and `errors`, the kit's three-part texts per
// error class (app/client_texts.py). runtime.js builds the instances main.js and the page
// modules share from it.
import { ragDetail } from "./actions.js";
import { installAnnouncements } from "./announce.js";
import { csrfToken } from "./csrf.js";
import { escapeHtml, fmt, fmtHtml, fmtPlural, localizeCheckedAt } from "./format.js";
import { installLegacy } from "./legacy.js";
import { installFloatMenus } from "./menus.js";
import { installPollGuard } from "./poll.js";
import { actions, report, showToast, texts } from "./runtime.js";
import { installSessionGuard } from "./session.js";

installLegacy(window, {
  _ragDetail: ragDetail,
  _ragEscape: escapeHtml,
  _ragRequest: actions.ragRequest,
  csrfToken: () => csrfToken(document),
  fmt,
  fmtHtml,
  fmtPlural,
  showToast,
});

installFloatMenus(window, document);
installSessionGuard(document, report);
installPollGuard(document);
installAnnouncements(document, window);
document.addEventListener("htmx:afterSettle", (event) => {
  localizeCheckedAt(event.detail && event.detail.elt ? event.detail.elt : document, texts.justNow);
});
