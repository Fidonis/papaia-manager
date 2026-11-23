// Browser paths of the settings pages (T-UI-03, oberfl 3.3), driven by
// tests/test_settings_scripts.py against the manager in the stand-in world
// (tests/design_serve.py).
//
// Manager tab (/settings): a refused interval is named under the field, a saved interval and
// a saved name come back after the reload, a logo upload shows in the preview and can be
// removed, the reset asks in its dialog, and a refusal of the server reaches the alert region.
// Modules tab (/settings/modules): a toggle opens the questions of its module under the row,
// the mail form follows its connection mode, Cancel closes it again, switching on sends the
// answers and follows the job, and switching off asks in the dialog, which Escape cancels.
// Writes to the core that the stand-in world does not have are answered by routes in the
// browser. Both pages are also opened in the dark theme. Every page records the reports of
// the enforced policy and uncaught script errors.
//
// Usage: node tests/settings_browser.mjs --base-url URL --storage-state FILE
// Prints a JSON report; exit 0 when every check holds, 1 otherwise, 2 on usage errors.
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..");
// A 1x1 PNG.
const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
  "base64",
);

function usage(message) {
  process.stderr.write(`settings-check: ${message}\n`);
  process.exit(2);
}

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 2) {
    const [flag, value] = [argv[i], argv[i + 1]];
    if (!["--base-url", "--storage-state"].includes(flag) || value === undefined) {
      usage(`bad argument ${flag}`);
    }
    args[flag.slice(2).replace("-", "_")] = value;
  }
  if (!args.base_url || !args.storage_state) usage("both arguments are required");
  if (!["127.0.0.1", "localhost"].includes(new URL(args.base_url).hostname)) usage("loopback only");
  return args;
}

// Runs in the page before its scripts: collects the reports of the enforced policy.
function collectReports() {
  window.__papaiaCsp = [];
  document.addEventListener("securitypolicyviolation", (event) => {
    window.__papaiaCsp.push(`${event.violatedDirective} ${event.blockedURI || "inline"}`);
  });
}

function alerts() {
  const region = document.querySelector('#pk-toasts > [role="alert"]');
  return region ? [...region.children].map((t) => t.textContent.replace(/\s+/g, " ").trim()) : [];
}

function statuses() {
  const region = document.querySelector('#pk-toasts > [role="status"]');
  return region ? [...region.children].map((t) => t.textContent.replace(/\s+/g, " ").trim()) : [];
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const playwright = pathToFileURL(resolve(REPO, ".pk/node_modules/playwright-core/index.mjs"));
  const { chromium } = await import(playwright.href);
  const browser = await chromium.launch();
  const report = { checks: [], errors: [], violations: [] };
  const check = (name, ok, detail) => report.checks.push({ name, ok: Boolean(ok), detail });
  const url = (path) => new URL(path, args.base_url).href;

  async function open(context, path) {
    const page = await context.newPage();
    page.on("pageerror", (error) => report.errors.push(`${path}: ${String(error.message).split("\n")[0]}`));
    await page.addInitScript(collectReports);
    await page.goto(url(path), { waitUntil: "networkidle" });
    return page;
  }

  async function reports(page, label) {
    const found = await page.evaluate(() => window.__papaiaCsp || []);
    report.violations.push(...found.map((v) => `${label}: ${v}`));
  }

  // Clicks and waits for the reload the page does after a saved change.
  async function reloadedBy(page, action) {
    await Promise.all([page.waitForEvent("load", { timeout: 10_000 }), action()]);
    await page.waitForLoadState("networkidle");
  }

  try {
    const context = await browser.newContext({
      storageState: args.storage_state,
      locale: "en-GB",
      reducedMotion: "reduce",
      colorScheme: "light",
    });

    // -- Manager tab --------------------------------------------------------------------
    let page = await open(context, "/settings");
    await page.fill("#host-interval", "0");
    await page.click('#host-form button[type="submit"]');
    check("a refused interval is named under the field",
      (await page.textContent("#host-interval-error")) === "Enter a whole number."
        && !(await page.getAttribute("#host-interval-error", "class")).includes("hidden"));
    await page.fill("#host-interval", "2");
    await page.selectOption("#host-unit", "min");
    await page.fill("#host-interval", "120");
    await page.click('#host-form button[type="submit"]');
    check("an interval above the bound is named with the bounds",
      (await page.textContent("#host-interval-error")) === "Choose between 10 seconds and 60 minutes.");
    await page.fill("#host-interval", "5");
    check("typing clears the message", (await page.textContent("#host-interval-error")) === "");
    await reloadedBy(page, () => page.click('#host-form button[type="submit"]'));
    check("the saved interval comes back after the reload",
      (await page.inputValue("#host-interval")) === "5" && (await page.inputValue("#host-unit")) === "min");
    await reports(page, "/settings host");

    await page.fill("#branding-name", "Musterfirma");
    await reloadedBy(page, () => page.click('#branding-form button[type="submit"]'));
    check("the saved name comes back after the reload", (await page.inputValue("#branding-name")) === "Musterfirma");

    await reloadedBy(page, () => page.setInputFiles("#logo-file", { name: "logo.png", mimeType: "image/png", buffer: PNG }));
    const preview = await page.evaluate(() => {
      const el = document.getElementById("logo-preview");
      return { tag: el.tagName, src: el.getAttribute("src"), loaded: el.tagName === "IMG" && el.complete && el.naturalWidth > 0 };
    });
    check("an uploaded logo shows in the preview", preview.tag === "IMG" && preview.loaded, preview);
    await reloadedBy(page, () => page.click("#logo-remove"));
    check("a removed logo leaves the built-in one",
      (await page.evaluate(() => document.getElementById("logo-preview").tagName)) === "SPAN");

    await page.click("#branding-reset");
    check("the reset asks in its dialog", await page.evaluate(() => document.getElementById("reset-branding-modal").open));
    await reloadedBy(page, () => page.click("#reset-branding-confirm"));
    check("the reset puts the name back", (await page.inputValue("#branding-name")) === "");

    await page.route("**/api/v1/settings/host", (route) =>
      route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "Changed <b>meanwhile</b>." }) }),
    );
    await page.click('#host-form button[type="submit"]');
    await page.waitForFunction(() => document.querySelector('#pk-toasts > [role="alert"] > *'));
    const refused = await page.evaluate(alerts);
    check("a refusal of the server reaches the alert region as text",
      refused.length === 1 && refused[0].includes("Changed <b>meanwhile</b>."), refused);
    await page.unroute("**/api/v1/settings/host");
    await reports(page, "/settings");
    await page.close();

    // -- Modules tab --------------------------------------------------------------------
    // The stand-in world has no core that ships the mail relay, so its toggle is disabled
    // there. The page is answered as on an installation that has one: the same markup with
    // the mail toggle switchable.
    await context.route("**/settings/modules", async (route) => {
      const response = await route.fetch();
      const body = (await response.text()).replace(/(<input[^>]*data-module="mail"[^>]*?)\s+disabled>/, "$1>");
      await route.fulfill({ response, body });
    });
    page = await open(context, "/settings/modules");
    const toggles = await page.evaluate(() =>
      [...document.querySelectorAll("input[data-module]")].map((b) => [b.dataset.module, b.checked, b.disabled]),
    );
    check("the locked toggles stay disabled", toggles.some(([n, , d]) => n === "manager" && d), toggles);

    await page.click('input[data-module="mail"]');
    await page.waitForSelector("#panel-mail:not(.hidden)");
    check("the mail toggle opens its questions under the row with the server field focused",
      await page.evaluate(() => document.activeElement && document.activeElement.id === "om-host"));
    await page.selectOption("#om-security", "tls");
    check("the port follows the connection mode", (await page.inputValue("#om-port")) === "465");
    await page.selectOption("#om-security", "none");
    check("without encryption there is no login",
      await page.evaluate(() => {
        const box = document.getElementById("om-login");
        return box.disabled && !box.checked && document.getElementById("om-user").disabled;
      }));
    await page.click("#panel-mail [data-panel-cancel]");
    check("Cancel closes the questions and leaves the module off",
      await page.evaluate(() =>
        document.getElementById("panel-mail").classList.contains("hidden")
          && !document.querySelector('input[data-module="mail"]').checked));

    // Switching on: the answers go along; a refusal stays in the panel, an accepted change
    // is followed as a job until the page reloads.
    let sent = null;
    let answer = { status: 422, body: { detail: "The mail server is not reachable." } };
    await page.route("**/api/v1/config/modules/mail/enable", (route) => {
      sent = route.request().postDataJSON();
      route.fulfill({ status: answer.status, contentType: "application/json", body: JSON.stringify(answer.body) });
    });
    await page.route("**/api/v1/jobs/itk-job-1", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: "itk-job-1", status: "succeeded" }) }),
    );
    await page.click('input[data-module="mail"]');
    await page.waitForSelector("#panel-mail:not(.hidden)");
    await page.fill("#om-host", "smtp.example.test");
    await page.selectOption("#om-security", "starttls");
    await page.fill("#om-from", "noreply@example.test");
    await page.click("#panel-mail [data-panel-go]");
    await page.waitForFunction(() => !document.querySelector("#panel-mail [data-panel-error]").classList.contains("hidden"));
    check("switching on sends the answers of the panel",
      sent && typeof sent.revision === "string" && sent.mail.host === "smtp.example.test"
        && sent.mail.port === 587 && sent.mail.from_address === "noreply@example.test", sent);
    check("a refusal is named in the open panel",
      (await page.textContent("#panel-mail [data-panel-error]")) === "The mail server is not reachable.");
    answer = { status: 200, body: { job_id: "itk-job-1" } };
    await reloadedBy(page, () => page.click("#panel-mail [data-panel-go]"));
    check("an accepted change is followed until the page reloads", true);
    await page.unroute("**/api/v1/config/modules/mail/enable");

    // Switching off asks first; Escape cancels and the module stays on.
    const ragOn = await page.evaluate(() => document.querySelector('input[data-module="rag"]').checked);
    check("the RAG system is on in the stand-in world", ragOn);
    await page.click('input[data-module="rag"]');
    await page.waitForFunction(() => document.getElementById("module-off-dialog").open);
    check("switching off asks in the dialog",
      (await page.textContent("#module-off-title")) === "Switch RAG System off?");
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.getElementById("module-off-dialog").open);
    check("Escape cancels and the module stays on",
      await page.evaluate(() => document.querySelector('input[data-module="rag"]').checked));

    let off = null;
    await page.route("**/api/v1/config/modules/rag/disable", (route) => {
      off = route.request().postDataJSON();
      route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "Another change is running." }) });
    });
    await page.click('input[data-module="rag"]');
    await page.waitForFunction(() => document.getElementById("module-off-dialog").open);
    await page.click("#module-off-go");
    await page.waitForFunction(() => document.querySelector('#pk-toasts > [role="alert"] > *'));
    check("switching off sends the choice", off && off.scope === "keep" && typeof off.revision === "string", off);
    check("a refused switch-off is told and the module stays on",
      (await page.evaluate(alerts)).some((t) => t.includes("Another change is running."))
        && (await page.evaluate(() => document.querySelector('input[data-module="rag"]').checked)));
    report.statuses = await page.evaluate(statuses);
    await reports(page, "/settings/modules");
    await page.close();
    await context.close();

    // -- the dark theme -----------------------------------------------------------------
    const dark = await browser.newContext({
      storageState: args.storage_state,
      locale: "en-GB",
      reducedMotion: "reduce",
      colorScheme: "dark",
    });
    for (const path of ["/settings", "/settings/modules"]) {
      const shown = await open(dark, path);
      await shown.waitForTimeout(400);
      await reports(shown, `${path} dark`);
      await shown.close();
    }
    await dark.close();
  } catch (error) {
    check("the paths ran to the end", false, String(error && error.stack ? error.stack : error));
  } finally {
    await browser.close();
  }
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  const ok = report.checks.every((c) => c.ok) && !report.errors.length && !report.violations.length;
  process.exit(ok ? 0 : 1);
}

main();
