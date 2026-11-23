// The release upgrade (upgrade.html and the partials htmx swaps into it, T-UI-03): the header
// cluster as the Alpine component `upgradeActions`, and the page script the partials call
// through `data-upgrade-action` attributes and change listeners instead of inline handlers.
// Everything a browser global used to give is injected, so the module runs under node --test.
import { csrfToken } from "../../core/csrf.js";
import { escapeHtml, fmt, fmtPlural } from "../../core/format.js";

// A sentence from the catalogue as DOM nodes. The only markup the sentences of this page carry
// is `<span class="font-mono">…</span>`; it becomes a span whose text is set with textContent,
// and every value is filled in as text. Anything else in a sentence stays text as well.
const MONO = /<span class="font-mono">([\s\S]*?)<\/span>/g;

export function fillSentence(element, template, values) {
  const doc = element.ownerDocument;
  const source = String(template || "");
  const nodes = [];
  let last = 0;
  for (const match of source.matchAll(MONO)) {
    if (match.index > last) nodes.push(doc.createTextNode(fmt(source.slice(last, match.index), values)));
    const span = doc.createElement("span");
    span.className = "font-mono";
    span.textContent = fmt(match[1], values);
    nodes.push(span);
    last = match.index + match[0].length;
  }
  if (last < source.length) nodes.push(doc.createTextNode(fmt(source.slice(last), values)));
  element.replaceChildren(...nodes);
}

// The page script: the requests of the page and the dialog of the image cleanup. `toast`
// takes a tone and escaped markup (the toaster of core/runtime.js).
export function createUpgradePage(T, { doc, fetchImpl, htmx, toast, authGuard, reload }) {
  // The check is not run by any GET: it fetches from the remote and materialises a
  // git worktree of the target tag. So the page asks for one explicitly on load,
  // which is what makes "open the page" mean "look for upgrades".
  function _csrfToken() {
    return csrfToken(doc);
  }

  async function _upgradeError(r) {
    try {
      const e = await r.json();
      return e.detail || JSON.stringify(e);
    } catch (_) {
      return String(r.status);
    }
  }

  // Every message is text: daemon error text and image names come from Docker rather than
  // from this page, and the toaster takes markup.
  function _toast(type, message) {
    toast(type, escapeHtml(message));
  }

  function refreshUpgradeStatus() {
    htmx().ajax('GET', '/partials/upgrade/status',
              { target: '#upgrade-status-slot', swap: 'innerHTML' });
    htmx().ajax('GET', '/partials/upgrade/check',
              { target: '#upgrade-check-slot', swap: 'innerHTML' });
  }

  function refreshUpgradeRunner() {
    htmx().ajax('GET', '/partials/upgrade/runner',
              { target: '#upgrade-runner-slot', swap: 'innerHTML' });
  }

  // runCheck: POST because it reaches the network and writes into .git/worktrees.
  // `version` is null for "the newest release", which is what papaia-ctl resolves
  // on its own.
  function runCheck(version) {
    return fetchImpl('/api/v1/upgrade/check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': _csrfToken() },
      body: JSON.stringify({ version: version }),
    }).then(async function (r) {
      if (authGuard(r)) return false;
      if (!r.ok) {
        _toast('error', T.checkFailedBecause + " " + (await _upgradeError(r)));
        refreshUpgradeStatus();
        return false;
      }
      refreshUpgradeStatus();
      return true;
    }).catch(function () {
      _toast('error', T.checkUnreachable);
      return false;
    });
  }

  function recheckVersion(version) {
    runCheck(version);
  }

  // startUpgrade: 202 and then nothing to poll in this request -- the runner
  // outlives us. #upgrade-runner picks the outcome back up, here and after the
  // reload that follows the manager being recreated.
  function startUpgrade(version, force, noBackup, pruneImages) {
    return fetchImpl('/api/v1/upgrade', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': _csrfToken() },
      body: JSON.stringify({
        version: version, force: force, no_backup: noBackup, prune_images: pruneImages,
      }),
    }).then(async function (r) {
      if (authGuard(r)) return false;
      if (!r.ok) {
        _toast('error', T.refused + " " + (await _upgradeError(r)));
        refreshUpgradeRunner();
        return false;
      }
      refreshUpgradeRunner();
      _toast('info', T.started);
      return true;
    });
  }

  // acknowledgeUpgrade: drop the finished runner so the next upgrade can start.
  // The record of what it did stays in upgrade.log, which is why this is safe to
  // offer as a single click. A run that was started with the image cleanup gets a
  // second pass here, because the runner container itself held the previous
  // panel image until now -- the response says what that pass did.
  function acknowledgeUpgrade() {
    return fetchImpl('/api/v1/upgrade/runner/clear', {
      method: 'POST',
      headers: { 'X-CSRF-Token': _csrfToken() },
    }).then(async function (r) {
      if (authGuard(r)) return;
      if (!r.ok) {
        _toast('error', T.dismissFailed + " " + (await _upgradeError(r)));
        return;
      }
      var data = await r.json();
      if (data.images) {
        if (data.images.error) {
          _toast('warning', T.dismissedImagesFailed + " " + data.images.error);
        } else if (data.images.removed.length) {
          _toast('success', fmtPlural(data.images.removed.length,
                    T.dismissedImagesOne,
                    T.dismissedImagesOther, { size: data.images.reclaimed_human }));
        }
      }
      refreshUpgradeRunner();
      refreshUpgradeStatus();
      refreshUpgradeImages();
      htmx().ajax('GET', '/partials/upgrade/log',
                { target: '#upgrade-log-slot', swap: 'innerHTML' });
    });
  }

  function refreshUpgradeImages() {
    htmx().ajax('GET', '/partials/upgrade/images',
              { target: '#upgrade-images-slot', swap: 'innerHTML' });
  }

  // The rows the operator is looking at, read back from the DOM: what the dialog
  // lists and what the request names are the same set, so a row that became
  // outdated between render and click is not removed on their behalf.
  function _imageRows(onlyChecked) {
    var sel = onlyChecked ? '.prune-image-check:checked' : '.prune-image-check';
    return Array.prototype.map.call(doc.querySelectorAll(sel), function (box) {
      return { id: box.value, name: box.getAttribute('data-name') };
    });
  }

  // Bound to the list's checkboxes: keeps the bulk button and the master box honest.
  function updatePruneSelection() {
    var all = doc.querySelectorAll('.prune-image-check');
    var picked = doc.querySelectorAll('.prune-image-check:checked');
    var button = doc.getElementById('prune-selected-button');
    if (button) {
      button.disabled = picked.length === 0;
      button.textContent = picked.length ? fmt(T.removeSelectedCount, { count: picked.length }) : T.removeSelected;
    }
    var master = doc.getElementById('prune-master-check');
    if (master) {
      master.checked = all.length > 0 && picked.length === all.length;
      master.indeterminate = picked.length > 0 && picked.length < all.length;
    }
  }

  function togglePruneAll(checked) {
    doc.querySelectorAll('.prune-image-check').forEach(function (box) {
      box.checked = checked;
    });
    updatePruneSelection();
  }

  var _pendingPrune = [];

  function openPruneImagesModal(onlyChecked) {
    _pendingPrune = _imageRows(onlyChecked);
    if (_pendingPrune.length === 0) return;
    doc.getElementById('prune-images-title').textContent = fmtPlural(_pendingPrune.length,
      T.removeTitleOne, T.removeTitleOther);
    var list = doc.getElementById('prune-images-list');
    list.textContent = '';
    _pendingPrune.forEach(function (row) {
      var li = doc.createElement('li');
      li.textContent = row.name;
      list.appendChild(li);
    });
    doc.getElementById('prune-images-modal').showModal();
  }

  function closePruneImagesModal() {
    doc.getElementById('prune-images-modal').close();
  }

  function confirmPruneImages() {
    var ids = _pendingPrune.map(function (row) { return row.id; });
    doc.getElementById('prune-images-modal').close();
    if (ids.length === 0) return Promise.resolve();
    return fetchImpl('/api/v1/upgrade/images/prune', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': _csrfToken() },
      body: JSON.stringify({ images: ids }),
    }).then(async function (r) {
      if (authGuard(r)) return;
      if (!r.ok) {
        _toast('error', T.removeFailed + " " + (await _upgradeError(r)));
        refreshUpgradeImages();
        return;
      }
      var data = await r.json();
      if (data.failed.length) {
        _toast('warning', fmt(T.removedPartly,
                  { removed: data.removed.length, kept: data.failed.length, name: data.failed[0].name, error: data.failed[0].error }));
      } else {
        _toast('success', fmtPlural(data.removed.length,
                  T.removedOne,
                  T.removedOther, { size: data.reclaimed_human }));
      }
      refreshUpgradeImages();
    });
  }

  // The buttons of the page and of its partials name what they do in `data-upgrade-action`;
  // one listener on the document serves the ones htmx swaps in later as well.
  const ACTIONS = {
    'dismiss': acknowledgeUpgrade,
    'reload': reload,
    'refresh-images': refreshUpgradeImages,
    'prune-selected': () => openPruneImagesModal(true),
    'prune-all': () => openPruneImagesModal(false),
    'prune-cancel': closePruneImagesModal,
    'prune-confirm': confirmPruneImages,
  };

  function handleClick(event) {
    const button = event.target && event.target.closest ? event.target.closest('[data-upgrade-action]') : null;
    if (!button) return;
    const action = ACTIONS[button.dataset.upgradeAction];
    if (action) action();
  }

  // The release picker (partials/upgrade_check.html) and the checkboxes of the image list
  // (partials/upgrade_images.html).
  function handleChange(event) {
    const element = event.target;
    if (!element) return;
    if (element.id === 'upgrade-version') recheckVersion(element.value);
    else if (element.id === 'prune-master-check') togglePruneAll(element.checked);
    else if (element.classList && element.classList.contains('prune-image-check')) updatePruneSelection();
  }

  // What the status and runner partials found, announced to the header cluster as the window
  // events it listens for. The status partial carries it in the data island `upgrade-state`,
  // the runner in data-* attributes of #upgrade-runner; called after every htmx settle with
  // the element that settled.
  function announce(root, win) {
    if (!root || typeof root.querySelectorAll !== 'function') return;
    const inside = (selector) => (root.matches(selector) ? [root] : [...root.querySelectorAll(selector)]);
    for (const island of inside('#upgrade-state')) {
      win.dispatchEvent(new win.CustomEvent('papaia-upgrade-state', { detail: JSON.parse(island.textContent) }));
    }
    for (const element of inside('#upgrade-runner[data-active]')) {
      win.dispatchEvent(new win.CustomEvent('papaia-upgrade-runner', { detail: {
        active: element.dataset.active === 'true',
        running: element.dataset.running === 'true',
        finished: element.dataset.finished === 'true',
      }}));
    }
  }

  return {
    runCheck, recheckVersion, startUpgrade, acknowledgeUpgrade, refreshUpgradeImages,
    updatePruneSelection, togglePruneAll, openPruneImagesModal, confirmPruneImages,
    handleClick, handleChange, announce,
  };
}

// Tier 1: one filled primary button, and it ships disabled.
//
// Nothing is known about this deployment at render time -- the page forks no
// subprocess, on purpose, so it comes up whether or not git, Docker and the
// remote are reachable. `#upgrade-status` and `#upgrade-runner` announce what
// they find, and this cluster listens. Disabled-until-proven is the safe
// default for an action that takes the whole stack down.
export function createUpgradeActions(T, page) {
  return {
    ready: false, target: null, checkFailed: false, forceable: false, gatePassed: true,
    hasBackupDir: false, current: '', runnerActive: false, runnerFinished: false,
    checking: true, starting: false,
    force: false, noBackup: false, pruneImages: true, confirmed: false,
    get blocked() { return this.runnerActive || this.checking || this.starting },
    open() {
      this.force = false; this.noBackup = !this.hasBackupDir; this.pruneImages = true;
      this.confirmed = false;
      this.$refs.upgradeModal.showModal();
    },
    run() {
      this.$refs.upgradeModal.close();
      this.starting = true;
      page.startUpgrade(this.target, this.force, this.noBackup, this.pruneImages)
        .then(ok => { if (!ok) this.starting = false });
    },
    check() {
      this.checking = true;
      page.runCheck(null).finally(() => { this.checking = false });
    },
    // @papaia-upgrade-state.window (partials/upgrade_status.html).
    applyState(detail) {
      this.ready = detail.ready;
      this.target = detail.target;
      this.checkFailed = detail.checkFailed;
      this.forceable = detail.forceable;
      this.gatePassed = detail.gatePassed;
      this.hasBackupDir = detail.hasBackupDir;
      this.current = detail.current;
      this.checking = false;
    },
    // @papaia-upgrade-runner.window (partials/upgrade_runner.html).
    applyRunner(detail) {
      this.runnerActive = detail.active;
      this.runnerFinished = detail.finished;
    },
    get installLabel() {
      if (this.target) {
        return this.runnerFinished ? T.dismissFirst : fmt(T.install, { version: this.target });
      }
      return this.checkFailed ? T.checkFailed : T.upToDate;
    },
    // x-effect of the sentences of the dialog (`data-sentence`): reads the values it shows,
    // so Alpine fills it again when one of them changes.
    fill(element) {
      const key = element.dataset.sentence;
      const values = {
        upgradeTitle: { target: this.target },
        upgradeIntro: { current: this.current, target: this.target },
        upgradeCheckout: { target: 'v' + this.target },
        upgradeLogs: { target: this.target },
      }[key];
      if (values) fillSentence(element, T[key], values);
    },
  };
}
