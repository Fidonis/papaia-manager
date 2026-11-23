// Add-on catalogs (catalogs.html), T-UI-03.
//
// The page has two dialogs, "Add catalog" and "Edit catalog"; the module adds the listeners
// their elements used to carry as on* attributes and finds the elements by their ids, which
// stay as they were. A button with `data-dialog-open` / `data-dialog-close` opens or closes
// the dialog of that id. The list (partials/catalog_list.html, swapped in by htmx) carries
// the Alpine component `catalogRow` per catalog: refresh, delete after a question, and the
// edit dialog filled from the row's data attributes. The sentences come from the data island
// `catalogs-texts`. Everything the page talks to comes in as an argument; the bottom of this
// file wires it up.
import { csrfToken } from "../core/csrf.js";
import { readIsland } from "../core/islands.js";
import { pageCore } from "../core/page.js";

export function installCatalogPage(doc, { texts, actions, fetchImpl, htmx, alert }) {
  const el = (id) => doc.getElementById(id);

  function submitAddCatalog(event) {
    event.preventDefault();
    const form = el("add-catalog-form");
    const type = form.elements["type"].value;
    const body = { name: form.elements["name"].value, type: type };
    if (type === "git") {
      body.url = form.elements["url"].value;
      body.ref = form.elements["ref"].value;
    } else {
      body.path = form.elements["path"].value;
    }
    return fetchImpl("/api/v1/catalogs", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken(doc),
      },
      body: JSON.stringify(body),
    }).then(async function (r) {
      if (actions.authGuard(r)) return;
      if (!r.ok) {
        let detail = r.status;
        try { const e = await r.json(); detail = e.detail || JSON.stringify(e); } catch { /* the status stands */ }
        alert(texts.addFailed + " " + detail);
        return;
      }
      el("add-catalog-modal").close();
      htmx().ajax("GET", "/partials/catalogs", { target: "#catalog-list", swap: "innerHTML" });
    });
  }

  function toggleCatalogFields(type) {
    const urlField = el("field-url");
    const refField = el("field-ref");
    const pathField = el("field-path");
    const urlInput = urlField.querySelector("input");
    const refInput = refField.querySelector("input");
    const pathInput = pathField.querySelector("input");
    if (type === "local") {
      urlField.classList.add("hidden");
      urlInput.required = false;
      urlInput.disabled = true;
      refField.classList.add("hidden");
      refInput.required = false;
      refInput.disabled = true;
      pathField.classList.remove("hidden");
      pathInput.required = true;
      pathInput.disabled = false;
    } else {
      pathField.classList.add("hidden");
      pathInput.required = false;
      pathInput.disabled = true;
      urlField.classList.remove("hidden");
      urlInput.required = true;
      urlInput.disabled = false;
      refField.classList.remove("hidden");
      refInput.required = true;
      refInput.disabled = false;
    }
  }

  function openEditCatalogModal(name, type, url, ref, path, enabled) {
    el("edit-name").value = name;
    el("edit-type").value = type;
    el("edit-url").value = url || "";
    el("edit-ref").value = ref || "main";
    el("edit-path").value = path || "";
    el("edit-enabled").checked = !!enabled;

    const urlField = el("edit-field-url");
    const refField = el("edit-field-ref");
    const pathField = el("edit-field-path");
    if (type === "local") {
      urlField.classList.add("hidden");
      refField.classList.add("hidden");
      pathField.classList.remove("hidden");
    } else {
      pathField.classList.add("hidden");
      urlField.classList.remove("hidden");
      refField.classList.remove("hidden");
    }

    const form = el("edit-catalog-form");
    form.dataset.name = name;
    form.dataset.type = type;
    el("edit-catalog-modal").showModal();
  }

  function submitEditCatalog(event) {
    event.preventDefault();
    const form = el("edit-catalog-form");
    const name = form.dataset.name;
    const type = form.dataset.type;
    const body = { enabled: el("edit-enabled").checked };
    if (type === "git") {
      body.url = el("edit-url").value;
      body.ref = el("edit-ref").value;
    } else {
      body.path = el("edit-path").value;
    }
    return actions.catalogUpdate(name, body).then(function (ok) {
      if (ok) el("edit-catalog-modal").close();
    });
  }

  doc.querySelectorAll("[data-dialog-open]").forEach((button) => {
    button.addEventListener("click", () => el(button.dataset.dialogOpen).showModal());
  });
  doc.querySelectorAll("[data-dialog-close]").forEach((button) => {
    button.addEventListener("click", () => el(button.dataset.dialogClose).close());
  });
  el("add-catalog-form").addEventListener("submit", submitAddCatalog);
  el("catalog-type-select").addEventListener("change", (event) => toggleCatalogFields(event.target.value));
  el("add-catalog-modal").addEventListener("close", function () {
    el("catalog-type-select").value = "git";
    toggleCatalogFields("git");
  });
  el("edit-catalog-form").addEventListener("submit", submitEditCatalog);

  return { openEditCatalogModal };
}

// One row of the list. Refreshing is the verb the row is there for; delete asks first. One
// `busy` for the row, so a refresh in flight also locks the menu.
export function catalogRow({ actions, confirm, openEdit }) {
  return {
    busy: false,
    jobId: null,
    refresh(name) {
      this.busy = true;
      return actions.catalogAction(name, "refresh", (id) => {
        this.jobId = id;
      }).then(() => {
        this.busy = false;
        this.jobId = null;
      });
    },
    remove(name, question) {
      if (!confirm(question)) return Promise.resolve();
      this.busy = true;
      return actions.catalogDelete(name).then(() => {
        this.busy = false;
      });
    },
    // The edit item carries the catalog's values (`item`, its data attributes).
    edit(item) {
      const d = item.dataset;
      openEdit(d.name, d.type, d.url, d.ref, d.path, d.enabled === "true");
    },
  };
}

// The page: module scripts run once the document is parsed, so the dialogs are there; the
// rows are registered for Alpine, which starts after this module ran.
if (globalThis.document && document.getElementById("catalogs-texts")) {
  const { actions } = pageCore();
  const page = installCatalogPage(document, {
    texts: readIsland("catalogs-texts"),
    actions,
    fetchImpl: (...args) => window.fetch(...args),
    htmx: () => window.htmx,
    alert: (text) => window.alert(text),
  });
  document.addEventListener("alpine:init", () => {
    window.Alpine.data("catalogRow", () =>
      catalogRow({ actions, confirm: (text) => window.confirm(text), openEdit: page.openEditCatalogModal }));
  });
}
