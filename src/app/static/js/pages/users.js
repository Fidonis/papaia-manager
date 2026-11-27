// Users (users.html and the list it swaps in, partials/user_list.html). The page carries no
// inline script and no event attribute (T-UI-03): its sentences come from the data island
// `users-page` in the language of the page, what the list knows about the realm from the
// marker element of the list, and the dialogs are Alpine components registered here before
// Alpine starts (base.html, page_modules).
import { readIsland } from "../core/islands.js";
import { actions, toaster } from "../core/runtime.js";
import { listFailureHandler } from "./list-failure.js";
import { createUserDialogs } from "./users-dialogs.js";

const texts = readIsland("users-page").texts;

// What the list on screen says about the realm. The partial carries it in a marker element,
// so the dialogs (which live outside the list) never need a request of their own for it.
function usersMarker() {
  return document.querySelector("#user-list [data-users-marker]");
}

function reloadUsers() {
  const marker = usersMarker();
  const params = new URLSearchParams();
  const search = document.getElementById("user-search").value.trim();
  if (search) params.set("search", search);
  if (marker && marker.dataset.first) params.set("first", marker.dataset.first);
  window.htmx.ajax("GET", "/partials/users?" + params.toString(), { target: "#user-list", swap: "innerHTML" });
}

// No clipboard API outside a secure context: select the field, the person can copy by hand.
async function copyText(text, field) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    field.select();
    try { document.execCommand("copy"); } catch (__) {}
  }
}

const dialogs = createUserDialogs({
  request: actions.ragRequest,
  toast: (type, html) => toaster.show(type, html),
  reload: reloadUsers,
  announce: (name, detail) => window.dispatchEvent(new CustomEvent(name, { detail })),
  smtp: () => {
    const marker = usersMarker();
    return !!marker && marker.dataset.smtp === "true";
  },
  texts,
  copyText,
  now: () => Date.now(),
  later: (fn, ms) => window.setTimeout(fn, ms),
  lang: document.documentElement.lang || undefined,
});

document.addEventListener("alpine:init", () => {
  for (const [name, make] of Object.entries(dialogs)) window.Alpine.data(name, make);
});

// Typing searches through htmx; the form itself never submits.
document.getElementById("user-search-form").addEventListener("submit", (event) => event.preventDefault());
document.body.addEventListener("htmx:responseError", listFailureHandler(document, "user-list", "/users", texts));
