// Settings -> Manager (settings.html): the branding and host monitoring cards. Each card is
// saved on its own through /api/v1/settings and the page reloads, so the sidebar, which
// reads the same file, shows it too. Values come from the data islands `mgr-settings`
// (revision, shared texts), `mgr-settings-branding` and `mgr-settings-host`; the page carries
// no inline script and no event attribute (T-UI-03).
import { csrfToken } from "../core/csrf.js";
import { escapeHtml, fmt } from "../core/format.js";
import { readIsland } from "../core/islands.js";
import { showToast } from "../core/runtime.js";

const page = readIsland("mgr-settings");
const el = (id) => document.getElementById(id);

// Shared by every settings section: send JSON/multipart with the CSRF header, surface the
// server's message, and keep the revision current.
let settingsRevision = page.revision;

// A saved section reloads the page so the sidebar, which reads the same file, shows it too.
function reloadAfter(message) {
  showToast("success", escapeHtml(message));
  setTimeout(() => window.location.reload(), 600);
}

async function settingsRequest(url, options) {
  const headers = Object.assign(
    { "X-CSRF-Token": csrfToken() },
    options.json !== undefined ? { "Content-Type": "application/json" } : {},
  );
  const res = await fetch(url, {
    method: options.method || "POST",
    headers: headers,
    body: options.json !== undefined ? JSON.stringify(options.json) : options.body,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof data.detail === "string" ? data.detail : fmt(page.texts.requestFailed, { status: res.status });
    showToast("error", escapeHtml(detail));
    return null;
  }
  settingsRevision = data.revision;
  return data;
}

// -- Branding (partials/_settings_branding.html) -------------------------------------------

function installBranding() {
  const texts = readIsland("mgr-settings-branding").texts;

  async function saveBranding(event) {
    event.preventDefault();
    const hide = el("branding-hide-tagline").checked;
    const tagline = el("branding-tagline").value.trim();
    const data = await settingsRequest("/api/v1/settings/branding", {
      method: "PUT",
      json: {
        revision: settingsRevision,
        name: el("branding-name").value,
        // null keeps the built-in line; an empty string hides it.
        tagline: hide ? "" : (tagline === "" ? null : tagline),
      },
    });
    if (data) reloadAfter(texts.saved);
  }

  function resetBranding() {
    el("reset-branding-modal").showModal();
  }

  async function confirmResetBranding() {
    const modal = el("reset-branding-modal");
    const button = el("reset-branding-confirm");
    button.disabled = true;
    const data = await settingsRequest("/api/v1/settings/branding/reset", {
      json: { revision: settingsRevision },
    });
    button.disabled = false;
    modal.close();
    if (data) reloadAfter(texts.reset);
  }

  async function uploadLogo(input) {
    if (!input.files.length) return;
    const form = new FormData();
    form.append("file", input.files[0]);
    const data = await settingsRequest("/api/v1/settings/branding/logo", { body: form });
    input.value = "";
    if (data) reloadAfter(texts.logoUpdated);
  }

  async function removeLogo() {
    const data = await settingsRequest("/api/v1/settings/branding/logo", { method: "DELETE" });
    if (data) reloadAfter(texts.logoRemoved);
  }

  el("branding-form").addEventListener("submit", saveBranding);
  el("branding-reset").addEventListener("click", resetBranding);
  el("reset-branding-confirm").addEventListener("click", confirmResetBranding);
  const file = el("logo-file");
  file.addEventListener("change", () => uploadLogo(file));
  // Only while an uploaded logo is shown.
  const remove = el("logo-remove");
  if (remove) remove.addEventListener("click", removeLogo);
}

// -- Host monitoring (partials/_settings_host.html) ----------------------------------------

function installHost() {
  const host = readIsland("mgr-settings-host");
  const HOST_MIN_SECONDS = host.min;
  const HOST_MAX_SECONDS = host.max;
  const HOST_DEFAULT_SECONDS = host.default;

  function hostIntervalError(message) {
    const error = el("host-interval-error");
    error.textContent = message || "";
    error.classList.toggle("hidden", !message);
  }

  // The server is the authority; this only saves a round trip for the obvious.
  function hostIntervalSeconds() {
    const value = Number(el("host-interval").value);
    const unit = el("host-unit").value;
    if (!Number.isInteger(value) || value <= 0) {
      hostIntervalError(host.texts.wholeNumber);
      return null;
    }
    const seconds = unit === "min" ? value * 60 : value;
    if (seconds < HOST_MIN_SECONDS || seconds > HOST_MAX_SECONDS) {
      hostIntervalError(fmt(host.texts.range, { min: HOST_MIN_SECONDS, max: HOST_MAX_SECONDS / 60 }));
      return null;
    }
    hostIntervalError("");
    return seconds;
  }

  async function submitHostInterval(seconds) {
    const data = await settingsRequest("/api/v1/settings/host", {
      method: "PUT",
      json: { revision: settingsRevision, refresh_seconds: seconds },
    });
    if (data) reloadAfter(host.texts.saved);
  }

  function saveHost(event) {
    event.preventDefault();
    const seconds = hostIntervalSeconds();
    if (seconds !== null) submitHostInterval(seconds);
  }

  function resetHost() {
    el("host-interval").value = HOST_DEFAULT_SECONDS;
    el("host-unit").value = "s";
    hostIntervalError("");
    submitHostInterval(HOST_DEFAULT_SECONDS);
  }

  el("host-form").addEventListener("submit", saveHost);
  el("host-reset").addEventListener("click", resetHost);
  el("host-interval").addEventListener("input", () => hostIntervalError(""));
}

installBranding();
installHost();
