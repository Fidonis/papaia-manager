// The dialogs of the Roles page (roles.html), as Alpine components: create, change or look at
// a role, and delete one. Built by createRoleDialogs() from what they talk to, so the tests can
// hand in stand-ins; pages/roles.js registers them with Alpine.
//
// `request` is the JSON request of the shared actions (core/actions.js, ragRequest), `toast`
// shows a message that carries markup, `reload` reloads the list, `texts` are the sentences of
// the page's data island, `lang` the language of the page for the plural of a count. The
// values the dialogs show inside a sentence (`clientMembersList`, `heldNumber`, ...) are
// methods, because the sentence is rendered by the server and binds them as text.
import { fmtHtml } from "../core/format.js";

const ROLES_API = "/api/v1/roles";
// How many holders the delete dialog names before it counts the rest.
const SHOWN_USERS = 12;

export function createRoleDialogs({ request, toast, reload, texts, lang }) {
  function roleDialog() {
    return {
      mode: "create", name: "", description: "", builtIn: false, all: [], selected: {}, initial: {},
      initialDescription: "", clientMembers: [],
      loading: false, loadError: "", busy: false, error: "",
      reset() {
        this.name = ""; this.description = ""; this.builtIn = false; this.all = [];
        this.selected = {}; this.initial = {}; this.initialDescription = ""; this.clientMembers = [];
        this.loadError = ""; this.error = "";
      },
      // Every other role, as something this one can contain.
      candidates() {
        const self = this;
        return this.all.filter(function (role) { return role.name !== self.name; });
      },
      clientMembersList() { return this.clientMembers.join(", "); },
      async load(open) {
        this.loading = true;
        const res = await request("GET", ROLES_API);
        this.loading = false;
        if (res.handled) return;
        if (!res.ok) { this.loadError = res.error; return; }
        this.all = res.data.roles;
        const picked = {};
        this.all.forEach(function (role) { picked[role.name] = false; });
        if (open) {
          const row = this.all.find(function (role) { return role.name === open; });
          if (!row) { this.loadError = texts.roleGone; return; }
          this.description = row.description;
          this.builtIn = row.built_in;
          this.clientMembers = row.client_members;
          row.members.forEach(function (member) { picked[member] = true; });
          this.initialDescription = row.description;
        }
        this.selected = Object.assign({}, picked);
        this.initial = Object.assign({}, picked);
      },
      async openNew() {
        this.reset(); this.mode = "create";
        this.$root.showModal();
        await this.load(null);
      },
      async openRole(detail) {
        this.reset(); this.name = detail.name;
        this.mode = detail.builtIn ? "view" : "edit";
        this.builtIn = !!detail.builtIn;
        this.$root.showModal();
        await this.load(detail.name);
      },
      chosen() {
        const self = this;
        return this.candidates().filter(function (r) { return self.selected[r.name]; })
                                .map(function (r) { return r.name; });
      },
      dirty() {
        const self = this;
        return this.description.trim() !== this.initialDescription.trim() ||
          this.candidates().some(function (r) { return !!self.selected[r.name] !== !!self.initial[r.name]; });
      },
      async submit() {
        this.error = "";
        this.busy = true;
        const create = this.mode === "create";
        const name = this.name.trim();
        const res = create
          ? await request("POST", ROLES_API,
              { name: name, description: this.description.trim(), members: this.chosen() })
          : await request("PUT", ROLES_API + "/" + encodeURIComponent(name),
              { description: this.description.trim(), members: this.chosen() });
        this.busy = false;
        if (res.handled) return;
        if (!res.ok) {
          this.error = res.error;
          // A step after creating the role may have failed: the role is there.
          if (create) reload();
          return;
        }
        this.$root.close();
        toast("success", fmtHtml(create ? texts.roleCreated : texts.roleSaved, { name: name }));
        reload();
      },
    };
  }

  function deleteRole() {
    return {
      name: "", users: [], more: false, typed: "",
      loading: false, loadError: "", busy: false, error: "",
      async open(detail) {
        this.name = detail.name; this.users = []; this.more = false; this.typed = "";
        this.loadError = ""; this.error = "";
        this.$root.showModal();
        this.loading = true;
        const res = await request("GET", ROLES_API + "/" + encodeURIComponent(this.name));
        this.loading = false;
        if (res.handled) return;
        if (!res.ok) { this.loadError = res.error; return; }
        this.users = res.data.users;
        this.more = res.data.users_more;
      },
      // The sentence "Held directly by {number} account(s)" in the plural the count needs.
      heldOne() { return new Intl.PluralRules(lang).select(this.users.length) === "one"; },
      heldNumber() { return this.users.length + this.plus(); },
      shownUsers() { return this.users.slice(0, SHOWN_USERS); },
      moreCount() { return this.users.length - SHOWN_USERS; },
      plus() { return this.more ? "+" : ""; },
      async submit() {
        this.error = "";
        this.busy = true;
        const res = await request("DELETE", ROLES_API + "/" + encodeURIComponent(this.name));
        this.busy = false;
        if (res.handled) return;
        if (!res.ok) { this.error = res.error; return; }
        this.$root.close();
        toast("success", fmtHtml(texts.roleDeleted, { name: this.name }));
        reload();
      },
    };
  }

  return { roleDialog, deleteRole };
}
