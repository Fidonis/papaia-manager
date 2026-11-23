// Embedding (embedding.html with partials/embedding_body.html, embedding_tree.html,
// embedding_uploads.html and embedding_status.html).
//
// One Alpine component, `embedPage`, holds the selection in the trees, the chosen upload and
// the options for the whole page, outside everything htmx swaps. The body tells it what it
// loaded through its data island `embed-state` (`loaded()` from the body's first element); the
// page's sentences come from the island `embed-texts`. The run strip announces whether its
// run works as the window event `papaia-embed-run` (core/announce.js), which the component
// takes in `runState()`.
//
// Loaded after core/main.js and before Alpine (base.html, block `page_modules`), on the CSP
// build of Alpine: the templates call plain methods of the component, and no server value
// reaches this file as script text.
import { ragDetail } from "../core/actions.js";
import { csrfToken } from "../core/csrf.js";
import { escapeHtml, fmt, fmtPlural } from "../core/format.js";
import { readIsland } from "../core/islands.js";
import { fillMarked } from "../core/marked.js";
import { pageCore } from "../core/page.js";

export const TEXTS_ISLAND = "embed-texts";
export const STATE_ISLAND = "embed-state";

// The connection on screen, from the page's selector.
export function embedConnection(doc) {
  const select = doc.getElementById("embed-connection");
  return select ? select.value : "default";
}

export function embedUrl(doc, path, extra) {
  return path + "?connection=" + encodeURIComponent(embedConnection(doc)) + (extra || "");
}

// Reload the body of the page, for the connection on screen. A collection name picks that
// collection; without one the first is shown.
export function reloadEmbedding(doc, htmx, collection) {
  const extra = collection ? "&collection=" + encodeURIComponent(collection) : "";
  htmx.ajax("GET", embedUrl(doc, "/partials/embedding", extra), { target: "#embed-body", swap: "innerHTML" });
}

// htmx does not swap a 4xx answer. Say what happened instead of leaving a stale body.
export function showBodyError(detail, texts) {
  if (!detail.target || detail.target.id !== "embed-body") return false;
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
    '<a class="link" href="/embedding">' + escapeHtml(texts.reloadPage) + "</a></div>";
  return true;
}

// The component of the page. `deps` are the page's browser parts, so it runs under
// `node --test` with stand-ins.
export function createEmbedPage(deps) {
  const { texts, doc, htmx, ragRequest, ragError, authGuard, showToast, csrf, fetchImpl, readState } = deps;

  return {
    connection: "default",
    collection: "", points: null, recorded: "", model: "",
    supportsAdd: true, ready: false, otherJobs: [],
    tab: "upload", mode: "add",
    batch: "", selected: {},
    uploading: false, progress: "", uploadErrors: [],
    error: "", busy: false, typed: "", confirmOthers: false,
    runId: "", runActive: false, aborting: false,
    discardItem: null, discardBusy: false, discardError: "",

    init() {
      this.connection = embedConnection(doc);
    },

    // The body was (re)loaded: what it says about the collection replaces what was here. The
    // body's first element calls this without arguments; the state is its island.
    loaded(state) {
      const d = state === undefined ? readState() : state;
      this.connection = embedConnection(doc);
      this.collection = d.collection;
      this.points = d.points;
      this.recorded = d.model || "";
      this.model = d.model || "";
      this.supportsAdd = d.supportsAdd;
      this.ready = d.ready;
      this.otherJobs = d.otherJobs;
      this.runId = d.activeRun || "";
      this.runActive = !!d.activeRun;
      this.aborting = false;
      if (!this.supportsAdd && this.mode === "add") this.mode = "replace";
      this.batch = "";
      this.selected = {};
      this.error = "";
      this.uploadErrors = [];
    },

    // The collection chooser of the body.
    pickCollection(name) {
      reloadEmbedding(doc, htmx, name);
    },

    // The sentences with markup of their own (x-effect: they follow the values they read).
    markReplaceTitle(el) {
      fillMarked(el, texts.replaceTitle, { collection: this.collection });
    },
    markPoints(el) {
      fillMarked(el, texts.points, { points: this.points });
    },
    markDiscard(el) {
      const item = this.discardItem;
      fillMarked(el, texts.discardWhat, {
        name: item && item.name,
        owner: item && item.owner,
        files: item && item.files,
      });
    },
    otherJobList() {
      return this.otherJobs.join(", ");
    },

    // ── what is selected: paths relative to the tree on screen, '' being its root ──
    paths() {
      const picked = this.selected;
      return Object.keys(picked).filter(function (key) { return picked[key]; });
    },
    isOn(path) { return !!this.selected[path]; },
    // Below a selected folder: shown as selected, and changed by changing the folder.
    covered(path) {
      if (path === "") return false;
      if (this.selected[""]) return true;
      const parts = path.split("/");
      for (let i = 1; i < parts.length; i++) {
        if (this.selected[parts.slice(0, i).join("/")]) return true;
      }
      return false;
    },
    toggle(path, on) {
      const next = Object.assign({}, this.selected);
      if (on) {
        const prefix = path === "" ? "" : path + "/";
        Object.keys(next).forEach(function (key) {
          if (key !== path && (path === "" || key.indexOf(prefix) === 0)) delete next[key];
        });
        next[path] = true;
      } else {
        delete next[path];
      }
      this.selected = next;
    },
    switchTab(tab) {
      this.tab = tab;
      this.selected = {};
      this.batch = "";
      this.error = "";
    },
    source() {
      if (this.tab === "upload") return { kind: "upload", batch: this.batch, paths: this.paths() };
      return { kind: "folder", paths: this.paths() };
    },
    canStart() {
      if (this.busy || this.uploading || this.runActive || !this.ready || !this.collection) return false;
      if (this.mode === "add" && !this.supportsAdd) return false;
      if (!this.model.trim()) return false;
      if (this.tab === "upload" && !this.batch) return false;
      return this.paths().length > 0;
    },

    // ── the upload area ─────────────────────────────────────────────────────
    refreshUploads() {
      htmx.ajax("GET", "/partials/embedding/uploads", { target: "#embed-uploads", swap: "outerHTML" });
    },
    loadBatchTree() {
      const target = doc.getElementById("embed-batch-tree");
      if (!target) return;
      if (!this.batch) { target.innerHTML = ""; return; }
      htmx.ajax("GET", "/partials/embedding/tree?source=upload&upload=" + encodeURIComponent(this.batch),
                { target: "#embed-batch-tree", swap: "innerHTML" });
    },
    pickBatch(id) {
      this.batch = id;
      this.selected = { "": true };
      this.loadBatchTree();
    },
    newUpload() {
      this.batch = "";
      this.selected = {};
      this.loadBatchTree();
    },
    // The file and folder pickers: take what was picked, then clear the picker so the same
    // files can be picked again.
    uploadPicked(input) {
      const files = Array.from(input.files || []);
      input.value = "";
      return this.uploadFiles(files);
    },
    async uploadFiles(list) {
      const files = Array.from(list || []);
      if (!files.length) return;
      this.uploadErrors = [];
      this.uploading = true;
      try {
        if (!this.batch) {
          const made = await ragRequest("POST", "/api/v1/rag/ingest/uploads", { name: "" });
          if (made.handled) return;
          if (!made.ok) { this.uploadErrors.push(made.error); return; }
          this.batch = made.data.id;
          this.selected = { "": true };
        }
        const url = "/api/v1/rag/ingest/uploads/" + encodeURIComponent(this.batch) + "/files";
        const token = csrf();
        let done = 0;
        for (const file of files) {
          const path = file.webkitRelativePath || file.name;
          this.progress = fmt(texts.uploading, { current: done + 1, total: files.length });
          const form = new FormData();
          form.append("file", file);
          form.append("path", path);
          let response;
          try {
            response = await fetchImpl(url, { method: "POST", headers: { "X-CSRF-Token": token }, body: form });
          } catch {
            this.uploadErrors.push(fmt(texts.interrupted, { path: path }));
            break;
          }
          if (authGuard(response)) return;
          if (!response.ok) this.uploadErrors.push(path + ": " + await ragError(response));
          done++;
        }
      } finally {
        this.uploading = false;
        this.progress = "";
        this.refreshUploads();
        this.loadBatchTree();
      }
    },
    // Discarding asks first, in a dialog of the page. `item` is what the list knows about the
    // upload: id, name, owner, files and size.
    askDiscard(item) {
      this.discardItem = item;
      this.discardError = "";
      this.$refs.discardDialog.showModal();
    },
    closeDiscard() {
      this.$refs.discardDialog.close();
    },
    async confirmDiscard() {
      if (!this.discardItem || this.discardBusy) return;
      const id = this.discardItem.id;
      this.discardBusy = true;
      this.discardError = "";
      const res = await ragRequest("DELETE", "/api/v1/rag/ingest/uploads/" + encodeURIComponent(id));
      this.discardBusy = false;
      if (res.handled) return;
      if (!res.ok) { this.discardError = res.error; return; }
      this.$refs.discardDialog.close();
      showToast("success", texts.discarded);
      if (this.batch === id) this.newUpload();
      this.refreshUploads();
    },

    // ── the run ─────────────────────────────────────────────────────────────
    closeConfirm() {
      this.$refs.confirmDialog.close();
    },
    async start() {
      this.error = "";
      if (this.mode === "replace") {
        this.typed = "";
        this.confirmOthers = false;
        this.$refs.confirmDialog.showModal();
        return;
      }
      await this.submit();
    },
    async submit() {
      this.busy = true;
      this.error = "";
      const res = await ragRequest(
        "POST", "/api/v1/rag/ingest/runs?connection=" + encodeURIComponent(this.connection), {
          collection: this.collection,
          mode: this.mode,
          model: this.model.trim() || null,
          source: this.source(),
          confirm_replace: this.mode === "replace",
          confirm_other_jobs: this.confirmOthers,
        });
      this.busy = false;
      if (res.handled) return;
      if (this.$refs.confirmDialog.open) this.$refs.confirmDialog.close();
      if (!res.ok) { this.error = res.error; return; }
      this.runId = res.data.run_id;
      this.runActive = true;
      this.aborting = false;
      showToast("success", fmtPlural(res.data.files, texts.startedOne, texts.startedOther, null, true));
      this.showStatus();
      this.refreshUploads();
    },
    showStatus() {
      htmx.ajax("GET", embedUrl(doc, "/partials/embedding/status",
                                "&collection=" + encodeURIComponent(this.collection) +
                                "&run=" + encodeURIComponent(this.runId)),
                { target: "#embed-status", swap: "outerHTML" });
    },
    // The ingester aborts between documents: the one it is working on is finished first,
    // and a run on its last document ends as an ordinary success. Say so, because the strip
    // keeps showing "Running" for a while after the click.
    async abort() {
      if (!this.runId || this.aborting) return;
      const res = await ragRequest("DELETE", "/api/v1/rag/ingest/runs/" + encodeURIComponent(this.runId) +
                                    "?connection=" + encodeURIComponent(this.connection));
      if (res.handled) return;
      if (!res.ok) { showToast("error", texts.abortFailed + " " + escapeHtml(res.error)); return; }
      this.aborting = true;
      showToast("info", texts.abortRequested);
      this.showStatus();
    },
    // The status strip tells when the run ends; the upload it read is gone by then.
    runState(detail) {
      if (detail.active) { this.runActive = true; return; }
      if (!this.runActive) return;
      this.runActive = false;
      this.aborting = false;
      this.newUpload();
      this.refreshUploads();
    },
  };
}

// The page's listeners outside the component: the header's Refresh, the connection selector
// and a body that could not be loaded.
export function installEmbeddingListeners(win, doc, texts) {
  doc.addEventListener("click", (event) => {
    const button = event.target.closest && event.target.closest("[data-embed-reload]");
    if (button) reloadEmbedding(doc, win.htmx);
  });
  doc.addEventListener("submit", (event) => {
    if (event.target && event.target.id === "embed-connection-form") event.preventDefault();
  });
  doc.addEventListener("change", (event) => {
    const select = event.target;
    if (!select || select.id !== "embed-connection") return;
    win.history.replaceState(null, "", "/embedding?connection=" + encodeURIComponent(select.value));
    reloadEmbedding(doc, win.htmx);
  });
  doc.addEventListener("htmx:responseError", (event) => {
    showBodyError(event.detail || {}, texts);
  });
}

export function installEmbedding(win, doc) {
  const { actions, toaster } = pageCore(doc, win);
  const texts = readIsland(TEXTS_ISLAND, doc);
  installEmbeddingListeners(win, doc, texts);
  doc.addEventListener("alpine:init", () => {
    win.Alpine.data("embedPage", () => createEmbedPage({
      texts,
      doc,
      htmx: win.htmx,
      ragRequest: actions.ragRequest,
      ragError: actions.ragError,
      authGuard: actions.authGuard,
      showToast: (type, html) => toaster.show(type, html),
      csrf: () => csrfToken(doc),
      fetchImpl: (url, options) => win.fetch(url, options),
      readState: () => readIsland(STATE_ISLAND, doc),
    }));
  });
}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  installEmbedding(window, document);
}
