// The dialogs of the Roles page (pages/roles-dialogs.js) against stand-ins: creating, changing
// and deleting a role, and the values the page shows as text instead of through x-html.
import assert from "node:assert/strict";
import { test } from "node:test";

import { createRoleDialogs } from "../../src/app/static/js/pages/roles-dialogs.js";

const TEXTS = {
  roleGone: "That role no longer exists.",
  roleCreated: "Role <b>{name}</b> created.",
  roleSaved: "Role <b>{name}</b> saved.",
  roleDeleted: "Role <b>{name}</b> deleted.",
};

const ALL = { ok: true, data: { roles: [
  { name: "sales", description: "Sales", built_in: false, members: ["user"], client_members: ["view-users", "query-users"] },
  { name: "user", description: "", built_in: true, members: [], client_members: [] },
] } };  // prettier-ignore

function setup(answers = {}) {
  const sent = [];
  const shown = [];
  let reloads = 0;
  const dialogs = createRoleDialogs({
    request: async (method, url, body) => {
      sent.push({ method, url, body });
      const answer = answers[`${method} ${url}`];
      return answer === undefined ? { ok: true, data: null } : answer;
    },
    toast: (type, html) => shown.push([type, html]),
    reload: () => {
      reloads += 1;
    },
    texts: TEXTS,
    lang: "en",
  });
  return { dialogs, sent, shown, reloads: () => reloads };
}

function mount(component) {
  const root = {
    open: false,
    showModal() {
      this.open = true;
    },
    close() {
      this.open = false;
    },
  };
  return Object.assign(component, { $root: root });
}

test("a new role offers every other role and sends what was chosen", async () => {
  const { dialogs, sent, shown, reloads } = setup({ "GET /api/v1/roles": ALL });
  const dialog = mount(dialogs.roleDialog());
  await dialog.openNew();
  assert.equal(dialog.mode, "create");
  dialog.name = " team ";
  dialog.description = "Team ";
  assert.deepEqual(dialog.candidates().map((r) => r.name), ["sales", "user"]);
  dialog.selected.user = true;
  await dialog.submit();
  assert.deepEqual(sent.at(-1), {
    method: "POST", url: "/api/v1/roles", body: { name: "team", description: "Team", members: ["user"] },
  });  // prettier-ignore
  assert.deepEqual(shown, [["success", "Role <b>team</b> created."]]);
  assert.equal(reloads(), 1);
});

test("an open role shows what it contains and saves only a change", async () => {
  const { dialogs, sent, shown } = setup({ "GET /api/v1/roles": ALL });
  const dialog = mount(dialogs.roleDialog());
  await dialog.openRole({ name: "sales", builtIn: false });
  assert.equal(dialog.mode, "edit");
  assert.deepEqual(dialog.candidates().map((r) => r.name), ["user"]);
  assert.equal(dialog.selected.user, true);
  assert.equal(dialog.dirty(), false);
  assert.equal(dialog.clientMembersList(), "view-users, query-users");
  dialog.description = "Sales team";
  assert.equal(dialog.dirty(), true);
  await dialog.submit();
  assert.deepEqual(sent.at(-1), {
    method: "PUT", url: "/api/v1/roles/sales", body: { description: "Sales team", members: ["user"] },
  });  // prettier-ignore
  assert.deepEqual(shown, [["success", "Role <b>sales</b> saved."]]);
});

test("a role that is gone says so", async () => {
  const { dialogs } = setup({ "GET /api/v1/roles": ALL });
  const dialog = mount(dialogs.roleDialog());
  await dialog.openRole({ name: "nope", builtIn: false });
  assert.equal(dialog.loadError, "That role no longer exists.");
});

test("deleting a role names its holders and sends the delete", async () => {
  const users = Array.from({ length: 14 }, (_, i) => `u${i}`);
  const { dialogs, sent, shown, reloads } = setup({
    "GET /api/v1/roles/sales": { ok: true, data: { users, users_more: true } },
  });
  const dialog = mount(dialogs.deleteRole());
  await dialog.open({ name: "sales" });
  assert.equal(dialog.heldOne(), false);
  assert.equal(dialog.heldNumber(), "14+");
  assert.deepEqual(dialog.shownUsers(), users.slice(0, 12));
  assert.equal(dialog.moreCount(), 2);
  assert.equal(dialog.plus(), "+");
  await dialog.submit();
  assert.deepEqual(sent.at(-1), { method: "DELETE", url: "/api/v1/roles/sales", body: undefined });
  assert.deepEqual(shown, [["success", "Role <b>sales</b> deleted."]]);
  assert.equal(reloads(), 1);
});

test("one holder is told in the singular", async () => {
  const { dialogs } = setup({
    "GET /api/v1/roles/sales": { ok: true, data: { users: ["anna"], users_more: false } },
  });
  const dialog = mount(dialogs.deleteRole());
  await dialog.open({ name: "sales" });
  assert.equal(dialog.heldOne(), true);
  assert.equal(dialog.heldNumber(), "1");
  assert.equal(dialog.plus(), "");
});
