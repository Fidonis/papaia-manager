// The core modules every page loads (base.html), before htmx and Alpine, so the names of
// legacy.js exist when Alpine evaluates the first expression. Page modules live under
// ../pages/<group>.js and import what they need from here, e.g. readIsland from ./islands.js.
//
// The data island `mgr-core` (base.html) carries the texts in the language of the page:
// `texts`, the sentences of the shared actions, and `errors`, the kit's three-part texts per
// error class (app/client_texts.py).
import { createActions, ragDetail, stackBody } from "./actions.js";
import { csrfToken } from "./csrf.js";
import { describeFailure } from "./errors.js";
import { escapeHtml, fmt, fmtHtml, fmtPlural, localizeCheckedAt } from "./format.js";
import { readIsland } from "./islands.js";
import { installLegacy } from "./legacy.js";
import { installFloatMenus } from "./menus.js";
import { installPollGuard } from "./poll.js";
import { installSessionGuard, SESSION_KEY, SESSION_STATUS } from "./session.js";
import { createToaster } from "./toasts.js";

const core = readIsland("mgr-core");
const texts = core.texts;
const toaster = createToaster({ doc: document });

// Tells a failure in three parts. A session end gets one message at a time whose part 2 is a
// link to this page, which signs in again.
function report(failure) {
  const parts = describeFailure(failure, core.errors);
  if (failure.status === SESSION_STATUS) {
    toaster.showError(parts, { key: SESSION_KEY, href: window.location.href });
  } else {
    toaster.showError(parts);
  }
}

const actions = createActions({ texts, toaster, report });

installLegacy(window, {
  I18N: texts,
  _addonPost: actions.addonPost,
  _authGuard: actions.authGuard,
  _pollJob: actions.pollJob,
  _ragDetail: ragDetail,
  _ragError: actions.ragError,
  _ragEscape: escapeHtml,
  _ragRequest: actions.ragRequest,
  _stackBody: stackBody,
  addonAction: actions.addonAction,
  addonMaintenanceAction: actions.addonMaintenanceAction,
  catalogAction: actions.catalogAction,
  catalogDelete: actions.catalogDelete,
  catalogUpdate: actions.catalogUpdate,
  csrfToken: () => csrfToken(document),
  fmt,
  fmtHtml,
  fmtPlural,
  galleryAction: actions.galleryAction,
  refreshStackRunner: actions.refreshStackRunner,
  serviceAddonAction: actions.serviceAddonAction,
  showToast: (type, html) => {
    toaster.show(type, html);
  },
  stackAction: actions.stackAction,
  stackGroupAction: actions.stackGroupAction,
});

installFloatMenus(window, document);
installSessionGuard(document, report);
installPollGuard(document);
document.addEventListener("htmx:afterSettle", (event) => {
  localizeCheckedAt(event.detail && event.detail.elt ? event.detail.elt : document, texts.justNow);
});
