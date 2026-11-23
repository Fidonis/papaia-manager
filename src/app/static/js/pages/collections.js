// Collections (collections.html with partials/collection_list.html).
//
// The three dialogs of the list are Alpine components: `newCollection`, `editRoles` and
// `deleteCollection`. A row reaches the dialog of its collection through the window events
// `collection-roles` and `collection-delete`. Every change names the connection of the list
// it was made on (the list's marker element, `data-connection`), not whatever the selector
// shows by then. The page's sentences come from the data island `collections-texts`.
//
// Loaded after core/main.js and before Alpine (base.html, block `page_modules`), on the CSP
// build of Alpine: the templates call plain methods of the components, and no server value
// reaches this file as script text.
import { ragDetail } from "../core/actions.js";
import { escapeHtml, fmtHtml } from "../core/format.js";
import { readIsland } from "../core/islands.js";
import { fillMarked } from "../core/marked.js";
import { pageCore } from "../core/page.js";

export const TEXTS_ISLAND = "collections-texts";

// The connection the list on screen belongs to. The partial carries it on a marker element;
// before that has loaded, whatever the selector shows.
export function collectionConnection(doc) {
  const shown = doc.querySelector("#collection-list [data-connection]");
  if (shown) return shown.getAttribute("data-connection");
  const select = doc.getElementById("collection-connection");
  return select ? select.value : "default";
}

export function collectionUrl(doc, path) {
  return "/api/v1/rag/collections" + path + "?connection=" + encodeURIComponent(collectionConnection(doc));
}

export function reloadCollections(doc, htmx) {
  htmx.ajax("GET", "/partials/collections?connection=" + encodeURIComponent(collectionConnection(doc)),
            { target: "#collection-list", swap: "innerHTML" });
}

// The role rows of a dialog: blank rows are dropped, names are trimmed.
export function collectionRoles(rows) {
  return rows
    .map(function (row) { return { role: row.role.trim(), access: row.access }; })
    .filter(function (row) { return row.role; });
}

// htmx does not swap a 4xx answer, which would leave the list of a connection that has
// vanished (deleted in the ingester's interface) on screen. Say so instead. A 401 is
// handled by the page itself.
export function showListError(detail, texts) {
  if (!detail.target || detail.target.id !== "collection-list") return false;
  if (detail.xhr && detail.xhr.status === 401) return false;
  let reason = texts.loadFailed;
  try {
    reason = ragDetail(JSON.parse(detail.xhr.responseText), detail.xhr.status);
  } catch {
    // Not JSON: the general sentence stays.
  }
  detail.target.innerHTML =
    '<div class="flex flex-col items-center py-24 gap-3 text-pk-text-muted text-center">' +
    '<p class="text-lg font-semibold">' + escapeHtml(texts.unavailable) + "</p>" +
    '<p class="max-w-xl text-sm">' + escapeHtml(reason) + "</p>" +
    '<a class="link" href="/collections">' + escapeHtml(texts.reloadPage) + "</a></div>";
  return true;
}

// The dialogs. `deps` are the page's browser parts, so they run under `node --test` with
// stand-ins.
export function createNewCollection(deps) {
  const { texts, doc, htmx, ragRequest, showToast } = deps;
  return {
    name: "", vectorSize: "", model: "", roles: [], busy: false, error: "",
    addRole() { this.roles.push({ role: "", access: "r" }); },
    removeRole(i) { this.roles.splice(i, 1); },
    reset() { this.name = ""; this.vectorSize = ""; this.model = ""; this.roles = []; this.error = ""; },
    close() { this.$root.close(); },
    async submit() {
      this.error = "";
      this.busy = true;
      const name = this.name.trim();
      const res = await ragRequest("POST", collectionUrl(doc, ""), {
        name: name,
        vector_size: Number(this.vectorSize),
        embedding_model: this.model.trim() || null,
        roles: collectionRoles(this.roles),
      });
      this.busy = false;
      if (res.handled) return;
      if (!res.ok) { this.error = res.error; return; }
      this.$root.close();
      showToast("success", fmtHtml(texts.created, { name: name }));
      reloadCollections(doc, htmx);
    },
  };
}

export function createEditRoles(deps) {
  const { texts, doc, htmx, ragRequest, showToast } = deps;
  return {
    name: "", roles: [], busy: false, error: "",
    open(detail) {
      this.name = detail.name;
      this.roles = detail.roles.map(function (row) { return { role: row.role, access: row.access }; });
      this.error = "";
      this.$root.showModal();
    },
    close() { this.$root.close(); },
    markTitle(el) {
      fillMarked(el, texts.rolesTitle, { name: this.name });
    },
    addRole() { this.roles.push({ role: "", access: "r" }); },
    removeRole(i) { this.roles.splice(i, 1); },
    async submit() {
      this.error = "";
      this.busy = true;
      const res = await ragRequest(
        "PUT", collectionUrl(doc, "/" + encodeURIComponent(this.name) + "/roles"),
        { roles: collectionRoles(this.roles) });
      this.busy = false;
      if (res.handled) return;
      if (!res.ok) { this.error = res.error; return; }
      this.$root.close();
      showToast("success", fmtHtml(texts.rolesSaved, { name: this.name }));
      reloadCollections(doc, htmx);
    },
  };
}

export function createDeleteCollection(deps) {
  const { texts, doc, htmx, ragRequest, showToast } = deps;
  return {
    name: "", points: null, typed: "", busy: false, error: "",
    open(detail) {
      this.name = detail.name;
      this.points = detail.points;
      this.typed = "";
      this.error = "";
      this.$root.showModal();
    },
    close() { this.$root.close(); },
    markTitle(el) {
      fillMarked(el, texts.deleteTitle, { name: this.name });
    },
    markPoints(el) {
      fillMarked(el, texts.points, { points: this.points });
    },
    async submit() {
      this.error = "";
      this.busy = true;
      const res = await ragRequest("DELETE", collectionUrl(doc, "/" + encodeURIComponent(this.name)));
      this.busy = false;
      if (res.handled) return;
      if (!res.ok) { this.error = res.error; return; }
      this.$root.close();
      showToast("success", fmtHtml(texts.deleted, { name: this.name }));
      reloadCollections(doc, htmx);
    },
  };
}

// "Create grant" of the list's warning: the operator role's global grant.
export async function createOperatorGrant(button, deps) {
  const { texts, doc, htmx, ragRequest, showToast } = deps;
  button.disabled = true;
  const res = await ragRequest("POST", collectionUrl(doc, "/operator-grant"));
  if (res.handled) return;
  if (!res.ok) {
    button.disabled = false;
    showToast("error", texts.grantFailed + " " + escapeHtml(res.error));
    return;
  }
  reloadCollections(doc, htmx);
}

// The page's listeners outside the components: "New collection" in the header, "Create
// grant", the connection selector and a list that could not be loaded.
export function installCollectionListeners(win, doc, deps) {
  doc.addEventListener("click", (event) => {
    const target = event.target;
    if (!target || typeof target.closest !== "function") return;
    if (target.closest("[data-collection-new]")) {
      const dialog = doc.getElementById("create-collection-modal");
      if (dialog) dialog.showModal();
      return;
    }
    const grant = target.closest("[data-collection-grant]");
    if (grant) createOperatorGrant(grant, deps);
  });
  doc.addEventListener("submit", (event) => {
    if (event.target && event.target.id === "collection-connection-form") event.preventDefault();
  });
  doc.addEventListener("change", (event) => {
    const select = event.target;
    if (!select || select.id !== "collection-connection") return;
    win.history.replaceState(null, "", "/collections?connection=" + encodeURIComponent(select.value));
  });
  doc.addEventListener("htmx:responseError", (event) => {
    showListError(event.detail || {}, deps.texts);
  });
}

export function installCollections(win, doc) {
  const { actions, toaster } = pageCore(doc, win);
  const deps = {
    texts: readIsland(TEXTS_ISLAND, doc),
    doc,
    get htmx() { return win.htmx; },
    ragRequest: actions.ragRequest,
    showToast: (type, html) => toaster.show(type, html),
  };
  installCollectionListeners(win, doc, deps);
  doc.addEventListener("alpine:init", () => {
    win.Alpine.data("newCollection", () => createNewCollection(deps));
    win.Alpine.data("editRoles", () => createEditRoles(deps));
    win.Alpine.data("deleteCollection", () => createDeleteCollection(deps));
  });
}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  installCollections(window, document);
}
