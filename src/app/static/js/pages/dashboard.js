// Dashboard editor (dashboard.html, partials/tile_editor.html, partials/tile_gallery.html).
//
// The editable dashboard is a client-side draft: nothing reaches the server until "Save
// changes" PUTs the whole document. That is what makes "Discard" an undo for everything,
// deletions included, and it is also the only shape that fits the file -- tiles have no
// identity beyond their position, so there is nothing for a per-tile endpoint to address.
//
// State lives in an Alpine store rather than in the partial, because the header cluster and
// the gallery are swapped as two separate elements and both need the same draft and the same
// unsaved-change count. The editor's root is the component `tileEditor`: it reads the draft
// from the data island `tiles-editor` of the partial and keeps drag and drop in step with the
// draft. The page's texts come from the data island `dashboard-texts` (dashboard.html).
//
// Loaded for administrators only, after core/main.js and before Alpine (base.html, block
// `page_modules`), on the CSP build of Alpine: every expression of the templates is a plain
// call or property of the store, and nothing here reaches the templates as script text.
import { csrfToken } from "../core/csrf.js";
import { escapeHtml, fmt, fmtPlural } from "../core/format.js";
import { readIsland } from "../core/islands.js";
import { pageCore } from "../core/page.js";

export const TEXTS_ISLAND = "dashboard-texts";
export const EDITOR_ISLAND = "tiles-editor";
const COUNT_MARK = '<span class="font-semibold">{count}</span>';
// An icon the page may load under the enforced policy: the rule of check_icon in
// app/core/tiles.py, which decides on save (the length cap is the server's alone).
const LOADABLE_ICON = /^(\/(?![/\\])\S*|data:image\/[a-z0-9.+-]+[;,]\S*)$/i;

// The text of a sentence that may carry markup of its own, for a text node.
function plain(text) {
  return String(text).replace(/<[^>]*>/g, "");
}

// The draft and every action on it. `deps` are the page's browser parts, so the store runs
// under `node --test` with stand-ins.
export function createTilesStore(deps) {
  const {
    texts,
    doc,
    fetchImpl,
    htmx,
    showToast,
    authGuard,
    confirm,
    csrf,
    clock = globalThis,
    Sortable = null,
  } = deps;

  let sortables = [];

  function headers() {
    return {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrf(),
    };
  }

  // Messages carry operator-typed group and tile names back from the server, and the toast
  // writes markup. Escape before handing anything over.
  async function detail(response) {
    try {
      const body = await response.json();
      if (Array.isArray(body.detail) && body.detail.length) {
        const first = body.detail[0];
        const where = first.tile === null || first.tile === undefined
          ? fmt(texts.whereGroup, { group: first.group + 1 })
          : fmt(texts.whereTile, { group: first.group + 1, tile: first.tile + 1 });
        return first.message + " (" + where + ")";
      }
      return body.detail || JSON.stringify(body);
    } catch (_) {
      return "HTTP " + response.status;
    }
  }

  return {
    revision: "",
    linkKeys: [],
    widgetTypes: [],
    widgetIcons: {},
    defaultIcons: {},
    groups: [],
    dirty: 0,
    preview: false,
    busy: false,
    dialog: null,
    widgetDialog: null,
    removed: null,
    raw: null,
    _seq: 0,
    _resolveTimer: null,
    _previewTimer: null,
    _removedTimer: null,

    // ── Switching between gallery and editor ───────────────────────────────

    enterEdit() {
      htmx.ajax("GET", "/partials/tiles/edit", { target: "#tile-gallery", swap: "innerHTML" });
    },

    exitEdit() {
      htmx.ajax("GET", "/partials/tiles", { target: "#tile-gallery", swap: "innerHTML" });
    },

    togglePreview() {
      this.preview = !this.preview;
    },

    // ── Draft lifecycle ────────────────────────────────────────────────────

    load(draft, revision, linkKeys, widgetTypes, widgetIcons, defaultIcons) {
      this._seq = 0;
      this.widgetTypes = widgetTypes || [];
      this.widgetIcons = widgetIcons || {};
      this.defaultIcons = defaultIcons || {};
      this.groups = (draft.groups || []).map((group) => ({
        _id: this._id(),
        name: group.name,
        tiles: (group.tiles || []).map((tile) => this._tile(tile)),
      }));
      this.revision = revision;
      this.linkKeys = linkKeys || [];
      this.dirty = 0;
      this.preview = false;
      this.dialog = null;
      this.widgetDialog = null;
      this.removed = null;
      this.raw = null;
    },

    // Client-side identity, never sent back. x-for needs a stable key to reuse nodes across
    // a reorder, and names are not unique.
    _id() {
      this._seq += 1;
      return this._seq;
    },

    _tile(tile) {
      if (tile.type === "widget") return this._widgetTile(tile);
      return {
        _id: this._id(),
        name: tile.name || "",
        href: tile.href || "",
        description: tile.description || "",
        icon: tile.icon || "",
        visibility: tile.visibility === "admin" ? "admin" : "all",
      };
    },

    // A widget tile in the draft. `id` stays empty until the server has saved the widget
    // once; it is what the dashboard's request for this one card names. Unlike a link, an
    // unset visibility is for administrators.
    _widgetTile(tile) {
      return {
        _id: this._id(),
        type: "widget",
        id: tile.id || "",
        widget: tile.widget || "",
        title: tile.title || "",
        size: tile.size === "m" || tile.size === "l" ? tile.size : "s",
        visibility: tile.visibility === "all" ? "all" : "admin",
      };
    },

    touch() {
      this.dirty += 1;
    },

    tileCount() {
      return this.groups.reduce((n, group) => n + group.tiles.length, 0);
    },

    // ── Widget types ───────────────────────────────────────────────────────

    widgetType(id) {
      return this.widgetTypes.find((type) => type.id === id) || null;
    },

    // What a widget card is called: its own title, else its type's name, else the raw id of
    // a type that is not registered (a hand-edited file).
    widgetName(tile) {
      const type = this.widgetType(tile.widget);
      return tile.title || (type ? type.name : tile.widget);
    },

    widgetIconPath(name) {
      return this.widgetIcons[name] || this.widgetIcons.alert || "";
    },

    // The icon path of the type a widget tile shows.
    widgetTileIcon(tile) {
      const type = this.widgetType(tile.widget);
      return this.widgetIconPath(type ? type.icon : undefined);
    },

    // The description a widget card shows, or the sentence for a type that does not exist.
    widgetDescription(tile) {
      const type = this.widgetType(tile.widget);
      return (type && type.description) || texts.noWidget;
    },

    // The image a link tile shows: its own icon, else the one bundled for a default
    // application of that name. The dashboard applies the same rule on the server. An own
    // icon counts only where the enforced policy lets the page load it (img-src 'self'
    // data:), a path on this site or a data image; anything else would be blocked, so the
    // bundled icon or the badge stands in, and the dialog names the problem.
    iconFor(tile) {
      const name = (tile.name || "").trim().toLowerCase();
      const own = tile.icon && LOADABLE_ICON.test(tile.icon) ? tile.icon : "";
      return own || this.defaultIcons[name] || "";
    },

    // The letter of the badge a tile without an icon shows.
    initial(name) {
      return (name || "?").charAt(0);
    },

    // "3 applications · 2 widgets", counting each kind in its own words.
    groupCount(group) {
      const widgets = group.tiles.filter((tile) => tile.type === "widget").length;
      const links = group.tiles.length - widgets;
      const parts = [];
      if (links || !widgets) parts.push(fmtPlural(links, texts.applicationOne, texts.applicationOther));
      if (widgets) parts.push(fmtPlural(widgets, texts.widgetOne, texts.widgetOther));
      return parts.join(" · ");
    },

    // Mirrors visible_groups: a group with nothing a plain user may see is not rendered for
    // them either. Presentation only -- the real filtering happens server-side, in the
    // gallery this preview stands in for.
    groupVisible(group) {
      if (!this.preview) return true;
      return group.tiles.some((tile) => tile.visibility === "all");
    },

    tileVisible(tile) {
      return !this.preview || tile.visibility === "all";
    },

    // The classes of a card: clickable while editing, and as wide as the widget will be on
    // the dashboard. Spelled out in full because app.css is built by scanning the sources.
    cardClass(tile) {
      const classes = [];
      if (!this.preview) classes.push("cursor-pointer hover:border-base-content/25 transition-colors");
      if (tile.type === "widget" && tile.size === "m") classes.push("sm:col-span-2");
      if (tile.type === "widget" && tile.size === "l") classes.push("sm:col-span-2 xl:col-span-3 2xl:col-span-4");
      return classes.join(" ");
    },

    // ── Labels with values ─────────────────────────────────────────────────

    addLinkLabel(group) {
      return fmt(texts.addLinkTo, { name: group.name });
    },

    undoLabel() {
      return fmt(texts.undoRemoved, { tiles: this.removedName() });
    },

    refreshLabel() {
      const dialog = this.widgetDialog;
      return fmt(texts.refreshesEvery, { seconds: dialog && dialog.type ? dialog.type.refresh : "" });
    },

    // "<b>3</b> unsaved changes", built as nodes: the count is the only value, and the bold
    // mark around it is the sentence's own markup.
    showDirty(element) {
      const chosen = fmtPlural(this.dirty, "one", "other") === "one" ? texts.unsavedOne : texts.unsavedOther;
      const at = chosen.indexOf(COUNT_MARK);
      const nodes = [];
      if (at < 0) {
        nodes.push(doc.createTextNode(plain(fmt(chosen, { count: this.dirty }))));
      } else {
        const count = doc.createElement("span");
        count.className = "font-semibold";
        count.textContent = String(this.dirty);
        nodes.push(doc.createTextNode(plain(chosen.slice(0, at))));
        nodes.push(count);
        nodes.push(doc.createTextNode(plain(chosen.slice(at + COUNT_MARK.length))));
      }
      element.replaceChildren(...nodes);
    },

    // ── Groups ─────────────────────────────────────────────────────────────

    addGroup() {
      this.groups.push({ _id: this._id(), name: texts.newGroup, tiles: [] });
      this.touch();
    },

    renameGroup(group, name) {
      group.name = name;
      this.touch();
    },

    deleteGroup(gi) {
      this.groups.splice(gi, 1);
      this.touch();
    },

    // Asks first: the group goes with every tile in it.
    confirmDeleteGroup(gi) {
      const count = this.groups[gi].tiles.length;
      if (confirm(fmtPlural(count, texts.deleteGroupOne, texts.deleteGroupOther))) this.deleteGroup(gi);
    },

    moveGroup(gi, delta) {
      const target = gi + delta;
      if (target < 0 || target >= this.groups.length) return;
      const [group] = this.groups.splice(gi, 1);
      this.groups.splice(target, 0, group);
      this.touch();
    },

    // ── Tiles ──────────────────────────────────────────────────────────────

    // Removing is undoable for a few seconds rather than confirmed: nothing is written before
    // "Save changes", and a confirm dialog for a draft edit is more in the way than the undo
    // strip is.
    removeTile(gi, ti) {
      const group = this.groups[gi];
      const [tile] = group.tiles.splice(ti, 1);
      this.removed = { groupId: group._id, ti: ti, tile: tile };
      clock.clearTimeout(this._removedTimer);
      this._removedTimer = clock.setTimeout(() => { this.removed = null; }, 8000);
      this.touch();
    },

    removedName() {
      const tile = this.removed && this.removed.tile;
      if (!tile) return "";
      return tile.type === "widget" ? this.widgetName(tile) : (tile.name || "tile");
    },

    undoRemove() {
      const removed = this.removed;
      if (!removed) return;
      clock.clearTimeout(this._removedTimer);
      this.removed = null;
      const group = this.groups.find((g) => g._id === removed.groupId);
      if (!group) return;
      group.tiles.splice(Math.min(removed.ti, group.tiles.length), 0, removed.tile);
      this.dirty = Math.max(0, this.dirty - 1);
    },

    duplicateTile(gi, ti) {
      const source = this.groups[gi].tiles[ti];
      const copy = this._tile(source);
      if (copy.type === "widget") {
        // A new widget: the server gives it an id of its own on save.
        copy.id = "";
        copy.title = fmt(texts.nameCopy, { name: this.widgetName(source) });
      } else {
        copy.name = fmt(texts.nameCopy, { name: copy.name });
      }
      this.groups[gi].tiles.splice(ti + 1, 0, copy);
      this.touch();
    },

    moveTile(gi, ti, delta) {
      const tiles = this.groups[gi].tiles;
      const target = ti + delta;
      if (target < 0 || target >= tiles.length) return;
      const [tile] = tiles.splice(ti, 1);
      tiles.splice(target, 0, tile);
      this.touch();
    },

    moveTileToGroup(gi, ti, target) {
      if (target === gi) return;
      const [tile] = this.groups[gi].tiles.splice(ti, 1);
      this.groups[target].tiles.push(tile);
      this.touch();
    },

    // Sortable owns the DOM for the length of a drag; the store catches up afterwards by
    // reading the order back. Reading ids rather than reconstructing the move keeps one code
    // path for reorders inside a group and moves between groups.
    syncOrderFromDom() {
      const byId = new Map();
      this.groups.forEach((group) => {
        group.tiles.forEach((tile) => byId.set(String(tile._id), tile));
      });

      const nextTiles = [];
      doc.querySelectorAll("[data-tile-list]").forEach((list) => {
        nextTiles.push([
          list.dataset.listGroup,
          Array.from(list.querySelectorAll(':scope > [data-tile-id]'))
            .map((node) => byId.get(node.dataset.tileId))
            .filter(Boolean),
        ]);
      });

      const groupOrder = Array.from(doc.querySelectorAll("[data-group-id]"))
        .map((node) => node.dataset.groupId);

      const groupsById = new Map(this.groups.map((group) => [String(group._id), group]));
      nextTiles.forEach(([id, tiles]) => {
        const group = groupsById.get(id);
        if (group) group.tiles = tiles;
      });
      const reordered = groupOrder.map((id) => groupsById.get(id)).filter(Boolean);
      if (reordered.length === this.groups.length) this.groups = reordered;

      this.touch();
    },

    // Drag and drop is an enhancement, not the mechanism: every reorder is also a menu
    // command, and a missing Sortable degrades to the menu instead of breaking the page.
    // Re-created rather than updated: a group added or removed changes which containers
    // exist, and destroying first keeps a stale instance from holding on to a detached list.
    initSortable() {
      sortables.forEach((s) => s.destroy());
      sortables = [];

      if (!Sortable) return;
      if (this.preview) return;

      doc.querySelectorAll("[data-tile-list]").forEach((list) => {
        sortables.push(new Sortable(list, {
          group: "papaia-tiles",
          draggable: "[data-tile-id]",
          handle: "[data-tile-handle]",
          animation: 150,
          onEnd: () => this.syncOrderFromDom(),
        }));
      });

      const groups = doc.querySelector("[data-group-list]");
      if (groups) {
        sortables.push(new Sortable(groups, {
          draggable: "[data-group-id]",
          handle: "[data-group-handle]",
          animation: 150,
          onEnd: () => this.syncOrderFromDom(),
        }));
      }
    },

    // ── Tile dialog ────────────────────────────────────────────────────────

    // The native dialog is opened and closed by id, the way catalogs.html drives its modals.
    // Binding it to an Alpine effect instead looks tidier and is a trap: an effect reading
    // `$refs` for its own element runs before the ref exists, and the error it throws inside
    // Alpine leaves `x-show` dead for the rest of the page.
    _openModal(id) {
      const el = doc.getElementById(id);
      if (el && !el.open) el.showModal();
    },

    _closeModal(id) {
      const el = doc.getElementById(id);
      if (el && el.open) el.close();
    },

    // A click on a card opens it, except in the preview.
    clickTile(gi, ti) {
      if (!this.preview) this.openTile(gi, ti);
    },

    newTile(gi) {
      this.dialog = {
        gi: gi,
        ti: null,
        tile: this._tile({ visibility: "all" }),
        resolved: "",
        problem: null,
        iconProblem: null,
        nameProblem: null,
      };
      this._openModal("tile-editor-modal");
    },

    openTile(gi, ti) {
      if (this.groups[gi].tiles[ti].type === "widget") {
        this.openWidget(gi, ti);
        return;
      }
      this.dialog = {
        gi: gi,
        ti: ti,
        tile: Object.assign({}, this.groups[gi].tiles[ti]),
        resolved: "",
        problem: null,
        iconProblem: null,
        nameProblem: null,
      };
      this._openModal("tile-editor-modal");
      this.resolveDialog();
    },

    closeTile() {
      this.dialog = null;
      this._closeModal("tile-editor-modal");
    },

    // The dialog was closed by Escape or the backdrop, which bypass the store.
    tileDialogClosed() {
      this.dialog = null;
    },

    setTileVisibility(visibility) {
      this.dialog.tile.visibility = visibility;
    },

    insertPlaceholder(key) {
      const open = "{" + "{";
      this.dialog.tile.href = open + key + "}" + "}" + this.dialog.tile.href;
      this.resolveDialog();
    },

    applyTile() {
      const dialog = this.dialog;
      if (!dialog) return;
      if (!dialog.tile.name.trim()) {
        dialog.nameProblem = texts.nameMissing;
        return;
      }
      if (dialog.problem) return;

      const tile = Object.assign({}, dialog.tile);
      if (dialog.ti === null) {
        this.groups[dialog.gi].tiles.push(tile);
      } else {
        this.groups[dialog.gi].tiles.splice(dialog.ti, 1, tile);
      }
      this.dialog = null;
      this._closeModal("tile-editor-modal");
      this.touch();
    },

    // ── Widget dialog ──────────────────────────────────────────────────────

    // One dialog, two steps: pick a type from the gallery, then place it. Editing a widget
    // that is already on the dashboard opens at the second step.
    newWidget(gi) {
      this.widgetDialog = {
        gi: gi,
        ti: null,
        step: "pick",
        query: "",
        type: null,
        tile: null,
        problem: null,
      };
      this._openModal("widget-modal");
    },

    openWidget(gi, ti) {
      const tile = Object.assign({}, this.groups[gi].tiles[ti]);
      this.widgetDialog = {
        gi: gi,
        ti: ti,
        step: "configure",
        query: "",
        type: this.widgetType(tile.widget),
        tile: tile,
        problem: null,
      };
      this._openModal("widget-modal");
      this.previewWidget();
    },

    widgetChoices() {
      const dialog = this.widgetDialog;
      if (!dialog) return [];
      const query = dialog.query.trim().toLowerCase();
      return this.widgetTypes.filter((type) =>
        !query || (type.name + " " + type.description).toLowerCase().includes(query)
      );
    },

    // A type the installation cannot use is listed with its reason and cannot be picked.
    pickWidget(type) {
      const dialog = this.widgetDialog;
      if (!dialog || !type.available) return;
      dialog.type = type;
      dialog.tile = this._widgetTile({ widget: type.id, size: type.size, visibility: type.visibility });
      dialog.problem = null;
      dialog.step = "configure";
      this.previewWidget();
    },

    backToGallery() {
      const dialog = this.widgetDialog;
      if (!dialog) return;
      dialog.step = "pick";
      dialog.type = null;
      dialog.tile = null;
      dialog.problem = null;
    },

    closeWidget() {
      clock.clearTimeout(this._previewTimer);
      this.widgetDialog = null;
      this._closeModal("widget-modal");
    },

    // The dialog was closed by Escape or the backdrop, which bypass the store.
    widgetDialogClosed() {
      this.widgetDialog = null;
    },

    setWidgetSize(size) {
      this.widgetDialog.tile.size = size;
    },

    setWidgetVisibility(visibility) {
      this.widgetDialog.tile.visibility = visibility;
    },

    applyWidget() {
      const dialog = this.widgetDialog;
      if (!dialog || !dialog.tile) return;
      if (!dialog.type) {
        dialog.problem = texts.widgetGone;
        return;
      }
      if (dialog.tile.title.length > 80) {
        dialog.problem = texts.titleTooLong;
        return;
      }
      const tile = Object.assign({}, dialog.tile, { title: dialog.tile.title.trim() });
      if (dialog.ti === null) {
        this.groups[dialog.gi].tiles.push(tile);
      } else {
        this.groups[dialog.gi].tiles.splice(dialog.ti, 1, tile);
      }
      this.closeWidget();
      this.touch();
    },

    // The preview is the real card: the server renders it the way the dashboard does, from
    // live data, and it comes back escaped like any other partial.
    previewWidget() {
      clock.clearTimeout(this._previewTimer);
      this._previewTimer = clock.setTimeout(() => {
        const dialog = this.widgetDialog;
        if (!dialog || !dialog.type || !dialog.tile) return;
        if (!doc.getElementById("widget-preview")) return;
        const title = (dialog.tile.title || "").trim();
        const query = title ? "?title=" + encodeURIComponent(title) : "";
        htmx.ajax(
          "GET",
          "/partials/widgets/preview/" + encodeURIComponent(dialog.type.id) + query,
          { target: "#widget-preview", swap: "innerHTML" }
        );
      }, 200);
    },

    // Resolution runs on the server: the values live in the core .env, which holds every
    // stack secret, so only the finished string comes back.
    resolveDialog() {
      clock.clearTimeout(this._resolveTimer);
      this._resolveTimer = clock.setTimeout(() => this._resolveNow(), 250);
    },

    async _resolveNow() {
      const dialog = this.dialog;
      if (!dialog) return;
      dialog.nameProblem = null;

      const response = await fetchImpl("/api/v1/tiles/resolve", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ values: [dialog.tile.href], icons: [dialog.tile.icon || ""] }),
      });
      if (authGuard(response)) return;
      if (!response.ok) return;
      const results = await response.json();
      if (this.dialog !== dialog) return;

      dialog.resolved = results[0].resolved;
      dialog.problem = results[0].ok ? null : results[0].reason;
      dialog.iconProblem = dialog.tile.icon && !results[1].ok ? results[1].reason : null;
    },

    // ── Raw YAML ───────────────────────────────────────────────────────────

    async openRaw() {
      const response = await fetchImpl("/api/v1/tiles/raw", { headers: headers() });
      if (authGuard(response)) return;
      if (!response.ok) {
        showToast("error", texts.rawReadFailed + " " + escapeHtml(await detail(response)));
        return;
      }
      const data = await response.json();
      this.raw = { text: data.yaml, revision: data.revision, problem: null };
      this._openModal("tiles-raw-modal");
    },

    closeRaw() {
      this.raw = null;
      this._closeModal("tiles-raw-modal");
    },

    // The dialog was closed by Escape or the backdrop, which bypass the store.
    rawDialogClosed() {
      this.raw = null;
    },

    async applyRaw() {
      if (this.busy) return;
      this.busy = true;
      try {
        const response = await fetchImpl("/api/v1/tiles/raw", {
          method: "PUT",
          headers: headers(),
          body: JSON.stringify({ revision: this.raw.revision, yaml: this.raw.text }),
        });
        if (authGuard(response)) return;
        if (!response.ok) {
          this.raw.problem = await detail(response);
          return;
        }
        this.raw = null;
        this._closeModal("tiles-raw-modal");
        showToast("success", texts.rawSaved);
        this.enterEdit();
      } finally {
        this.busy = false;
      }
    },

    // ── Persistence ────────────────────────────────────────────────────────

    async save() {
      if (this.busy) return;
      this.busy = true;
      try {
        const response = await fetchImpl("/api/v1/tiles", {
          method: "PUT",
          headers: headers(),
          body: JSON.stringify({
            revision: this.revision,
            version: 1,
            groups: this.groups.map((group) => ({
              name: group.name,
              tiles: group.tiles.map((tile) => tile.type === "widget" ? ({
                type: "widget",
                id: tile.id,
                widget: tile.widget,
                title: tile.title,
                size: tile.size,
                visibility: tile.visibility,
              }) : ({
                name: tile.name,
                href: tile.href,
                description: tile.description,
                icon: tile.icon || null,
                visibility: tile.visibility,
              })),
            })),
          }),
        });

        if (authGuard(response)) return;
        if (response.status === 409) {
          showToast("error", texts.saveConflict);
          return;
        }
        if (!response.ok) {
          showToast("error", texts.saveFailed + " " + escapeHtml(await detail(response)));
          return;
        }

        const data = await response.json();
        this.revision = data.revision;
        this.dirty = 0;
        showToast("success", texts.saved);
        this.exitEdit();
      } finally {
        this.busy = false;
      }
    },

    discard() {
      if (this.dirty && !confirm(texts.discardConfirm)) return;
      this.enterEdit();
    },

    leave() {
      if (this.dirty && !confirm(texts.leaveConfirm)) return;
      this.exitEdit();
    },
  };
}

// The editor's root (partials/tile_editor.html): hands the draft of the island to the store
// as the partial comes in, so the editor comes up populated instead of blank, and re-creates
// drag and drop whenever the groups or the preview change.
export function createTileEditor({ doc }) {
  return {
    init() {
      const island = readIsland(EDITOR_ISLAND, doc);
      this.$store.tiles.load(
        island.draft,
        island.revision,
        island.linkKeys,
        island.widgetTypes,
        island.widgetIcons,
        island.defaultIcons,
      );
    },

    // An x-effect: reading the two values makes Alpine run it again when either changes.
    layout() {
      const store = this.$store.tiles;
      void store.groups.length;
      void store.preview;
      this.$nextTick(() => store.initSortable());
    },
  };
}

export function installDashboard(win, doc) {
  doc.addEventListener("alpine:init", () => {
    const { actions, toaster } = pageCore();
    win.Alpine.store("tiles", createTilesStore({
      texts: readIsland(TEXTS_ISLAND, doc),
      doc,
      fetchImpl: (url, options) => win.fetch(url, options),
      htmx: win.htmx,
      showToast: (type, html) => toaster.show(type, html),
      authGuard: actions.authGuard,
      confirm: (message) => win.confirm(message),
      csrf: () => csrfToken(doc),
      clock: win,
      Sortable: win.Sortable || null,
    }));
    win.Alpine.data("tileEditor", () => createTileEditor({ doc }));
  });
}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  installDashboard(window, document);
}
