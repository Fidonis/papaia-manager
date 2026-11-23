// Notifications (notifications.html, at /mail): who is told when something needs attention, and
// what each recipient receives. The page carries no inline script and no event attribute
// (T-UI-03): it loads this module in the block `page_modules` of base.html, before Alpine
// starts. The module registers the two dialogs as Alpine components on `alpine:init` and binds
// the ticks of the matrix, the test buttons and the settings form. The recipients, the events
// and the sentences come from the data island `mgr-notifications`.
import { escapeHtml, fmt, fmtHtml } from "../core/format.js";
import { readIsland } from "../core/islands.js";
import { actions, showToast } from "../core/runtime.js";

const page = readIsland("mgr-notifications");
const NOTIFY = page.doc;
const texts = page.texts;
const NOTIFY_API = "/api/v1/notifications";
let notifyRevision = NOTIFY.revision;

// One request at a time: each change carries the revision the one before it produced, so
// two ticks in a row must not be in flight together (the second would be refused as stale).
let notifyChain = Promise.resolve();
function notifySerial(task) {
  const next = notifyChain.then(task);
  notifyChain = next.catch(function () {});
  return next;
}

function notifyReload(delay) {
  setTimeout(function () { window.location.reload(); }, delay || 700);
}

let savedTimer = null;
function flashSaved() {
  const el = document.getElementById("notify-saved");
  if (!el) return;
  el.textContent = texts.saved;
  clearTimeout(savedTimer);
  savedTimer = setTimeout(function () { el.textContent = ""; }, 1800);
}

function toggleEvent(box) {
  const row = box.closest("tr");
  const events = Array.prototype.map.call(
    row.querySelectorAll("input[data-event]:checked"), function (b) { return b.dataset.event; });
  return notifySerial(async function () {
    box.disabled = true;
    const res = await actions.ragRequest("PUT", NOTIFY_API + "/recipients/" + encodeURIComponent(row.dataset.recipient) + "/events",
      { revision: notifyRevision, events: events });
    box.disabled = false;
    if (res.handled) return;
    if (!res.ok) {
      box.checked = !box.checked;
      showToast("error", escapeHtml(res.error));
      return;
    }
    notifyRevision = res.data.revision;
    flashSaved();
  });
}

async function testRecipient(id, name, button) {
  button.disabled = true;
  const res = await actions.ragRequest("POST", NOTIFY_API + "/recipients/" + encodeURIComponent(id) + "/test");
  button.disabled = false;
  if (res.handled) return;
  if (!res.ok) { showToast("error", escapeHtml(res.error)); return; }
  if (res.data.ok) {
    showToast("success", fmtHtml(texts.testSent, { name: name }));
  } else {
    showToast("error", fmtHtml(texts.testFailed, { name: name, reason: res.data.reason }));
  }
  notifyReload(1200);
}

function saveSettings(event) {
  event.preventDefault();
  const button = document.getElementById("set-save");
  const number = function (id) { return Number(document.getElementById(id).value); };
  return notifySerial(async function () {
    button.disabled = true;
    const res = await actions.ragRequest("PUT", NOTIFY_API + "/settings", {
      revision: notifyRevision,
      check_interval_minutes: number("set-interval"),
      debounce_minutes: number("set-debounce"),
      cert_lead_days: number("set-lead"),
    });
    button.disabled = false;
    if (res.handled) return;
    if (!res.ok) { showToast("error", escapeHtml(res.error)); return; }
    notifyRevision = res.data.revision;
    showToast("success", escapeHtml(texts.settingsSaved));
  });
}

function recipientDialog() {
  return {
    mode: "create", id: "", kind: NOTIFY.mail.supported ? "email" : "webhook",
    name: "", target: "", maskedTarget: "", enabled: true, picked: {},
    events: NOTIFY.events, mailSupported: NOTIFY.mail.supported,
    busy: false, error: "",
    fmt: fmt,
    reset() {
      this.id = ""; this.name = ""; this.target = ""; this.maskedTarget = ""; this.enabled = true;
      this.kind = NOTIFY.mail.supported ? "email" : "webhook"; this.picked = {};
      this.error = ""; this.busy = false;
    },
    openNew() {
      this.reset(); this.mode = "create";
      this.$root.showModal();
    },
    openRecipient(detail) {
      const r = NOTIFY.recipients.find(function (x) { return x.id === detail.id; });
      if (!r) return;
      this.reset(); this.mode = "edit";
      this.id = r.id; this.kind = r.kind; this.name = r.name; this.enabled = r.enabled;
      // An address is shown as it is; a webhook URL is not sent back, so the field stays empty.
      this.target = r.kind === "email" ? r.target : "";
      this.maskedTarget = r.target;
      const picked = {};
      r.events.forEach(function (e) { picked[e] = true; });
      this.picked = picked;
      this.$root.showModal();
    },
    chosen() {
      const self = this;
      return this.events.map(function (e) { return e.id; }).filter(function (id) { return self.picked[id]; });
    },
    allChosen() { return this.chosen().length === this.events.length; },
    toggleAll() {
      const on = !this.allChosen();
      const picked = {};
      this.events.forEach(function (e) { picked[e.id] = on; });
      this.picked = picked;
    },
    async submit() {
      this.error = "";
      if (!this.name.trim()) { this.error = texts.enterName; return; }
      if (this.mode === "create" && !this.target.trim()) {
        this.error = this.kind === "email" ? texts.enterAddress : texts.enterUrl;
        return;
      }
      this.busy = true;
      const self = this;
      const create = this.mode === "create";
      const res = await notifySerial(function () {
        const body = { revision: notifyRevision, name: self.name.trim(), enabled: self.enabled, events: self.chosen() };
        if (create) {
          body.kind = self.kind;
          body.target = self.target.trim();
          return actions.ragRequest("POST", NOTIFY_API + "/recipients", body);
        }
        if (self.target.trim()) body.target = self.target.trim();
        return actions.ragRequest("PUT", NOTIFY_API + "/recipients/" + encodeURIComponent(self.id), body);
      });
      this.busy = false;
      if (res.handled) return;
      if (!res.ok) { this.error = res.error; return; }
      notifyRevision = res.data.revision;
      this.$root.close();
      showToast("success", fmtHtml(create ? texts.added : texts.changed, { name: this.name.trim() }));
      notifyReload(900);
    },
  };
}

function deleteRecipient() {
  return {
    id: "", name: "", busy: false, error: "",
    open(detail) {
      this.id = detail.id; this.name = detail.name; this.error = ""; this.busy = false;
      this.$root.showModal();
    },
    async submit() {
      this.error = "";
      this.busy = true;
      const self = this;
      const res = await notifySerial(function () {
        return actions.ragRequest("DELETE", NOTIFY_API + "/recipients/" + encodeURIComponent(self.id),
          { revision: notifyRevision });
      });
      this.busy = false;
      if (res.handled) return;
      if (!res.ok) { this.error = res.error; return; }
      notifyRevision = res.data.revision;
      this.$root.close();
      showToast("success", fmtHtml(texts.deleted, { name: this.name }));
      notifyReload(700);
    },
  };
}

document.addEventListener("alpine:init", () => {
  window.Alpine.data("recipientDialog", recipientDialog);
  window.Alpine.data("deleteRecipient", deleteRecipient);
});

// The ticks of the matrix and the test buttons of the rows, delegated from the document;
// the row names its recipient in `data-recipient`, the button its name in `data-name`.
document.addEventListener("change", (event) => {
  const box = event.target;
  if (box instanceof HTMLInputElement && box.matches("tr[data-recipient] input[data-event]")) toggleEvent(box);
});
document.addEventListener("click", (event) => {
  const button = event.target instanceof Element ? event.target.closest("[data-recipient-test]") : null;
  if (!button) return;
  const row = button.closest("tr[data-recipient]");
  if (row) testRecipient(row.dataset.recipient, button.dataset.name, button);
});
const settingsForm = document.getElementById("notify-settings");
if (settingsForm) settingsForm.addEventListener("submit", saveSettings);
