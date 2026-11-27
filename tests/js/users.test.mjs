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
  chatsCleared: "The chats of <b>{username}</b> were cleared.",
  chatsClearFailed: 'The chats of <b>{username}</b> could not be cleared. <a href="{href}">View log</a>',
  subThreadsOne: "including {count} sub-thread",
  subThreadsOther: "including {count} sub-threads",
  filesFailedOne: "{count} file could not be removed.",
  filesFailedOther: "{count} files could not be removed.",
};

function setup(answers = {}, { smtp = true } = {}) {
  const sent = [];
  const timers = [];
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
    later: (fn, ms) => timers.push([fn, ms]),
    lang: "en",
  });
  return { dialogs, sent, shown, events, timers, reloads: () => reloads };
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

// Clearing the chats of an account (the core's `librechat clear-history`): the dry run first,
// the deletion only from the dialog's button, confirmed, then the job until it ends.
const COUNTS = { conversations: 3, sub_threads: 1, messages: 40, files: 2, shared_links: 0, tool_calls: 0, files_failed: 0, empty: false };
const CHATS = "/api/v1/users/u%201/chat-history";

test("clearing chats shows the dry run, deletes only when confirmed and follows the job", async () => {
  const { dialogs, sent, shown, timers } = setup({
    [`GET ${CHATS}`]: { ok: true, data: { counts: COUNTS } },
    [`POST ${CHATS}/clear`]: { ok: true, data: { job_id: "j-5" } },
    [`GET ${CHATS}/jobs/j-5`]: { ok: true, data: { status: "running" } },
  });
  const dialog = mount(dialogs.clearChats());
  await dialog.open({ id: "u 1", username: "anna" });
  assert.equal(dialog.$root.open, true);
  assert.equal(dialog.phase, "confirm");
  assert.equal(dialog.counts.messages, 40);
  assert.equal(dialog.subThreads(), "including 1 sub-thread");
  assert.deepEqual(sent.map((r) => [r.method, r.url]), [["GET", CHATS]]);

  await dialog.submit();
  assert.deepEqual(sent[1], { method: "POST", url: `${CHATS}/clear`, body: { confirmed: true } });
  assert.equal(dialog.phase, "running");
  assert.equal(dialog.jobId, "j-5");
  // Still running: asked again later.
  assert.equal(timers.length, 1);
  assert.equal(timers[0][1], 2000);
  assert.deepEqual(shown, []);
});

test("a cleared history is told; a failed one keeps its counts and links the log", async () => {
  const done = setup({
    [`GET ${CHATS}`]: { ok: true, data: { counts: COUNTS } },
    [`POST ${CHATS}/clear`]: { ok: true, data: { job_id: "j-5" } },
    [`GET ${CHATS}/jobs/j-5`]: { ok: true, data: { status: "succeeded", counts: { ...COUNTS, files_failed: 2 } } },
  });
  const ok = mount(done.dialogs.clearChats());
  await ok.open({ id: "u 1", username: "anna" });
  await ok.submit();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ok.phase, "done");
  assert.equal(ok.filesFailed(), "2 files could not be removed.");
  assert.deepEqual(done.shown, [["success", "The chats of <b>anna</b> were cleared."]]);

  const bad = setup({
    [`GET ${CHATS}`]: { ok: true, data: { counts: COUNTS } },
    [`POST ${CHATS}/clear`]: { ok: true, data: { job_id: "j-6" } },
    [`GET ${CHATS}/jobs/j-6`]: { ok: true, data: { status: "failed", error: "LibreChat is not running." } },
  });
  const failed = mount(bad.dialogs.clearChats());
  await failed.open({ id: "u 1", username: "<b>x</b>" });
  await failed.submit();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(failed.phase, "failed");
  assert.equal(failed.error, "LibreChat is not running.");
  assert.equal(failed.counts.messages, 40);
  assert.equal(bad.shown[0][0], "error");
  assert.ok(bad.shown[0][1].includes("&lt;b&gt;x&lt;/b&gt;"));
  assert.ok(bad.shown[0][1].includes('href="/jobs/j-6"'));
});

test("a refused dry run or deletion is shown in the dialog, and nothing is deleted", async () => {
  const { dialogs, sent } = setup({
    [`GET ${CHATS}`]: { ok: false, error: "This account has no email address." },
  });
  const dialog = mount(dialogs.clearChats());
  await dialog.open({ id: "u 1", username: "anna" });
  assert.equal(dialog.phase, "failed");
  assert.equal(dialog.error, "This account has no email address.");
  assert.equal(sent.length, 1);

  const refused = setup({
    [`GET ${CHATS}`]: { ok: true, data: { counts: COUNTS } },
    [`POST ${CHATS}/clear`]: { ok: false, error: "Busy." },
  });
  const other = mount(refused.dialogs.clearChats());
  await other.open({ id: "u 1", username: "anna" });
  await other.submit();
  assert.equal(other.phase, "confirm");
  assert.equal(other.error, "Busy.");
  assert.equal(other.busy, false);
});

test("a dialog opened again ignores the answer meant for the one before", async () => {
  let release;
  const { dialogs } = setup({
    [`GET ${CHATS}`]: new Promise((resolve) => { release = resolve; }),
    ["GET /api/v1/users/u2/chat-history"]: { ok: true, data: { counts: { ...COUNTS, messages: 7 } } },
  });
  const dialog = mount(dialogs.clearChats());
  const first = dialog.open({ id: "u 1", username: "anna" });
  await dialog.open({ id: "u2", username: "ben" });
  release({ ok: true, data: { counts: COUNTS } });
  await first;
  assert.equal(dialog.username, "ben");
  assert.equal(dialog.counts.messages, 7);
});
