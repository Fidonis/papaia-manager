// Polls that pause while the page is hidden (T-UI-03: htmx runs without eval, so the trigger
// filter `[document.visibilityState === 'visible']` is not available).
//
// An element marked `data-poll-visible` sends no request while the page is hidden: the
// listener cancels htmx's request before it goes out. Its trigger lists
// `visibilitychange from:document` as well, so it catches up as soon as the page is shown.

export function shouldSkipPoll(doc, element) {
  if (doc.visibilityState !== "hidden") return false;
  if (!element || typeof element.closest !== "function") return false;
  return element.closest("[data-poll-visible]") !== null;
}

export function installPollGuard(doc = document) {
  doc.addEventListener("htmx:beforeRequest", (event) => {
    const element = event.detail ? event.detail.elt : null;
    if (shouldSkipPoll(doc, element)) event.preventDefault();
  });
}
