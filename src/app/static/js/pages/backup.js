// Backup / Restore (backup.html and the partials it swaps in: backup_job_status.html,
// backup_schedule.html, restore_point_list.html, restore_status.html). The page carries no
// inline script and no event attribute (T-UI-03): its texts come from the data island
// `backup-texts` in the language of the page, the values of a row from its data-* attributes,
// and the buttons name what they do in `data-backup-action`, which one listener on the
// document dispatches -- so the rows htmx swaps in need nothing run for them. The Alpine
// components of the page are registered here, before Alpine starts (base.html, block `page_modules`).
import { csrfToken } from "../core/csrf.js";
import { escapeHtml, fmt, fmtHtml, fmtPlural } from "../core/format.js";
import { readIsland } from "../core/islands.js";
import { actions, toaster } from "../core/runtime.js";
import {
  applySelectors,
  freshScope,
  isFullRestore,
  nextBlocked,
  restoreRequest,
  selectedCount,
  selectedGroups,
  setMode,
  toggleGroup,
  toggleItem,
} from "./backup-scope.js";

const T = readIsland("backup-texts");

function _el(id) { return document.getElementById(id); }

// A toast whose text is a sentence of the island plus what the server said; both are text.
function toastText(type, text) {
  toaster.show(type, escapeHtml(text));
}

// Shared error extraction: the API answers with {detail: ...} on every 4xx.
async function _apiError(r) {
  try {
    const e = await r.json();
    return e.detail || JSON.stringify(e);
  } catch (_) {
    return String(r.status);
  }
}

function _send(method, url, body) {
  const headers = { 'X-CSRF-Token': csrfToken() };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  return fetch(url, {
    method: method,
    headers: headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function _swap(url, target, swap) {
  window.htmx.ajax('GET', url, { target: target, swap: swap || 'innerHTML' });
}

function refreshRestorePoints() {
  _swap('/partials/backup/restore-points', '#restore-point-list');
}

// refreshBackupJobStatus: re-read the strip that owns the running state. Called
// on both outcomes of a POST -- after a 409 ("a backup job is already running")
// it is what corrects a page that thought the queue was idle, without waiting
// for the strip's own tick.
function refreshBackupJobStatus() {
  _swap('/partials/backup/job-status', '#backup-job-status', 'outerHTML');
}

// startBackup: POST the backup, then poll the job with the shared poller.
// onJobId lets the caller show a "view log" link while the job still runs.
function startBackup(onJobId) {
  var raw = _el('retention-days').value.trim();
  var body = {};
  if (raw !== '') body.retention_days = parseInt(raw, 10);
  var node = _el('backup-node');
  if (node && node.value) body.node = node.value;

  return _send('POST', '/api/v1/maintenance/backup', body).then(async function (r) {
    if (actions.authGuard(r)) return false;
    if (!r.ok) {
      toastText('error', T.backupFailedPrefix + " " + (await _apiError(r)));
      refreshBackupJobStatus();
      return false;
    }
    var data = await r.json();
    if (onJobId) onJobId(data.job_id);
    refreshBackupJobStatus();
    return new Promise(function (resolve) {
      actions.pollJob(data.job_id, function (job) {
        refreshRestorePoints();
        refreshBackupJobStatus();
        if (job.status === 'succeeded') {
          toastText('success', T.backupComplete);
        } else {
          toaster.show('error', fmtHtml(T.backupFailedLog, { job_id: data.job_id }));
        }
        resolve(true);
      });
    });
  });
}

// ── The job strip ───────────────────────────────────────────────────────────
//
// The header button lives outside #backup-job-status, and an out-of-band swap of
// that cluster would rebuild the <dialog> it contains -- closing an open
// confirmation on every poll. So the strip carries its state in data-job-*
// attributes, and every time htmx has settled a new strip it is announced as
// `papaia-job-state`, which the button listens to.
var _announcedStrip = _el('backup-job-status');

function announceJobState() {
  var strip = _el('backup-job-status');
  if (!strip || strip === _announcedStrip) return;
  _announcedStrip = strip;
  var d = strip.dataset;
  var active = d.jobActive === 'true';
  window.dispatchEvent(new CustomEvent('papaia-job-state', { detail: {
    active: active,
    jobId: active ? d.jobId : null,
    action: active ? d.jobAction : null,
  }}));
}

document.addEventListener('htmx:afterSettle', announceJobState);

// ── Delete restore points ────────────────────────────────────────────────
//
// The selection is an array on #backup-selection so it survives the list
// refresh, the same shape as the services page. The confirmation dialog is
// plain DOM; only the POST + job poll differ from startBackup.
var _pendingDeleteIds = [];

function openDeleteBackupModal(ids) {
  _pendingDeleteIds = (ids || []).slice();
  if (_pendingDeleteIds.length === 0) return;

  var many = _pendingDeleteIds.length > 1;
  _el('delete-backup-title').textContent = many
    ? fmt(T.deleteManyTitle, { count: _pendingDeleteIds.length })
    : fmt(T.deleteOneTitle, { id: _pendingDeleteIds[0] });
  _el('delete-backup-text').textContent = many ? T.deleteManyText : T.deleteOneText;

  var list = _el('delete-backup-id-list');
  list.textContent = '';
  _pendingDeleteIds.forEach(function (id) {
    var li = document.createElement('li');
    li.textContent = id;
    list.appendChild(li);
  });

  _el('delete-backup-modal').showModal();
}

function confirmDeleteBackups() {
  var ids = _pendingDeleteIds.slice();
  _el('delete-backup-modal').close();
  if (ids.length === 0) return;

  return _send('POST', '/api/v1/maintenance/restore-points/delete', { restore_points: ids })
    .then(async function (r) {
      if (actions.authGuard(r)) return;
      if (!r.ok) {
        toastText('error', T.deleteFailedPrefix + " " + (await _apiError(r)));
        refreshBackupJobStatus();
        return;
      }
      var data = await r.json();
      refreshBackupJobStatus();
      actions.pollJob(data.job_id, function (job) {
        refreshRestorePoints();
        refreshBackupJobStatus();
        if (job.status === 'succeeded') {
          window.dispatchEvent(new CustomEvent('papaia-backups-deleted'));
          toastText('success', ids.length > 1
            ? fmt(T.deletedMany, { count: ids.length })
            : T.deletedOne);
        } else {
          toaster.show('error', fmtHtml(T.deleteFailedLog, { job_id: data.job_id }));
        }
      });
    });
}

// ── Restore wizard ────────────────────────────────────────────────────────
//
// _scope holds what the three steps share (backup-scope.js).
//
// Plain DOM, no Alpine: the picker is built from JSON that arrives after the
// dialog opens, and an `x-effect` reading `$refs` of its own <dialog> silently
// kills `x-show` for the entire page. Same reason openRestorePointDetail builds
// its table by hand.
var _scope = null;

// Every value below comes from the API and is written with textContent or as a
// DOM node, never innerHTML. Same rule as openRestorePointDetail.
function _node(tag, className, text) {
  var el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

function openRestoreModal(id, artifacts, result, scope) {
  _scope = freshScope(id, artifacts, result);
  var scoped = !!(scope && scope.length);
  _el('restore-title').innerHTML = fmtHtml(T.restoreTitle, { id: id });
  _el('restore-everything-detail').textContent = fmt(T.everythingDetail, { count: artifacts });
  _el('restore-partial-note').classList.toggle('hidden', result !== 'partial');
  _el('restore-clean').checked = false;
  _el('restore-confirm').checked = false;
  _el('restore-go').disabled = true;
  _el('restore-picker').textContent = '';
  _el('restore-notes').textContent = '';
  _el('restore-tally').textContent = '';
  // A scoped restore point has no configuration directory and no core volumes,
  // so it cannot be restored as a whole: the choice is made for the operator.
  var allRadio = document.querySelector('input[name=restore-scope][value=all]');
  allRadio.checked = !scoped;
  allRadio.disabled = scoped;
  document.querySelector('input[name=restore-scope][value=some]').checked = scoped;
  if (scoped) {
    _scope.mode = 'some';
    _el('restore-everything-detail').textContent = fmt(T.scopedOnly, { scope: scope.join(', ') });
  }
  _renderStep();
  _el('restore-modal').showModal();

  var opened = _scope;
  fetch('/api/v1/maintenance/restore-points/' + encodeURIComponent(id) + '/selectors')
    .then(async function (r) {
      if (actions.authGuard(r)) return;
      if (!r.ok) return;
      var data = await r.json();
      // The dialog may have been opened for another point in the meantime.
      if (_scope !== opened) return;
      applySelectors(_scope, data);
      _renderPicker();
    });
}

function setRestoreScope(mode) {
  setMode(_scope, mode);
  _renderPicker();
}

function _renderPicker() {
  var box = _el('restore-picker');
  var notes = _el('restore-notes');
  box.textContent = '';
  notes.textContent = '';

  if (_scope.mode !== 'some') {
    box.classList.add('hidden');
    _el('restore-tally').textContent = '';
    _updateNextButton();
    return;
  }
  box.classList.remove('hidden');

  // Re-rendering resets the scroll box to the top, which throws the cursor off
  // the row that was just clicked once the list is longer than the panel.
  var scrollTop = box.scrollTop;

  var headings = { config: T.headingStack, module: T.headingCore, addon: T.headingAddons, node: T.headingNodes };
  var lastKind = '';
  _scope.groups.forEach(function (group) {
    if (group.kind !== lastKind) {
      lastKind = group.kind;
      box.appendChild(_node('div',
        'px-3 py-1.5 text-xs uppercase tracking-wide text-pk-text-muted bg-base-200',
        headings[group.kind] || T.headingOther));
    }
    box.appendChild(_groupRow(group));
  });

  // Not instead of the groups: such a snapshot still offers its configuration,
  // and the note says what it does not offer.
  if (!_scope.supported) {
    box.appendChild(_node('p', 'px-3 py-3 text-sm text-pk-text-muted border-t border-base-300',
      T.olderCore));
  }

  box.scrollTop = scrollTop;
  _scope.notes.forEach(function (note) { notes.appendChild(_node('span', '', note)); });
  _updateTally();
}

function _groupRow(group) {
  var wrap = _node('div', 'px-3 py-2.5 border-t border-base-300');
  var head = _node('div', 'flex items-start gap-3');

  var box = document.createElement('input');
  box.type = 'checkbox';
  box.className = 'checkbox checkbox-sm mt-0.5';
  box.checked = _scope.selected.has(group.selector);
  box.addEventListener('change', function () {
    toggleGroup(_scope, group, box.checked);
    _renderPicker();
  });
  head.appendChild(box);

  var text = _node('div', 'min-w-0 flex-1');
  text.appendChild(_node('span', 'text-sm font-medium', group.label));
  text.appendChild(_node('span', 'block text-xs text-pk-text-muted', group.summary));
  head.appendChild(text);

  head.appendChild(_node('div', 'text-xs text-pk-text-muted text-right whitespace-nowrap',
    fmtPlural(group.archives, T.archiveOne, T.archiveOther)));
  wrap.appendChild(head);

  if (group.requires_config && _scope.selected.has(group.selector)) {
    var alert = _node('div', 'alert alert-warning text-xs mt-2 py-2');
    var inner = _node('div', '');
    inner.appendChild(_node('span', 'font-semibold block',
      group.kind === 'config'
        ? T.configWhole
        : fmt(T.bringsConfig, { label: group.label })));
    group.hazards.forEach(function (h) { inner.appendChild(_node('span', 'block', h)); });
    inner.appendChild(_node('span', 'block mt-1', T.runsFull));
    alert.appendChild(inner);
    wrap.appendChild(alert);
  }

  if (group.items.length > 1) {
    var open = _scope.expanded.has(group.selector);
    var toggle = _node('button', 'btn btn-ghost btn-xs mt-1 ml-7',
                       open ? T.hideVolumes : T.showVolumes);
    toggle.type = 'button';
    var list = _node('div', 'ml-7 mt-1 pl-3 border-l border-base-300' + (open ? '' : ' hidden'));
    group.items.forEach(function (item) { list.appendChild(_itemRow(group, item)); });
    // The set is the source of truth, not the class: an item checkbox rebuilds
    // this whole row, and a DOM-only toggle would come back collapsed.
    toggle.addEventListener('click', function () {
      if (_scope.expanded.has(group.selector)) _scope.expanded.delete(group.selector);
      else _scope.expanded.add(group.selector);
      var hidden = list.classList.toggle('hidden');
      toggle.textContent = hidden ? T.showVolumes : T.hideVolumes;
    });
    wrap.appendChild(toggle);
    wrap.appendChild(list);
  }
  return wrap;
}

function _itemRow(group, item) {
  var label = _node('label', 'flex items-center gap-2 py-1 cursor-pointer');
  var box = document.createElement('input');
  box.type = 'checkbox';
  box.className = 'checkbox checkbox-xs';
  box.checked = _scope.selected.has(item.selector) || _scope.selected.has(group.selector);
  box.disabled = _scope.selected.has(group.selector);
  box.addEventListener('change', function () {
    toggleItem(_scope, group, item, box.checked);
    _renderPicker();
  });
  label.appendChild(box);
  label.appendChild(_node('span', 'text-xs', item.label));
  label.appendChild(_node('span', 'text-xs font-mono text-pk-text-muted', item.target));
  if (item.advanced) label.appendChild(_node('span', 'badge badge-ghost badge-xs', 'large'));
  return label;
}

function _updateTally() {
  _el('restore-tally').textContent = selectedGroups(_scope).length
    ? fmtPlural(selectedCount(_scope), T.itemOne, T.itemOther)
    : T.nothingSelected;
  _updateNextButton();
}

function _updateNextButton() {
  _el('restore-next').disabled = nextBlocked(_scope);
}

function restoreStep(delta) {
  var target = _scope.step + delta;
  if (target < 1 || target > 3) return;
  if (target === 2) _renderImpact();
  if (target === 3) _renderConfirm();
  _scope.step = target;
  _renderStep();
}

function _renderStep() {
  [1, 2, 3].forEach(function (n) {
    _el('restore-step-' + n).classList.toggle('hidden', n !== _scope.step);
  });
  _el('restore-step-badge').textContent = fmt(T.step, { step: _scope.step });
  _el('restore-back').classList.toggle('hidden', _scope.step === 1);
  _el('restore-next').classList.toggle('hidden', _scope.step === 3);
  _el('restore-go').classList.toggle('hidden', _scope.step !== 3);
  if (_scope.step === 1) _updateNextButton();
}

function _section(title, bodyClass) {
  var wrap = _node('div', '');
  wrap.appendChild(_node('p', 'text-xs uppercase tracking-wide text-pk-text-muted mb-1.5', title));
  var body = _node('div', bodyClass || '');
  wrap.appendChild(body);
  return { wrap: wrap, body: body };
}

function _renderImpact() {
  var full = isFullRestore(_scope);
  var groups = selectedGroups(_scope);
  var profiles = {};
  var addons = [];
  groups.forEach(function (g) {
    if (g.kind === 'addon') addons.push(g.label);
    else (g.profiles || []).forEach(function (p) { profiles[p] = true; });
  });

  var over = _section(T.overwritten);
  if (full) {
    over.body.appendChild(_node('p', 'text-sm',
      fmt(T.overwrittenFull, { count: _scope.artifacts })));
  } else {
    var list = _node('div', 'flex flex-col gap-0.5');
    groups.forEach(function (g) {
      if (_scope.selected.has(g.selector)) {
        list.appendChild(_node('span', 'text-sm',
          g.label + ' — ' + fmtPlural(g.archives, T.archiveOne, T.archiveOther)));
      } else {
        g.items.forEach(function (i) {
          if (_scope.selected.has(i.selector)) {
            list.appendChild(_node('span', 'text-sm font-mono', i.target));
          }
        });
      }
    });
    over.body.appendChild(list);
  }
  _el('impact-overwritten').textContent = '';
  _el('impact-overwritten').appendChild(over.wrap);

  // Built from profiles, not from the modules that were ticked: a profile is
  // what actually stops. Selecting SearXNG stops librechat-websearch, which is
  // nine containers across four modules.
  var restarted = _section(T.restarted, 'flex flex-wrap gap-1');
  var running = _section(T.running, 'flex flex-wrap gap-1');
  if (full) {
    restarted.body.appendChild(_node('span', 'text-sm', T.restartedFull));
    running.body.appendChild(_node('span', 'text-sm text-pk-text-muted', T.nothing));
  } else {
    _scope.services.forEach(function (svc) {
      var hit = (svc.profiles || []).some(function (p) { return profiles[p] === true; });
      if (hit) restarted.body.appendChild(_node('span', 'badge badge-warning badge-sm', svc.service));
      else running.body.appendChild(_node('span', 'badge badge-success badge-sm', svc.service));
    });
    addons.forEach(function (name) {
      restarted.body.appendChild(_node('span', 'badge badge-warning badge-sm', name));
    });
    if (!restarted.body.childNodes.length) {
      restarted.body.appendChild(_node('span', 'text-sm text-pk-text-muted', T.nothingTouched));
    }
  }
  _el('impact-restarted').textContent = '';
  _el('impact-restarted').appendChild(restarted.wrap);
  _el('impact-running').textContent = '';
  _el('impact-running').appendChild(running.wrap);

  var hazards = _el('impact-hazards');
  hazards.textContent = '';
  var seen = [];
  groups.forEach(function (g) {
    (g.hazards || []).forEach(function (h) { if (seen.indexOf(h) === -1) seen.push(h); });
  });
  seen.push(T.discarded);
  seen.forEach(function (text) {
    var alert = _node('div', 'alert alert-warning text-xs mb-2 py-2');
    alert.appendChild(_node('span', '', text));
    hazards.appendChild(alert);
  });
}

function _renderConfirm() {
  var full = isFullRestore(_scope);
  var points = _el('restore-confirm-points');
  points.textContent = '';
  if (full) {
    points.appendChild(_node('li', '', T.confirmConfig));
    points.appendChild(_node('li', '', fmt(T.confirmArchives, { count: _scope.artifacts })));
    points.appendChild(_node('li', '', T.confirmContainers));
  } else {
    selectedGroups(_scope).forEach(function (g) {
      points.appendChild(_node('li', '', fmt(T.confirmReplaced, { label: g.label })));
    });
    points.appendChild(_node('li', '', T.confirmOutside));
  }
  _el('restore-clean-row').classList.toggle('hidden', !full);
  if (!full) _el('restore-clean').checked = false;
  _el('restore-detach-note').classList.toggle('hidden', !full);
  _el('restore-inprocess-note').classList.toggle('hidden', full);
  _el('restore-confirm').checked = false;
  _el('restore-go').disabled = true;
}

// The destructive step: only after the confirmation box was ticked, which is what enables
// the button; the server checks the request again.
function submitRestore() {
  if (!_el('restore-confirm').checked) return;
  _el('restore-go').disabled = true;
  var request = restoreRequest(_scope, _el('restore-clean').checked);
  if (isFullRestore(_scope)) _submitFullRestore(request);
  else _submitScopedRestore(request);
}

function _submitFullRestore(request) {
  _send('POST', request.url, request.body).then(async function (r) {
    if (actions.authGuard(r)) return;
    if (!r.ok) {
      toastText('error', T.restoreNotStarted + " " + (await _apiError(r)));
      _el('restore-go').disabled = false;
      return;
    }
    _el('restore-modal').close();
    toastText('success', T.restoreStarted);
    // Load the status panel, which then polls itself across the restart.
    _swap('/partials/backup/restore-status', '#restore-status-slot');
  });
}

// A scoped restore is an ordinary job: it never replaces the config directory,
// so the manager -- and this page -- stay up for the whole thing. That is why
// it reports through the job log rather than the runner status panel.
function _submitScopedRestore(request) {
  _send('POST', request.url, request.body).then(async function (r) {
    if (actions.authGuard(r)) return;
    if (!r.ok) {
      toastText('error', T.restoreNotStarted + " " + (await _apiError(r)));
      _el('restore-go').disabled = false;
      return;
    }
    var data = await r.json();
    _el('restore-modal').close();
    toaster.show('success', fmtHtml(T.restoreStartedLog, { job_id: data.job_id }));
    refreshBackupJobStatus();
    actions.pollJob(data.job_id, function (job) {
      refreshBackupJobStatus();
      refreshRestorePoints();
      if (job.status === 'succeeded') {
        toastText('success', T.restoreComplete);
      } else {
        toaster.show('error', fmtHtml(T.restoreFailedLog, { job_id: data.job_id }));
      }
    });
  });
}

// acknowledgeRestore: drop the finished runner container so the next restore can
// start, then clear the panel and re-read the catalogue.
function acknowledgeRestore() {
  _send('DELETE', '/api/v1/maintenance/restore').then(async function (r) {
    if (actions.authGuard(r)) return;
    if (!r.ok) {
      toastText('error', T.dismissFailed + " " + (await _apiError(r)));
      return;
    }
    _swap('/partials/backup/restore-status', '#restore-status-slot');
    refreshRestorePoints();
  });
}

// openRestorePointDetail: renders the snapshot manifest. Every value comes from
// the API as text and is written via textContent / DOM nodes, never innerHTML.
function openRestorePointDetail(id) {
  var body = _el('detail-body');
  _el('detail-title').innerHTML = fmtHtml(T.detailTitle, { id: id });
  body.textContent = T.loading;
  _el('restore-detail-modal').showModal();

  fetch('/api/v1/maintenance/restore-points/' + encodeURIComponent(id))
    .then(async function (r) {
      if (actions.authGuard(r)) return;
      if (!r.ok) {
        body.textContent = T.detailFailed + " " + (await _apiError(r));
        return;
      }
      var data = await r.json();
      body.textContent = '';

      var meta = document.createElement('p');
      meta.className = 'text-pk-text-muted mb-3';
      meta.textContent = fmt(T.detailMeta,
                             { created: data.created_at, size: data.size_mb, artifacts: data.artifacts, result: data.result }) +
                         (data.papaia_version ? ' · ' + fmt(T.detailCore, { version: data.papaia_version }) : '');
      body.appendChild(meta);

      var list = data.artifact_list || [];
      if (!list.length) {
        var empty = document.createElement('p');
        empty.className = 'text-pk-text-muted';
        empty.textContent = T.noManifest;
        body.appendChild(empty);
        return;
      }

      var table = document.createElement('table');
      table.className = 'table table-zebra table-xs';
      var head = document.createElement('thead');
      var headRow = document.createElement('tr');
      [T.kind, T.target, T.owner].forEach(function (label) {
        var th = document.createElement('th');
        th.textContent = label;
        headRow.appendChild(th);
      });
      head.appendChild(headRow);
      table.appendChild(head);

      var tbody = document.createElement('tbody');
      list.forEach(function (a) {
        var tr = document.createElement('tr');
        [a.kind, a.target, a.owner].forEach(function (value, i) {
          var td = document.createElement('td');
          if (i === 1) td.className = 'font-mono';
          td.textContent = value == null ? '' : String(value);
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);

      var wrapper = document.createElement('div');
      wrapper.className = 'overflow-x-auto max-h-96 overflow-y-auto';
      wrapper.appendChild(table);
      body.appendChild(wrapper);
    });
}

// ── Backup schedule ──────────────────────────────────────────────────────────
//
// The page only edits the form and posts what the preview validated. Compiling a
// preset to cron, checking the expression, the timezone, the retention and the
// shortest allowed interval all happen on the server -- the same code the PUT
// uses -- so there is no second copy of those rules in this file.
function refreshScheduleStrip() {
  _swap('/partials/backup/schedule', '#backup-schedule-slot');
}

// Which rows apply to the chosen repeat. Hidden fields are still part of the form
// and still sent; the server reads only the ones its mode uses.
function scheduleModeChanged() {
  var mode = _el('schedule-mode').value;
  _el('schedule-time-row').classList.toggle('hidden', !(mode === 'daily' || mode === 'weekly'));
  _el('schedule-weekdays-row').classList.toggle('hidden', mode !== 'weekly');
  _el('schedule-hours-row').classList.toggle('hidden', mode !== 'hourly');
  _el('schedule-minute-row').classList.toggle('hidden', mode !== 'hourly');
  _el('schedule-cron-row').classList.toggle('hidden', mode !== 'custom');
}

function _fillScheduleForm(data) {
  var s = data.schedule;
  var p = data.preset;
  _el('schedule-enabled').checked = s ? s.enabled : true;
  _el('schedule-mode').value = p ? p.mode : 'daily';
  _el('schedule-time').value = p && (p.mode === 'daily' || p.mode === 'weekly') ? p.time : '03:00';
  document.querySelectorAll('input[name=weekdays]').forEach(function (box) {
    box.checked = !!p && p.mode === 'weekly' && p.weekdays.indexOf(box.value) !== -1;
  });
  _el('schedule-every-hours').value = String(p ? p.every_hours : 6);
  _el('schedule-minute').value = p && p.mode === 'hourly' ? parseInt(p.time.slice(3), 10) : 0;
  _el('schedule-cron').value = p && p.mode === 'custom' ? p.cron : (s ? s.cron : '');
  _el('schedule-timezone').value = s ? s.timezone : data.timezone;
  _el('schedule-retention').value = s && s.retention_days ? s.retention_days : '';
  _el('schedule-catchup').checked = s ? s.run_on_startup === 'if_missed' : true;
  // An unreadable schedule.yaml has no schedule to show, but it can be removed.
  _el('schedule-remove').classList.toggle('hidden', !data.configured && !data.error);
  _disarmRemoveSchedule();
  scheduleModeChanged();
}

function openScheduleModal() {
  fetch('/api/v1/maintenance/schedule').then(async function (r) {
    if (actions.authGuard(r)) return;
    if (!r.ok) {
      toastText('error', T.scheduleLoadFailed + " " + (await _apiError(r)));
      return;
    }
    _fillScheduleForm(await r.json());
    _el('schedule-save').disabled = true;
    _el('schedule-modal').showModal();
    window.htmx.trigger('#schedule-form', 'change');
  });
}

// Save is only offered for a form the server has just validated, and not while a
// newer validation is still on its way -- otherwise a quick click would send the
// values of the previous keystroke.
document.addEventListener('htmx:beforeRequest', function (e) {
  if (e.detail.elt && e.detail.elt.id === 'schedule-form') _el('schedule-save').disabled = true;
});
document.addEventListener('htmx:afterSwap', function (e) {
  if (e.detail.target && e.detail.target.id === 'schedule-preview') {
    _el('schedule-save').disabled =
      !document.querySelector('#schedule-preview [data-valid="true"]');
  }
});

function saveSchedule() {
  if (!document.querySelector('#schedule-preview [data-valid="true"]')) {
    toastText('error', T.fixSchedule);
    return;
  }
  var raw = _el('schedule-retention').value.trim();
  var body = {
    enabled: _el('schedule-enabled').checked,
    cron: _el('schedule-compiled-cron').value,
    timezone: _el('schedule-compiled-timezone').value,
    retention_days: raw === '' ? null : parseInt(raw, 10),
    run_on_startup: _el('schedule-catchup').checked ? 'if_missed' : 'never',
  };
  _el('schedule-save').disabled = true;
  _send('PUT', '/api/v1/maintenance/schedule', body).then(async function (r) {
    if (actions.authGuard(r)) return;
    if (!r.ok) {
      toastText('error', T.scheduleNotSaved + " " + (await _apiError(r)));
      _el('schedule-save').disabled = false;
      return;
    }
    _el('schedule-modal').close();
    toastText('success', body.enabled ? T.scheduleSaved : T.scheduleSavedPaused);
    refreshScheduleStrip();
  });
}

// Removing is two clicks on the same button: the first arms it, the second does it.
// A dialog for a confirmation inside a dialog is more ceremony than a file this
// small deserves.
var _removeScheduleTimer = null;

function _disarmRemoveSchedule() {
  clearTimeout(_removeScheduleTimer);
  _removeScheduleTimer = null;
  _el('schedule-remove').textContent = T.removeSchedule;
}

function removeSchedule() {
  if (_removeScheduleTimer === null) {
    _el('schedule-remove').textContent = T.clickAgain;
    _removeScheduleTimer = setTimeout(_disarmRemoveSchedule, 4000);
    return;
  }
  _disarmRemoveSchedule();
  _send('DELETE', '/api/v1/maintenance/schedule').then(async function (r) {
    if (actions.authGuard(r)) return;
    if (!r.ok) {
      toastText('error', T.scheduleNotRemoved + " " + (await _apiError(r)));
      return;
    }
    _el('schedule-modal').close();
    toastText('success', T.scheduleRemoved);
    refreshScheduleStrip();
  });
}

// ── Wiring ──────────────────────────────────────────────────────────────────
//
// The buttons of the page and of the partials say what they do in data-backup-action; the
// values a row hands over are data-* attributes the server wrote. A dialog's cancel button
// names its dialog in data-dialog-close.
function _scopeOf(element) {
  try {
    var scope = JSON.parse(element.dataset.scope || '[]');
    return Array.isArray(scope) ? scope.map(String) : [];
  } catch (_) {
    return [];
  }
}

const ACTIONS = {
  'open-detail': function (el) { openRestorePointDetail(el.dataset.restorePoint); },
  'open-restore': function (el) {
    openRestoreModal(el.dataset.restorePoint, Number(el.dataset.artifacts),
                     el.dataset.result || null, _scopeOf(el));
  },
  'delete-one': function (el) { openDeleteBackupModal([el.dataset.restorePoint]); },
  'confirm-delete': function () { confirmDeleteBackups(); },
  'restore-back': function () { restoreStep(-1); },
  'restore-next': function () { restoreStep(1); },
  'restore-go': function () { submitRestore(); },
  'acknowledge-restore': function () { acknowledgeRestore(); },
  'open-schedule': function () { openScheduleModal(); },
  'save-schedule': function () { saveSchedule(); },
  'remove-schedule': function () { removeSchedule(); },
};

document.addEventListener('click', function (event) {
  var target = event.target instanceof Element ? event.target : null;
  if (!target) return;
  var close = target.closest('[data-dialog-close]');
  if (close) {
    var dialog = _el(close.dataset.dialogClose);
    if (dialog) dialog.close();
    return;
  }
  var button = target.closest('[data-backup-action]');
  if (!button || button.disabled) return;
  var run = ACTIONS[button.dataset.backupAction];
  if (run) run(button);
});

document.addEventListener('change', function (event) {
  var target = event.target;
  if (!(target instanceof HTMLInputElement) && !(target instanceof HTMLSelectElement)) return;
  if (target.name === 'restore-scope' && target.type === 'radio') {
    setRestoreScope(target.value === 'some' ? 'some' : 'all');
  } else if (target.id === 'restore-confirm') {
    _el('restore-go').disabled = !target.checked;
  } else if (target.id === 'schedule-mode') {
    scheduleModeChanged();
  }
});

// The editor is validated by htmx on input; Enter in one of its fields submits nothing.
document.addEventListener('submit', function (event) {
  if (event.target instanceof HTMLFormElement && event.target.id === 'schedule-form') {
    event.preventDefault();
  }
});

// ── Alpine components ──────────────────────────────────────────────────────
//
// backupActions: the header cluster. `busy` starts from the server (data-busy), not from
// `false`, so a backup running in another tab is not offered twice; #backup-job-status keeps
// it current from there (papaia-job-state above).
function backupActions() {
  return {
    busy: false,
    jobId: null,
    init() {
      var d = this.$el.dataset;
      this.busy = d.busy === 'true';
      this.jobId = d.jobId || null;
    },
    get busyTitle() {
      return this.busy ? T.busyTitle : '';
    },
    get logHref() {
      return '/jobs/' + this.jobId;
    },
    onJobState(event) {
      this.busy = event.detail.active;
      this.jobId = event.detail.jobId;
    },
    openBackupModal() {
      _el('backup-modal').showModal();
    },
    closeBackupModal() {
      _el('backup-modal').close();
    },
    createBackup() {
      var self = this;
      _el('backup-modal').close();
      self.busy = true;
      startBackup(function (id) { self.jobId = id; }).then(function () {
        self.busy = false;
        self.jobId = null;
      });
    },
  };
}

// backupSelection: the restore points ticked for the bulk delete. It sits on
// #backup-selection, outside the list htmx swaps, so the selection outlives a refresh.
function backupSelection() {
  return {
    sel: [],
    busy: false,
    toggle(id) {
      this.sel = this.sel.includes(id)
        ? this.sel.filter(function (x) { return x !== id; })
        : this.sel.concat([id]);
    },
    clear() {
      this.sel = [];
    },
    get selectionText() {
      return fmtPlural(this.sel.length, T.selectedOne, T.selectedOther);
    },
    deleteSelected() {
      openDeleteBackupModal(this.sel);
    },
  };
}

document.addEventListener('alpine:init', function () {
  window.Alpine.data('backupActions', backupActions);
  window.Alpine.data('backupSelection', backupSelection);
});
