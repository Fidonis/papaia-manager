// The dialogs of the Users page (users.html), as Alpine components: create an account, show a
// temporary password once, change the roles of an account, list and end its sessions, reset
// its password, enable or disable it. Built by createUserDialogs() from what they talk to, so
// the tests can hand in stand-ins; pages/users.js registers them with Alpine.
//
// `request` is the JSON request of the shared actions (core/actions.js, ragRequest), `toast`
// shows a message that carries markup, `reload` reloads the list, `announce` sends a window
// event to another dialog, `smtp` tells whether the realm can send mail, `texts` are the
// sentences of the page's data island in the language of the page.
import { escapeHtml, fmtHtml } from "../core/format.js";

const USERS_API = "/api/v1/users";

export function usersWhen(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function usersAgo(iso, now, justNow) {
  if (!iso) return "—";
  const seconds = Math.round((new Date(iso).getTime() - now) / 1000);
  const units = [["day", 86400], ["hour", 3600], ["minute", 60]];
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size) return rtf.format(Math.round(seconds / size), unit);
  }
  return justNow;
}

export function createUserDialogs({ request, toast, reload, announce, smtp, texts, copyText, now }) {
  const userUrl = (id, rest) => USERS_API + "/" + encodeURIComponent(id) + (rest || "");

  function mailBlocked(hasSmtp, email) {
    if (!hasSmtp) return texts.mailNoServer;
    if (!email) return texts.mailNoAddress;
    return "";
  }

  function newUser() {
    return {
      username: "", email: "", firstName: "", lastName: "", credential: "temporary",
      roles: [], selected: {}, rolesLoading: false, rolesError: "",
      smtp: false, busy: false, error: "",
      async open() {
        this.reset();
        this.smtp = smtp();
        this.$root.showModal();
        this.rolesLoading = true;
        const res = await request("GET", USERS_API + "/roles");
        this.rolesLoading = false;
        if (res.handled) return;
        if (!res.ok) { this.rolesError = res.error; return; }
        this.roles = res.data.roles;
        // The dashboard role is what makes a new account usable, so it starts ticked.
        const picked = {};
        this.roles.forEach(function (role) { picked[role.name] = role.default; });
        this.selected = picked;
      },
      reset() {
        this.username = ""; this.email = ""; this.firstName = ""; this.lastName = "";
        this.credential = "temporary"; this.error = "";
        this.roles = []; this.selected = {}; this.rolesError = "";
      },
      chosen() {
        const self = this;
        return this.roles.filter(function (r) { return self.selected[r.name]; })
                         .map(function (r) { return r.name; });
      },
      canMail() { return this.smtp && this.email.trim() !== ""; },
      mailBlocked() { return mailBlocked(this.smtp, this.email.trim()); },
      async submit() {
        this.error = "";
        if (this.credential === "email" && !this.canMail()) this.credential = "temporary";
        this.busy = true;
        const name = this.username.trim();
        const res = await request("POST", USERS_API, {
          username: name,
          email: this.email.trim(),
          first_name: this.firstName.trim(),
          last_name: this.lastName.trim(),
          credential: this.credential,
          roles: this.chosen(),
        });
        this.busy = false;
        if (res.handled) return;
        if (!res.ok) {
          this.error = res.error;
          // A step after creating the account may have failed: the account is there.
          reload();
          return;
        }
        this.$root.close();
        reload();
        const made = res.data;
        if (made.temporary_password) {
          announce("user-secret", { username: made.username, password: made.temporary_password, created: true });
        } else if (made.email_sent) {
          toast("success", fmtHtml(texts.userCreatedLinkSent, { username: made.username }));
        } else if (made.email_error) {
          toast("warning", fmtHtml(texts.userCreatedMailFailed, { username: made.username }) + " " + escapeHtml(made.email_error));
        } else {
          toast("success", fmtHtml(texts.userCreated, { username: made.username }));
        }
      },
    };
  }

  function secretDialog() {
    return {
      username: "", password: "", created: false, copied: false,
      open(detail) {
        this.username = detail.username;
        this.password = detail.password;
        this.created = !!detail.created;
        this.copied = false;
        this.$root.showModal();
      },
      // The password is shown once: nothing of it stays in the page after the dialog closes.
      clear() { this.password = ""; this.username = ""; this.copied = false; },
      async copy() {
        await copyText(this.password, this.$root.querySelector("input[readonly]"));
        this.copied = true;
      },
    };
  }

  function editUserRoles() {
    return {
      id: "", username: "", roles: [], selected: {}, initial: {},
      loading: false, loadError: "", busy: false, error: "",
      async open(detail) {
        this.id = detail.id; this.username = detail.username;
        this.roles = []; this.selected = {}; this.initial = {};
        this.loading = true; this.loadError = ""; this.error = "";
        this.$root.showModal();
        const res = await request("GET", userUrl(this.id, "/roles"));
        this.loading = false;
        if (res.handled) return;
        if (!res.ok) { this.loadError = res.error; return; }
        this.roles = res.data.roles;
        const picked = {};
        this.roles.forEach(function (role) { picked[role.name] = role.direct; });
        this.selected = Object.assign({}, picked);
        this.initial = Object.assign({}, picked);
      },
      chosen() {
        const self = this;
        return this.roles.filter(function (r) { return self.selected[r.name]; }).map(function (r) { return r.name; });
      },
      dirty() {
        const self = this;
        return this.roles.some(function (r) { return !!self.selected[r.name] !== !!self.initial[r.name]; });
      },
      async submit() {
        this.error = "";
        this.busy = true;
        const res = await request("PUT", userUrl(this.id, "/roles"), { roles: this.chosen() });
        this.busy = false;
        if (res.handled) return;
        if (!res.ok) { this.error = res.error; return; }
        this.$root.close();
        toast("success", fmtHtml(texts.rolesSaved, { username: this.username }));
        reload();
      },
    };
  }

  function userSessions() {
    return {
      id: "", username: "", isSelf: false, sessions: [],
      loading: false, loadError: "", busy: false, error: "",
      when: usersWhen,
      ago(iso) { return usersAgo(iso, now(), texts.justNow); },
      async open(detail) {
        this.id = detail.id; this.username = detail.username;
        this.sessions = []; this.isSelf = false;
        this.loadError = ""; this.error = "";
        this.$root.showModal();
        await this.load();
      },
      async load() {
        this.loading = true;
        const res = await request("GET", userUrl(this.id, "/sessions"));
        this.loading = false;
        if (res.handled) return;
        if (!res.ok) { this.loadError = res.error; return; }
        this.sessions = res.data.sessions;
        this.isSelf = res.data.is_self;
      },
      async end(sessionId) {
        this.error = "";
        this.busy = true;
        const res = await request("DELETE", userUrl(this.id, "/sessions/" + encodeURIComponent(sessionId)));
        this.busy = false;
        if (res.handled) return;
        if (!res.ok) { this.error = res.error; return; }
        toast("success", escapeHtml(texts.sessionEnded));
        await this.load();
      },
      async endAll() {
        this.error = "";
        this.busy = true;
        const res = await request("DELETE", userUrl(this.id, "/sessions"));
        this.busy = false;
        if (res.handled) return;
        if (!res.ok) { this.error = res.error; return; }
        toast("success", fmtHtml(texts.sessionsEnded, { username: this.username }));
        await this.load();
      },
    };
  }

  function resetPassword() {
    return {
      id: "", username: "", emailAddress: "", smtp: false, busy: false, error: "",
      open(detail) {
        this.id = detail.id; this.username = detail.username;
        this.emailAddress = detail.email || ""; this.smtp = smtp();
        this.error = "";
        this.$root.showModal();
      },
      canMail() { return this.smtp && this.emailAddress !== ""; },
      mailBlocked() { return mailBlocked(this.smtp, this.emailAddress); },
      async temporary() {
        this.error = "";
        this.busy = true;
        const res = await request("POST", userUrl(this.id, "/password/temporary"));
        this.busy = false;
        if (res.handled) return;
        if (!res.ok) { this.error = res.error; return; }
        this.$root.close();
        reload();
        announce("user-secret", { username: res.data.username, password: res.data.temporary_password, created: false });
      },
      async email() {
        this.error = "";
        this.busy = true;
        const res = await request("POST", userUrl(this.id, "/password/email"));
        this.busy = false;
        if (res.handled) return;
        if (!res.ok) { this.error = res.error; return; }
        this.$root.close();
        toast("success", fmtHtml(texts.linkSent, { username: this.username }));
      },
    };
  }

  function toggleUser() {
    return {
      id: "", username: "", enable: false, busy: false, error: "",
      open(detail) {
        this.id = detail.id; this.username = detail.username;
        this.enable = !detail.enabled; this.error = "";
        this.$root.showModal();
      },
      async submit() {
        this.error = "";
        this.busy = true;
        const res = await request("PUT", userUrl(this.id, "/enabled"), { enabled: this.enable });
        this.busy = false;
        if (res.handled) return;
        if (!res.ok) { this.error = res.error; return; }
        this.$root.close();
        toast("success", fmtHtml(this.enable ? texts.accountEnabled : texts.accountDisabled, { username: this.username }));
        reload();
      },
    };
  }

  return { newUser, secretDialog, editUserRoles, userSessions, resetPassword, toggleUser };
}
