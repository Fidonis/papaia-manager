// Settings -> Modules (settings_modules.html): switch the optional parts of the stack on and
// off. A change runs as a configuration job; the job strip above the list
// (partials/config_job_status.html) shows its progress and announces its state through
// core/announce.js. Values come from the data island `mgr-settings-modules`; the page carries
// no inline script and no event attribute (T-UI-03), the listeners are added at the end.
import { escapeHtml, fmt, fmtHtml, fmtPlural } from "../core/format.js";
import { readIsland } from "../core/islands.js";
import { actions, showToast } from "../core/runtime.js";

const page = readIsland("mgr-settings-modules");
const T = page.texts;

let modulesRevision = page.revision;
const MODULE_ROWS = page.rows;
const MAIL_DEFAULT_PORTS = page.defaultPorts;
let offTarget = null;
let offScope = "keep";
// Whether the operator files go too (only offered for a module that has them).
let offOperator = false;

// What a switch-off adds to the generic sentence, per module.
const OFF_NOTES = page.offNotes;

function moduleRow(name) { return MODULE_ROWS.find((r) => r.name === name); }
function moduleBox(name) { return document.querySelector('[data-module="' + name + '"]'); }
function offEl(id) { return document.getElementById(id); }

function moduleToggled(box) {
  const name = box.dataset.module;
  const row = moduleRow(name);
  if (box.checked) {
    if (name === "localai" || name === "rag" || name === "voice" || (name === "mail" && !row.configured)) openPanel(name);
    else enableModule(name);
  } else {
    openOffDialog(name);
  }
}

// What `setup` asks when a module is switched on and its settings are missing: the core says
// which questions are open (module-info) and what it would derive as the answer. Questions
// that are answered already are not asked again. A core that can't say asks nothing.
async function openQuestions(name) {
  const info = await loadInfo(name);
  return info ? (info.questions || []).filter((q) => q.ask) : [];
}

function questionField(name, question) {
  const wrap = document.createElement("div");
  wrap.className = "form-control w-full";
  const label = document.createElement("label");
  label.className = "label-text mb-1 text-sm font-medium";
  label.htmlFor = "q-url-" + name;
  label.textContent = question.label;
  const input = document.createElement("input");
  input.id = "q-url-" + name;
  input.type = "text";
  input.className = "input input-bordered input-sm w-full";
  input.value = question.default;
  input.autocomplete = "off";
  input.spellcheck = false;
  input.dataset.option = question.option;
  const hint = document.createElement("p");
  hint.className = "mt-1 text-xs text-pk-text-muted";
  hint.textContent = T.questionHint;
  wrap.append(label, input, hint);
  return wrap;
}

// The questions of a module open under its row, the same for every module and the same on a
// phone: nothing covers the page.
function panel(name) { return offEl("panel-" + name); }
function panelOpen(name) { const p = panel(name); return !!p && !p.classList.contains("hidden"); }

function panelError(name, text) {
  const el = panel(name).querySelector("[data-panel-error]");
  el.textContent = text || "";
  el.classList.toggle("hidden", !text);
}

function closePanel(name) {
  const p = panel(name);
  if (!p) return;
  p.classList.add("hidden");
  panelError(name, "");
  const probe = p.querySelector("#om-probe-result");
  if (probe) probe.classList.add("hidden");
  const questions = p.querySelector("[data-questions]");
  if (questions) questions.replaceChildren();
  p.querySelectorAll('input[type="password"]').forEach((el) => { el.value = ""; });
}

function cancelPanel(name) {
  moduleBox(name).checked = false;
  closePanel(name);
}

async function openPanel(name) {
  const row = moduleRow(name);
  const p = panel(name);
  panelError(name, "");
  if (name === "mail") {
    fillMail(row.mail_defaults);
    p.classList.remove("hidden");
    offEl("om-host").focus();
    return;
  }
  const box = moduleBox(name);
  box.disabled = true;
  const open = await openQuestions(name);
  box.disabled = false;
  // The RAG system has nothing else to choose: with its settings in place it just switches on.
  if (!open.length && name === "rag") {
    enableModule(name);
    return;
  }
  const questions = p.querySelector("[data-questions]");
  questions.replaceChildren();
  if (open.length) questions.append(questionField(name, open[0]));
  p.classList.remove("hidden");
  const first = p.querySelector("input, select");
  if (first) first.focus();
}

// The mail server's values as the form holds them. The password is only sent when there is a
// login and one was typed.
function mailForm() {
  const login = offEl("om-login").checked;
  const port = offEl("om-port").value.trim();
  const values = {
    host: offEl("om-host").value.trim(),
    port: port === "" ? null : Number(port),
    security: offEl("om-security").value,
    user: login ? offEl("om-user").value.trim() : "",
  };
  const password = offEl("om-password").value;
  if (login && password) values.password = password;
  return values;
}

function probeResult(ok, text) {
  const el = offEl("om-probe-result");
  el.textContent = text;
  el.classList.remove("hidden", "border-success", "bg-success/10", "border-error", "bg-error/10");
  el.classList.add(ok ? "border-success" : "border-error", ok ? "bg-success/10" : "bg-error/10");
}

// Talks to the mail server with what is in the form: greeting, encryption, login. Nothing is
// sent and nothing is saved (the same check as on the Mail page).
async function probeOnMail() {
  const button = offEl("om-probe");
  button.disabled = true;
  panelError("mail", "");
  probeResult(true, T.connecting);
  const reply = await actions.ragRequest("POST", button.dataset.url, mailForm());
  button.disabled = false;
  if (!reply.ok) {
    if (!reply.handled) probeResult(false, reply.error);
    return;
  }
  const r = reply.data;
  if (r.ok) {
    const login = r.login === "ok" ? T.loginAccepted : T.noLoginNeeded;
    probeResult(true, fmt(T.probeAnswered, { login: login }));
  } else {
    probeResult(false, fmt(T.probeStopped, { stage: r.stage, reason: r.reason }));
  }
}

function fillMail(d) {
  offEl("om-host").value = d.host || "";
  offEl("om-security").value = d.security;
  offEl("om-port").value = d.port;
  offEl("om-port").dataset.mode = d.security;
  offEl("om-login").checked = d.login || !d.host;
  offEl("om-user").value = d.user || "";
  offEl("om-password").value = "";
  offEl("om-from").value = d.from_address || "";
  offEl("om-from-name").value = d.from_name || "";
  syncOnMail();
}

// A login makes no sense without encryption, and without a login there is nothing to type in
// the two fields (the same rules as the Mail page).
function syncOnMail() {
  const none = offEl("om-security").value === "none";
  const box = offEl("om-login");
  if (none) box.checked = false;
  box.disabled = none;
  offEl("om-user").disabled = !box.checked;
  offEl("om-password").disabled = !box.checked;
}

offEl("om-security").addEventListener("change", () => {
  const port = offEl("om-port");
  const mode = offEl("om-security").value;
  // 587, 465 and 25 belong to their mode: follow it unless a port was typed on purpose.
  if (port.value === "" || Number(port.value) === MAIL_DEFAULT_PORTS[port.dataset.mode]) {
    port.value = MAIL_DEFAULT_PORTS[mode];
  }
  port.dataset.mode = mode;
  syncOnMail();
});

// The dialog first shows the plain question, then asks the core what the module holds and
// adds the choice. A core that can't say (or a failed request) leaves the plain switch-off.
// What the core says a module holds takes a moment to ask (it lists the Docker volumes), so it
// is asked before the page needs it: once for every module that is on when the page loads, and
// again when the answer is older than a minute. The dialog opens when it has the answer, so it
// never shows the plain question first and the choice a moment later.
const INFO_MAX_AGE_MS = 60000;
const infoCache = {};

function loadInfo(name) {
  const hit = infoCache[name];
  if (hit && Date.now() - hit.at < INFO_MAX_AGE_MS) return hit.promise;
  const promise = actions.ragRequest("GET", "/api/v1/config/modules/" + name + "/info").then((reply) => {
    if (!reply.ok || !reply.data.available) {
      delete infoCache[name];
      return null;
    }
    return reply.data;
  });
  infoCache[name] = { at: Date.now(), promise: promise };
  return promise;
}

// The modules that are on may be switched off (volumes, settings); the ones that ask a
// question may be switched on.
MODULE_ROWS.filter((r) => r.can_delete && (r.enabled || r.name === "rag" || r.name === "localai"))
  .forEach((r) => loadInfo(r.name));

async function openOffDialog(name) {
  const row = moduleRow(name);
  let info = null;
  if (row.can_delete && !row.keep_only) {
    const box = moduleBox(name);
    box.disabled = true;
    info = await loadInfo(name);
    box.disabled = false;
  }
  offTarget = name;
  offScope = "keep";
  offOperator = false;
  offEl("module-off-operator-input").checked = false;
  offEl("module-off-operator").classList.add("hidden");
  offEl("module-off-title").textContent = fmt(T.offTitle, { module: row.title });
  const containers = fmtPlural(row.services, T.container, T.containers);
  offEl("module-off-text").textContent = row.modules > 1
    ? fmt(T.stopsInModules, { containers: containers, modules: row.modules })
    : fmt(T.stops, { containers: containers });
  offEl("module-off-scopes").classList.add("hidden");
  offEl("module-off-confirm").classList.add("hidden");
  offEl("module-off-confirm-input").value = "";
  const note = offEl("module-off-note");
  note.textContent = [OFF_NOTES[name], row.keep_only].filter(Boolean).join(" ");
  note.classList.toggle("hidden", !note.textContent);
  if (info) renderScopes(name, info);
  syncOffButton();
  offEl("module-off-dialog").showModal();
}

function option(value, title, detail, checked) {
  const label = document.createElement("label");
  label.className = "flex cursor-pointer items-start gap-3 rounded-lg border border-base-300 p-3";
  const input = document.createElement("input");
  input.type = "radio";
  input.name = "module-off-scope";
  input.value = value;
  input.className = "radio radio-sm mt-0.5";
  input.checked = checked;
  input.addEventListener("change", () => { offScope = value; syncOffScope(); });
  const text = document.createElement("span");
  text.className = "min-w-0 text-sm";
  const head = document.createElement("span");
  head.className = "block font-medium";
  head.textContent = title;
  const body = document.createElement("span");
  body.className = "block break-words text-xs text-pk-text-muted";
  body.textContent = detail;
  text.append(head, body);
  label.append(input, text);
  return label;
}

function renderScopes(name, info) {
  const row = moduleRow(name);
  const box = offEl("module-off-scopes");
  box.querySelectorAll("label").forEach((n) => n.remove());
  const volumes = info.volumes || [];
  const present = volumes.filter((v) => v.exists !== false);
  const unknown = volumes.some((v) => v.exists === null);
  const names = present.map((v) => v.name).join(", ");
  const settings = (info.settings.dirs || []).map((d) => d + "/.env")
    .concat(info.settings.root_keys && info.settings.root_keys.length ? [".env (" + info.settings.root_keys.join(", ") + ")"] : [])
    .concat(info.settings.files || []).join(", ");

  box.append(option("keep", T.keep,
    T.keepDetail, true));
  if (present.length) {
    box.append(option("data", T.deleteData,
      fmtPlural(present.length, T.volume, T.volumes, { names: names }) +
      (unknown ? " " + T.dockerUnknown : ""), false));
    box.append(option("all", T.deleteAll,
      fmt(info.settings.regenerates_secrets
        ? T.settingsSecrets
        : T.settingsDefaults, { settings: settings }), false));
  } else {
    box.append(option("all", T.deleteSettings,
      fmt(T.noData, { settings: settings }), false));
  }
  box.classList.remove("hidden");
  renderOperatorFiles(row, info);
  offEl("module-off-confirm-label").innerHTML = fmtHtml(T.confirmDeletion, { name: name });
  syncOffScope();
}

// The operator files that exist, named the way the AI map page names them. No files, no
// choice.
function renderOperatorFiles(row, info) {
  const labels = row.operator_files || {};
  const files = (info.operator_files || []).filter((f) => f.exists && labels[f.path]);
  const list = offEl("module-off-operator-files");
  list.replaceChildren();
  files.forEach((f) => {
    const item = document.createElement("li");
    const head = document.createElement("span");
    head.className = "font-medium";
    head.textContent = labels[f.path].label;
    const path = document.createElement("code");
    path.className = "ml-1 text-pk-text-muted";
    path.textContent = f.path.split("/").pop();
    const detail = document.createElement("span");
    detail.className = "block text-pk-text-muted";
    detail.textContent = labels[f.path].description;
    item.append(head, path, detail);
    list.append(item);
  });
  offEl("module-off-operator").classList.toggle("hidden", files.length === 0);
}

function offDeleting() {
  return offScope !== "keep" || offOperator;
}

function syncOffScope() {
  const deleting = offDeleting();
  offEl("module-off-confirm").classList.toggle("hidden", !deleting);
  const go = offEl("module-off-go");
  go.classList.toggle("btn-error", deleting);
  go.classList.toggle("btn-primary", !deleting);
  go.textContent = deleting ? T.switchOffDelete : T.switchOff;
  if (deleting) offEl("module-off-confirm-input").focus();
  syncOffButton();
}

function syncOffButton() {
  const deleting = offDeleting();
  offEl("module-off-go").disabled =
    deleting && offEl("module-off-confirm-input").value.trim() !== offTarget;
}

// Runs for Cancel, Escape (the cancel event) and the backdrop, before the dialog is closed, so
// the toggle shows the module as on again the moment the dialog is gone. The close event,
// which the browser queues after the dialog has closed, only catches any other way out.
function cancelOff() {
  const name = offTarget;
  offTarget = null;
  if (name) moduleBox(name).checked = true;
  const dialog = offEl("module-off-dialog");
  if (dialog.open) dialog.close();
}

function startedModuleJob(jobId, started) {
  showToast("info", escapeHtml(started));
  window.htmx.ajax("GET", "/partials/config/job-status", { target: "#config-job-status", swap: "outerHTML" });
  followConfigJob(jobId);
}

async function startModuleJob(path, body, started) {
  const reply = await actions.ragRequest("POST", "/api/v1/config/modules/" + path, body);
  if (!reply.ok) {
    if (!reply.handled) showToast("error", escapeHtml(reply.error));
    return false;
  }
  startedModuleJob(reply.data.job_id, started);
  return true;
}

// Switches a module on. With its panel open the answers in it go along; a refusal is shown in
// the panel, which stays open so the answer can be corrected.
async function enableModule(name) {
  const row = moduleRow(name);
  const body = { revision: modulesRevision };
  const open = panelOpen(name);
  if (open) {
    const p = panel(name);
    if (name === "localai") body.variant = offEl("localai-variant").value;
    if (name === "voice") body.variant = offEl("voice-variant").value;
    const url = p.querySelector("[data-option]");
    if (url) body.public_url = url.value.trim();
    if (name === "mail") {
      body.mail = Object.assign(mailForm(), {
        from_address: offEl("om-from").value.trim(),
        from_name: offEl("om-from-name").value.trim(),
      });
    }
  }
  const go = open ? panel(name).querySelector("[data-panel-go]") : null;
  if (go) go.disabled = true;
  const reply = await actions.ragRequest("POST", "/api/v1/config/modules/" + name + "/enable", body);
  if (go) go.disabled = false;
  if (!reply.ok) {
    if (open) {
      if (!reply.handled) panelError(name, reply.error);
    } else {
      if (!reply.handled) showToast("error", escapeHtml(reply.error));
      moduleBox(name).checked = false;
    }
    return;
  }
  // The password has done its job; it does not stay in the field while the job runs.
  closePanel(name);
  startedModuleJob(reply.data.job_id, fmt(T.switchingOn, { module: row.title }));
}

async function confirmOff() {
  const name = offTarget;
  const scope = offScope;
  const operatorFiles = offOperator;
  const confirm = offEl("module-off-confirm-input").value.trim();
  offEl("module-off-dialog").close();
  offTarget = null;
  const row = moduleRow(name);
  const body = { revision: modulesRevision, scope: scope };
  if (operatorFiles) body.operator_files = true;
  if (scope !== "keep" || operatorFiles) body.confirm = confirm;
  const ok = await startModuleJob(name + "/disable", body, fmt(T.switchingOff, { module: row.title }));
  if (!ok) moduleBox(name).checked = true;
}

// The job strip shows the progress; this only waits for the end to show the new state.
let configFollowing = false;
function followConfigJob(jobId) {
  configFollowing = true;
  actions.pollJob(jobId, (job) => {
    if (job.status === "succeeded") showToast("success", escapeHtml(T.done));
    else showToast("error", escapeHtml(T.failed));
    setTimeout(() => window.location.reload(), 900);
  });
}

// A job started in another tab, or before this page was opened, changes what the toggles
// show: reload once when the strip sees it end.
let configJobWasActive = false;
window.addEventListener("papaia-config-job", (event) => {
  if (configFollowing) return;
  if (event.detail.active) configJobWasActive = true;
  else if (configJobWasActive) window.location.reload();
});

// -- bindings ---------------------------------------------------------------------------------

// Every toggle the page may switch (the locked ones are disabled in the markup).
document.querySelectorAll("input[data-module]").forEach((box) => {
  if (!box.disabled) box.addEventListener("change", () => moduleToggled(box));
});

// The panels under the rows: switch on with the answers, or cancel. The mail form also
// switches on when it is submitted (Enter in a field).
document.querySelectorAll("[data-module-row]").forEach((row) => {
  const name = row.dataset.moduleRow;
  const go = row.querySelector("[data-panel-go]");
  if (go) go.addEventListener("click", () => enableModule(name));
  const cancel = row.querySelector("[data-panel-cancel]");
  if (cancel) cancel.addEventListener("click", () => cancelPanel(name));
});
const mailFormElement = offEl("om-form");
if (mailFormElement) {
  mailFormElement.addEventListener("submit", (event) => {
    event.preventDefault();
    enableModule("mail");
  });
  offEl("om-login").addEventListener("change", syncOnMail);
  offEl("om-probe").addEventListener("click", probeOnMail);
}

// The switch-off dialog. Escape fires its cancel event and the backdrop is a button of its own;
// both cancel at once. The close event comes after the dialog has closed and stays the fallback.
const offDialog = offEl("module-off-dialog");
offDialog.addEventListener("cancel", () => {
  if (offTarget) cancelOff();
});
offDialog.querySelector(".modal-backdrop button").addEventListener("click", () => {
  if (offTarget) cancelOff();
});
offDialog.addEventListener("close", () => {
  if (offTarget) cancelOff();
});
offEl("module-off-operator-input").addEventListener("change", (event) => {
  offOperator = event.target.checked;
  syncOffScope();
});
offEl("module-off-confirm-input").addEventListener("input", syncOffButton);
offEl("module-off-cancel").addEventListener("click", cancelOff);
offEl("module-off-go").addEventListener("click", confirmOff);
