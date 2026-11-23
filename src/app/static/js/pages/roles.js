// Roles (roles.html and the list it swaps in, partials/role_list.html). The page carries no
// inline script and no event attribute (T-UI-03): its sentences come from the data island
// `roles-page` in the language of the page, and the dialogs are Alpine components registered
// here before Alpine starts (base.html, page_modules).
import { readIsland } from "../core/islands.js";
import { actions, toaster } from "../core/runtime.js";
import { listFailureHandler } from "./list-failure.js";
import { createRoleDialogs } from "./roles-dialogs.js";

const texts = readIsland("roles-page").texts;

function reloadRoles() {
  window.htmx.ajax("GET", "/partials/roles", { target: "#role-list", swap: "innerHTML" });
}

const dialogs = createRoleDialogs({
  request: actions.ragRequest,
  toast: (type, html) => toaster.show(type, html),
  reload: reloadRoles,
  texts,
  lang: document.documentElement.lang || undefined,
});

document.addEventListener("alpine:init", () => {
  for (const [name, make] of Object.entries(dialogs)) window.Alpine.data(name, make);
});

document.body.addEventListener("htmx:responseError", listFailureHandler(document, "role-list", "/users/roles", texts));
