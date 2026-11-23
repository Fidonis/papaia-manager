// Mail -> Server (mail.html): the form of the relay's mail server, its probe and the test mail.
//
// The page has no Alpine component: the module adds the listeners the elements used to carry
// as on* attributes and finds the elements by their ids, which stay as they were. The
// server's values come from the data island `mail-init`: the stored settings (never the
// password), the default port of each connection mode and the sentences in the language of
// the page. Results are set as text. Everything the page talks to comes in as an argument;
// the bottom of this file wires it up.
import { escapeHtml, fmt } from "../core/format.js";
import { readIsland } from "../core/islands.js";
import { pageCore } from "../core/page.js";

// The toast of the outcome stays readable for a moment before the page is loaded again.
const RELOAD_MS = 900;

export function installMailPage(doc, { init, request, show, pollJob, refreshStrip, events, reload, clock }) {
  const texts = init.texts;
  const mail = init.mail;
  const defaultPorts = init.default_ports;
  const revision = mail.revision;
  const el = (id) => doc.getElementById(id);
  let following = false;
  let jobWasActive = false;

  // A login makes no sense without encryption, and without a login there is nothing to
  // type in the two fields.
  function syncLogin() {
    const none = el("mail-security").value === "none";
    const box = el("mail-login");
    if (none) box.checked = false;
    box.disabled = none;
    el("mail-user").disabled = !box.checked;
    el("mail-password").disabled = !box.checked;
  }

  function followSecurity() {
    const port = el("mail-port");
    const mode = el("mail-security").value;
    // 587, 465 and 25 belong to their mode: follow it unless a port was typed on purpose.
    if (port.value === "" || Number(port.value) === defaultPorts[port.dataset.mode]) {
      port.value = defaultPorts[mode];
    }
    port.dataset.mode = mode;
    syncLogin();
  }

  function values() {
    const login = el("mail-login").checked;
    const port = el("mail-port").value.trim();
    const result = {
      host: el("mail-host").value.trim(),
      port: port === "" ? null : Number(port),
      security: el("mail-security").value,
      user: login ? el("mail-user").value.trim() : "",
    };
    const password = el("mail-password").value;
    if (login && password) result.password = password;
    return result;
  }

  function showResult(id, ok, text) {
    const box = el(id);
    box.textContent = text;
    box.classList.remove("hidden", "border-success", "bg-success/10", "border-error", "bg-error/10");
    if (id === "mail-probe-result") box.classList.add(ok ? "border-success" : "border-error", ok ? "bg-success/10" : "bg-error/10");
    else box.classList.toggle("text-error", !ok);
  }

  async function save(event) {
    event.preventDefault();
    const button = el("mail-save");
    button.disabled = true;
    const reply = await request("PUT", "/api/v1/mail", Object.assign(
      { revision: revision, from_address: el("mail-from").value.trim(),
        from_name: el("mail-from-name").value.trim() },
      values(),
    ));
    if (!reply.ok) {
      button.disabled = false;
      if (!reply.handled) show("error", escapeHtml(reply.error));
      return;
    }
    // The password has done its job; it does not stay in the field while the job runs.
    el("mail-password").value = "";
    show("info", mail.enabled ? texts.applying : texts.saving);
    refreshStrip();
    follow(reply.data.job_id);
  }

  // The job strip shows the progress; this only waits for the end to show the new state.
  function follow(jobId) {
    following = true;
    pollJob(jobId, (job) => {
      if (job.status === "succeeded") show("success", mail.enabled ? texts.applied : texts.saved);
      else show("error", texts.applyFailed);
      clock.setTimeout(() => reload(), RELOAD_MS);
    });
  }

  async function probe() {
    const button = el("mail-probe");
    button.disabled = true;
    showResult("mail-probe-result", true, texts.connecting);
    const reply = await request("POST", "/api/v1/mail/probe", values());
    button.disabled = false;
    if (!reply.ok) {
      if (reply.handled) return;
      showResult("mail-probe-result", false, reply.error);
      return;
    }
    const r = reply.data;
    if (r.ok) {
      const login = r.login === "ok" ? texts.loginAccepted : texts.loginNotNeeded;
      showResult("mail-probe-result", true, fmt(texts.answered, { login: login }));
    } else {
      showResult("mail-probe-result", false, fmt(texts.stopped, { stage: r.stage, reason: r.reason }));
    }
  }

  async function sendTest(event) {
    event.preventDefault();
    const button = el("mail-test-send");
    button.disabled = true;
    const reply = await request("POST", "/api/v1/mail/test-message", { to: el("mail-test-to").value.trim() });
    button.disabled = false;
    if (!reply.ok) {
      if (!reply.handled) showResult("mail-test-result", false, reply.error);
      return;
    }
    if (reply.data.ok) showResult("mail-test-result", true, texts.accepted);
    else showResult("mail-test-result", false, reply.data.reason);
  }

  // A job started in another tab, or before this page was opened, changes what the form
  // shows: reload once when the strip sees it end.
  function onJob(event) {
    if (following) return;
    if (event.detail.active) jobWasActive = true;
    else if (jobWasActive) reload();
  }

  el("mail-security").addEventListener("change", followSecurity);
  el("mail-login").addEventListener("change", syncLogin);
  el("mail-form").addEventListener("submit", save);
  el("mail-probe").addEventListener("click", probe);
  el("mail-test-send").form.addEventListener("submit", sendTest);
  events.addEventListener("papaia-mail-job", onJob);
  syncLogin();
}

// The page: module scripts run once the document is parsed, so the form is there. A core
// without the relay renders neither the form nor the island, nor this module.
if (globalThis.document && document.getElementById("mail-init")) {
  const { actions, toaster } = pageCore();
  installMailPage(document, {
    init: readIsland("mail-init"),
    request: actions.ragRequest,
    show: toaster.show,
    pollJob: actions.pollJob,
    refreshStrip: () =>
      window.htmx.ajax("GET", "/partials/mail/job-status", { target: "#mail-job-status", swap: "outerHTML" }),
    events: window,
    reload: () => window.location.reload(),
    clock: window,
  });
}
