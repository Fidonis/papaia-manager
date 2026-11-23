// The Mail page (pages/mail.js) against a stand-in document: the listeners the module adds in
// place of the former on* attributes, the values it sends, the results it shows as text and
// the job it follows. The module reaches nothing global; everything comes in as an argument.
import assert from "node:assert/strict";
import { test } from "node:test";

import { installMailPage } from "../../src/app/static/js/pages/mail.js";

const TEXTS = {
  applying: "Applying the mail settings …",
  saving: "Saving the mail settings …",
  applied: "Mail settings applied",
  saved: "Mail settings saved",
  applyFailed: "Applying the mail settings failed. The log is above.",
  connecting: "Connecting …",
  loginAccepted: "login accepted",
  loginNotNeeded: "no login needed",
  answered: "The mail server answered ({login}). No mail was sent.",
  stopped: "Stopped at {stage}: {reason}",
  accepted: "The relay accepted the message. It may take a moment to arrive; the status above shows it while it waits.",
};

class ClassList {
  constructor(names) {
    this.names = new Set(names);
  }
  add(...names) {
    names.forEach((n) => this.names.add(n));
  }
  remove(...names) {
    names.forEach((n) => this.names.delete(n));
  }
  toggle(name, on) {
    if (on) this.names.add(name);
    else this.names.delete(name);
  }
  contains(name) {
    return this.names.has(name);
  }
}

function element(props = {}) {
  const listeners = {};
  return {
    value: "",
    checked: false,
    disabled: false,
    dataset: {},
    textContent: "",
    classList: new ClassList(props.classes || []),
    listeners,
    addEventListener(name, listener) {
      (listeners[name] ||= []).push(listener);
    },
    fire(name, event = {}) {
      (listeners[name] || []).forEach((l) => l(event));
    },
    ...props,
  };
}

function page({ enabled = true, answer = () => ({ ok: true, data: {} }) } = {}) {
  const testForm = element();
  const els = {
    "mail-form": element(),
    "mail-host": element({ value: " smtp.example.org " }),
    "mail-security": element({ value: "starttls" }),
    "mail-port": element({ value: "587", dataset: { mode: "starttls" } }),
    "mail-login": element({ checked: true }),
    "mail-user": element({ value: " mailer " }),
    "mail-password": element({ value: "" }),
    "mail-from": element({ value: " noreply@example.org " }),
    "mail-from-name": element({ value: " Our Mail " }),
    "mail-probe-result": element({ classes: ["hidden", "rounded-lg"] }),
    "mail-save": element(),
    "mail-probe": element(),
    "mail-test-to": element({ value: " you@example.org " }),
    "mail-test-send": element({ form: testForm }),
    "mail-test-result": element({ classes: ["hidden", "text-sm"] }),
  };
  const doc = { getElementById: (id) => els[id] || null };
  const sent = [];
  const shown = [];
  const polled = [];
  const timers = [];
  const events = {};
  let strips = 0;
  let reloads = 0;
  installMailPage(doc, {
    init: { mail: { revision: "rev-7", enabled }, default_ports: { starttls: 587, tls: 465, none: 25 }, texts: TEXTS },
    request: async (method, url, body) => {
      sent.push([method, url, body]);
      return answer(method, url, body);
    },
    show: (type, html) => shown.push([type, html]),
    pollJob: (id, onDone) => polled.push([id, onDone]),
    refreshStrip: () => strips++,
    events: { addEventListener: (name, listener) => (events[name] = listener) },
    reload: () => reloads++,
    clock: { setTimeout: (fn, ms) => timers.push([fn, ms]) },
  });
  return { els, testForm, sent, shown, polled, timers, events, strips: () => strips, reloads: () => reloads };
}

const submit = () => {
  const event = { prevented: false, preventDefault: () => (event.prevented = true) };
  return event;
};

test("the listeners replace the former attributes", () => {
  const { els, testForm, events } = page();
  assert.equal(els["mail-form"].listeners.submit.length, 1);
  assert.equal(els["mail-login"].listeners.change.length, 1);
  assert.equal(els["mail-security"].listeners.change.length, 1);
  assert.equal(els["mail-probe"].listeners.click.length, 1);
  assert.equal(testForm.listeners.submit.length, 1);
  assert.deepEqual(Object.keys(events), ["papaia-mail-job"]);
});

test("without encryption there is no login; with it the fields follow the box", () => {
  const { els } = page();
  assert.equal(els["mail-user"].disabled, false);
  els["mail-security"].value = "none";
  els["mail-security"].fire("change");
  assert.equal(els["mail-login"].checked, false);
  assert.equal(els["mail-login"].disabled, true);
  assert.equal(els["mail-user"].disabled, true);
  assert.equal(els["mail-password"].disabled, true);
  els["mail-security"].value = "tls";
  els["mail-security"].fire("change");
  assert.equal(els["mail-login"].disabled, false);
  els["mail-login"].checked = true;
  els["mail-login"].fire("change");
  assert.equal(els["mail-user"].disabled, false);
  assert.equal(els["mail-password"].disabled, false);
});

test("the port follows the mode unless one was typed on purpose", () => {
  const { els } = page();
  els["mail-security"].value = "tls";
  els["mail-security"].fire("change");
  assert.equal(els["mail-port"].value, 465);
  assert.equal(els["mail-port"].dataset.mode, "tls");
  els["mail-port"].value = "2525";
  els["mail-security"].value = "starttls";
  els["mail-security"].fire("change");
  assert.equal(els["mail-port"].value, "2525");
});

test("save sends the form against the revision of the page and follows the job", async () => {
  const t = page({ answer: () => ({ ok: true, data: { job_id: "job-9" } }) });
  t.els["mail-password"].value = "typed-in-pass";
  const event = submit();
  await t.els["mail-form"].listeners.submit[0](event);
  assert.equal(event.prevented, true);
  assert.deepEqual(t.sent, [[
    "PUT",
    "/api/v1/mail",
    {
      revision: "rev-7",
      from_address: "noreply@example.org",
      from_name: "Our Mail",
      host: "smtp.example.org",
      port: 587,
      security: "starttls",
      user: "mailer",
      password: "typed-in-pass",
    },
  ]]);
  assert.equal(t.els["mail-password"].value, "");
  assert.deepEqual(t.shown, [["info", "Applying the mail settings …"]]);
  assert.equal(t.strips(), 1);
  assert.equal(t.polled[0][0], "job-9");
  t.polled[0][1]({ status: "succeeded" });
  assert.deepEqual(t.shown.at(-1), ["success", "Mail settings applied"]);
  assert.equal(t.timers[0][1], 900);
  t.timers[0][0]();
  assert.equal(t.reloads(), 1);
});

test("a relay that is off is saved, not applied; a failed job is told", async () => {
  const t = page({ enabled: false, answer: () => ({ ok: true, data: { job_id: "job-10" } }) });
  t.els["mail-login"].checked = false;
  await t.els["mail-form"].listeners.submit[0](submit());
  const body = t.sent[0][2];
  assert.equal(body.user, "");
  assert.equal("password" in body, false);
  assert.deepEqual(t.shown, [["info", "Saving the mail settings …"]]);
  t.polled[0][1]({ status: "failed" });
  assert.deepEqual(t.shown.at(-1), ["error", "Applying the mail settings failed. The log is above."]);
});

test("a refused save frees the button and tells the reason escaped", async () => {
  const t = page({ answer: () => ({ ok: false, error: "port <0>" }) });
  await t.els["mail-form"].listeners.submit[0](submit());
  assert.equal(t.els["mail-save"].disabled, false);
  assert.deepEqual(t.shown, [["error", "port &lt;0&gt;"]]);
  assert.deepEqual(t.polled, []);
});

test("the probe shows its outcome as text in the result box", async () => {
  let answer = { ok: true, data: { ok: true, login: "ok" } };
  const t = page({ answer: () => answer });
  await t.els["mail-probe"].listeners.click[0]();
  const box = t.els["mail-probe-result"];
  assert.deepEqual(t.sent[0], ["POST", "/api/v1/mail/probe", { host: "smtp.example.org", port: 587, security: "starttls", user: "mailer" }]);
  assert.equal(box.textContent, "The mail server answered (login accepted). No mail was sent.");
  assert.equal(box.classList.contains("hidden"), false);
  assert.equal(box.classList.contains("border-success"), true);
  assert.equal(t.els["mail-probe"].disabled, false);
  answer = { ok: true, data: { ok: false, stage: "tls", reason: "<certificate> refused" } };
  await t.els["mail-probe"].listeners.click[0]();
  assert.equal(box.textContent, "Stopped at tls: <certificate> refused");
  assert.equal(box.classList.contains("border-error"), true);
  assert.equal(box.classList.contains("border-success"), false);
  answer = { ok: false, error: "refused" };
  await t.els["mail-probe"].listeners.click[0]();
  assert.equal(box.textContent, "refused");
});

test("a test mail is sent to the address typed and its outcome shown as text", async () => {
  let answer = { ok: true, data: { ok: true } };
  const t = page({ answer: () => answer });
  const event = submit();
  await t.testForm.listeners.submit[0](event);
  assert.equal(event.prevented, true);
  assert.deepEqual(t.sent[0], ["POST", "/api/v1/mail/test-message", { to: "you@example.org" }]);
  const result = t.els["mail-test-result"];
  assert.equal(result.textContent, TEXTS.accepted);
  assert.equal(result.classList.contains("hidden"), false);
  assert.equal(result.classList.contains("text-error"), false);
  answer = { ok: true, data: { ok: false, reason: "550 <no such user>" } };
  await t.testForm.listeners.submit[0](submit());
  assert.equal(result.textContent, "550 <no such user>");
  assert.equal(result.classList.contains("text-error"), true);
  assert.equal(t.els["mail-test-send"].disabled, false);
});

test("a mail job that ends elsewhere reloads the page once it was seen running", async () => {
  const t = page({ answer: () => ({ ok: true, data: { job_id: "job-11" } }) });
  t.events["papaia-mail-job"]({ detail: { active: false } });
  t.events["papaia-mail-job"]({ detail: { active: true } });
  assert.equal(t.reloads(), 0);
  t.events["papaia-mail-job"]({ detail: { active: false } });
  assert.equal(t.reloads(), 1);
});

test("while the page follows its own job, the strip's announcements are left alone", async () => {
  const t = page({ answer: () => ({ ok: true, data: { job_id: "job-12" } }) });
  await t.els["mail-form"].listeners.submit[0](submit());
  t.events["papaia-mail-job"]({ detail: { active: true } });
  t.events["papaia-mail-job"]({ detail: { active: false } });
  assert.equal(t.reloads(), 0);
});
