// Settings -> Installation: each card saves on its own through the installation API and
// reloads the page, so the sidebar shows the new brand. The texts come from data attributes
// of #installation-settings; the page carries no inline script and no event attributes.
// Uses csrfToken, fmt and showToast of the shared page script.
(() => {
  "use strict";

  const root = document.getElementById("installation-settings");
  if (!root) return;
  const base = "/api/v1/settings/installation";
  const text = (name) => root.dataset[name] || "";

  // The toast takes markup; a message of the core may quote what was typed.
  function plainText(value) {
    const span = document.createElement("span");
    span.textContent = value;
    return span.innerHTML;
  }

  async function send(path, options) {
    const headers = { "X-CSRF-Token": csrfToken() };
    if (options.json !== undefined) headers["Content-Type"] = "application/json";
    const res = await fetch(base + path, {
      method: options.method || "PUT",
      headers: headers,
      body: options.json !== undefined ? JSON.stringify(options.json) : options.body,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = typeof data.detail === "string"
        ? data.detail
        : fmt(text("msgFailed"), { status: res.status });
      showToast("error", plainText(detail));
      return null;
    }
    return data;
  }

  function reloadWith(message) {
    showToast("success", plainText(message));
    setTimeout(() => window.location.reload(), 600);
  }

  const value = (id) => document.getElementById(id).value;

  function onSubmit(id, handler) {
    const form = document.getElementById(id);
    if (form) form.addEventListener("submit", (event) => {
      event.preventDefault();
      handler();
    });
  }

  onSubmit("brand-form", async () => {
    const data = await send("/brand", {
      json: { name: value("brand-name"), suffix: value("brand-suffix") },
    });
    if (data) reloadWith(text("msgBrand"));
  });

  onSubmit("contact-form", async () => {
    const data = await send("/contact", {
      json: { name: value("contact-name"), mail: value("contact-mail"), phone: value("contact-phone") },
    });
    if (data) reloadWith(text("msgContact"));
  });

  onSubmit("delegations-form", async () => {
    const data = await send("/delegations", {
      json: {
        idle_days: Number.parseInt(value("idle-days"), 10),
        max_days: Number.parseInt(value("max-days"), 10),
      },
    });
    if (!data) return;
    if (data.realm_synced === false) {
      showToast("warning", plainText(text("msgRealmLater")));
      setTimeout(() => window.location.reload(), 1500);
      return;
    }
    reloadWith(text("msgLifetime"));
  });

  const logo = document.getElementById("installation-logo");
  if (logo) logo.addEventListener("change", async () => {
    if (!logo.files.length) return;
    const form = new FormData();
    form.append("file", logo.files[0]);
    const data = await send("/logo", { method: "POST", body: form });
    logo.value = "";
    if (data) reloadWith(text("msgLogo"));
  });

  const removeLogo = document.getElementById("installation-logo-remove");
  if (removeLogo) removeLogo.addEventListener("click", async () => {
    const data = await send("/logo", { method: "DELETE" });
    if (data) reloadWith(text("msgLogoRemoved"));
  });

  const fourEyes = document.getElementById("four-eyes");
  if (fourEyes && !fourEyes.disabled) fourEyes.addEventListener("change", async () => {
    fourEyes.disabled = true;
    const data = await send("/four-eyes", { json: { four_eyes: fourEyes.checked ? "on" : "off" } });
    if (data) {
      reloadWith(text("msgApprovals"));
    } else {
      fourEyes.checked = !fourEyes.checked;
      fourEyes.disabled = false;
    }
  });
})();
