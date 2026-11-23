// The dialogs of the Users page (pages/users-dialogs.js) against stand-ins for the request,
// the toaster, the list and the window events: what each sends, what it tells, and that the
// temporary password leaves nothing behind.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createUserDialogs,
  usersAgo,
  usersWhen,
} from "../../src/app/static/js/pages/users-dialogs.js";

const TEXTS = {
  justNow: "just now",
  mailNoServer: "No mail server.",
  mailNoAddress: "No address.",
  userCreatedLinkSent: "User <b>{username}</b> created. Link sent.",
  userCreatedMailFailed: "User <b>{username}</b> was created, but the email was not sent:",
  userCreated: "User <b>{username}</b> created.",
  rolesSaved: "Roles of <b>{username}</b> saved.",
  sessionEnded: "Session ended.",
  sessionsEnded: "All sessions of <b>{username}</b> ended.",
  linkSent: "A link was sent to <b>{username}</b>.",
  accountEnabled: "Account <b>{username}</b> enabled.",
  accountDisabled: "Account <b>{username}</b> disabled.",
};

function setup(answers = {}, { smtp = true } = {}) {
  const sent = [];
  const shown = [];
  const events = [];
  let reloads = 0;
  const dialogs = createUserDialogs({
    request: async (method, url, body) => {
      sent.push({ method, url, body });
      const answer = answers[`${method} ${url}`];
      return answer === undefined ? { ok: true, data: null } : answer;
    },
    toast: (type, html) => shown.push([type, html]),
    reload: () => {
      reloads += 1;
    },
    announce: (name, detail) => events.push([name, detail]),
    smtp: () => smtp,
    texts: TEXTS,
    copyText: async () => {},
    now: () => Date.parse("2026-11-23T12:00:00Z"),
  });
  return { dialogs, sent, shown, events, reloads: () => reloads };
}

// A component as Alpine would hold it, with a dialog that records whether it is open.
function mount(component) {
  const root = {
    open: false,
    showModal() {
      this.open = true;
    },
    close() {
      this.open = false;
    },
    querySelector: () => ({ select() {} }),
  };
  return Object.assign(component, { $root: root });
}

const ROLES = { ok: true, data: { roles: [
  { name: "user", default: true, description: "" },
  { name: "sales", default: false, description: "Sales" },
] } };  // prettier-ignore

test("a new user starts with the dashboard role ticked and sends what was chosen", async () => {
  const { dialogs, sent, shown, reloads } = setup({
    "GET /api/v1/users/roles": ROLES,
    "POST /api/v1/users": { ok: true, data: { username: "jane", email_sent: false } },
  });
  const dialog = mount(dialogs.newUser());
  await dialog.open();
  assert.equal(dialog.$root.open, true);
  assert.deepEqual(dialog.selected, { user: true, sales: false });
  dialog.username = " jane ";
  dialog.email = "jane@example.test";
  dialog.credential = "none";
  dialog.selected.sales = true;
  await dialog.submit();
  assert.deepEqual(sent.at(-1), {
    method: "POST",
    url: "/api/v1/users",
    body: {
      username: "jane", email: "jane@example.test", first_name: "", last_name: "",
      credential: "none", roles: ["user", "sales"],
    },
  });  // prettier-ignore
  assert.equal(dialog.$root.open, false);
  assert.equal(reloads(), 1);
  assert.deepEqual(shown, [["success", "User <b>jane</b> created."]]);
});

test("a temporary password goes to the secret dialog, not into a toast", async () => {
  const { dialogs, shown, events } = setup({
    "GET /api/v1/users/roles": ROLES,
    "POST /api/v1/users": { ok: true, data: { username: "jane", temporary_password: "pw-1" } },
  });
  const dialog = mount(dialogs.newUser());
  await dialog.open();
  dialog.username = "jane";
  await dialog.submit();
  assert.deepEqual(events, [["user-secret", { username: "jane", password: "pw-1", created: true }]]);
  assert.deepEqual(shown, []);
});

test("a mail that was not sent is told with the server's reason escaped", async () => {
  const { dialogs, shown } = setup({
    "GET /api/v1/users/roles": ROLES,
    "POST /api/v1/users": { ok: true, data: { username: "jane", email_error: "<smtp> down" } },
  });
  const dialog = mount(dialogs.newUser());
  await dialog.open();
  dialog.username = "jane";
  await dialog.submit();
  assert.deepEqual(shown, [
    ["warning", "User <b>jane</b> was created, but the email was not sent: &lt;smtp&gt; down"],
  ]);
});

test("a refused new user keeps the dialog open and reloads the list", async () => {
  const { dialogs, reloads } = setup({
    "GET /api/v1/users/roles": ROLES,
    "POST /api/v1/users": { ok: false, error: "taken" },
  });
  const dialog = mount(dialogs.newUser());
  await dialog.open();
  await dialog.submit();
  assert.equal(dialog.error, "taken");
  assert.equal(dialog.$root.open, true);
  assert.equal(reloads(), 1);
});

test("an email link needs a mail server and an address", async () => {
  const off = mount(setup({ "GET /api/v1/users/roles": ROLES }, { smtp: false }).dialogs.newUser());
  await off.open();
  off.email = "jane@example.test";
  assert.equal(off.canMail(), false);
  assert.equal(off.mailBlocked(), "No mail server.");
  const on = mount(setup({ "GET /api/v1/users/roles": ROLES }).dialogs.newUser());
  await on.open();
  assert.equal(on.mailBlocked(), "No address.");
  on.email = "jane@example.test";
  assert.equal(on.canMail(), true);
  assert.equal(on.mailBlocked(), "");
});

test("the secret dialog forgets the password when it closes", () => {
  const { dialogs } = setup();
  const dialog = mount(dialogs.secretDialog());
  dialog.open({ username: "jane", password: "pw-1", created: true });
  assert.equal(dialog.$root.open, true);
  assert.equal(dialog.password, "pw-1");
  dialog.clear();
  assert.equal(dialog.password, "");
  assert.equal(dialog.username, "");
});

test("the roles of a user are saved only as they were changed", async () => {
  const { dialogs, sent, shown, reloads } = setup({
    "GET /api/v1/users/u%2F1/roles": { ok: true, data: { roles: [
      { name: "user", direct: true }, { name: "sales", direct: false },
    ] } },  // prettier-ignore
  });
  const dialog = mount(dialogs.editUserRoles());
  await dialog.open({ id: "u/1", username: "ben" });
  assert.equal(dialog.dirty(), false);
  dialog.selected.sales = true;
  assert.equal(dialog.dirty(), true);
  await dialog.submit();
  assert.deepEqual(sent.at(-1), {
    method: "PUT", url: "/api/v1/users/u%2F1/roles", body: { roles: ["user", "sales"] },
  });  // prettier-ignore
  assert.deepEqual(shown, [["success", "Roles of <b>ben</b> saved."]]);
  assert.equal(reloads(), 1);
});

test("disabling an account sends enabled false and says so", async () => {
  const { dialogs, sent, shown } = setup();
  const dialog = mount(dialogs.toggleUser());
  dialog.open({ id: "u-2", username: "<ben>", enabled: true });
  assert.equal(dialog.enable, false);
  await dialog.submit();
  assert.deepEqual(sent, [{ method: "PUT", url: "/api/v1/users/u-2/enabled", body: { enabled: false } }]);
  assert.deepEqual(shown, [["success", "Account <b>&lt;ben&gt;</b> disabled."]]);
});

test("sessions are listed, ended one by one and all at once", async () => {
  const list = { ok: true, data: { sessions: [{ id: "s1", clients: [] }], is_self: false } };
  const { dialogs, sent, shown } = setup({ "GET /api/v1/users/u-2/sessions": list });
  const dialog = mount(dialogs.userSessions());
  await dialog.open({ id: "u-2", username: "ben" });
  assert.equal(dialog.sessions.length, 1);
  await dialog.end("s/1");
  assert.equal(sent[1].url, "/api/v1/users/u-2/sessions/s%2F1");
  await dialog.endAll();
  assert.deepEqual(shown, [
    ["success", "Session ended."],
    ["success", "All sessions of <b>ben</b> ended."],
  ]);
});

test("a temporary password reset hands the password to the secret dialog", async () => {
  const { dialogs, events, reloads } = setup({
    "POST /api/v1/users/u-2/password/temporary": {
      ok: true, data: { username: "ben", temporary_password: "pw-2" },
    },
  });  // prettier-ignore
  const dialog = mount(dialogs.resetPassword());
  dialog.open({ id: "u-2", username: "ben", email: "" });
  assert.equal(dialog.canMail(), false);
  await dialog.temporary();
  assert.deepEqual(events, [["user-secret", { username: "ben", password: "pw-2", created: false }]]);
  assert.equal(reloads(), 1);
});

test("a handled failure changes nothing on the dialog", async () => {
  const { dialogs, shown } = setup({ "PUT /api/v1/users/u-2/enabled": { ok: false, handled: true } });
  const dialog = mount(dialogs.toggleUser());
  dialog.open({ id: "u-2", username: "ben", enabled: false });
  await dialog.submit();
  assert.equal(dialog.error, "");
  assert.equal(dialog.$root.open, true);
  assert.deepEqual(shown, []);
});

test("times are told in the reader's words", () => {
  const now = Date.parse("2026-11-23T12:00:00Z");
  assert.equal(usersWhen(""), "—");
  assert.equal(usersAgo(null, now, "just now"), "—");
  assert.equal(usersAgo("2026-11-23T11:59:50Z", now, "just now"), "just now");
  // In the language of the browser, whatever it is where the tests run.
  const twoHours = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(-2, "hour");
  assert.equal(usersAgo("2026-11-23T10:00:00Z", now, "just now"), twoHours);
  assert.notEqual(usersWhen("2026-11-23T10:00:00Z"), "—");
});
