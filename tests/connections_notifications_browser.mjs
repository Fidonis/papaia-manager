// Browser paths of the Connections and notifications pages (T-UI-03, oberfl 3.3), driven by
// tests/test_connections_notifications_scripts.py against the manager in the stand-in world
// (tests/design_serve.py --rag-catalog, so the connection store is writable).
//
// Connections (/connections): a new connection is tested in its dialog and saved, tested from
// its row, edited, and deleted after the question in its dialog; the reset dialog names the
// connection; a list that cannot be loaded says so instead of spinning.
// Notifications (/mail): a recipient needs a name, is added as a webhook, gets an event ticked
// in the matrix, is edited, gets a test that a webhook on the loopback receives, and is deleted
// after the question in its dialog; the event settings are saved. Both pages are also opened
// in the dark theme. Every page records the reports of the report-only policy and uncaught
// script errors.
//
// Usage: node tests/connections_notifications_browser.mjs --base-url URL --storage-state FILE
// Prints a JSON report; exit 0 when every check holds, 1 otherwise, 2 on usage errors.
import { createServer } from "node:http";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function usage(message) {
  process.stderr.write(`connections-check: ${message}\n`);
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

// Runs in the page before its scripts: collects the reports of the report-only policy.
function collectReports() {
  window.__papaiaCsp = [];
  document.addEventListener("securitypolicyviolation", (event) => {
    window.__papaiaCsp.push(`${event.violatedDirective} ${event.blockedURI || "inline"}`);
  });
}

function toasts(role) {
  const region = document.querySelector(`#pk-toasts > [role="${role}"]`);
  return region ? [...region.children].map((t) => t.textContent.replace(/\s+/g, " ").trim()) : [];
}

// A webhook on the loopback that answers every post with 204 and counts them.
function listen() {
  const posts = [];
  const server = createServer((request, response) => {
    let body = "";
    request.on("data", (chunk) => { body += chunk; });
    request.on("end", () => {
      posts.push({ method: request.method, body });
      response.writeHead(204);
      response.end();
    });
  });
  return new Promise((done) => {
    server.listen(0, "127.0.0.1", () => done({ server, posts, port: server.address().port }));
  });
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const playwright = pathToFileURL(resolve(REPO, ".pk/node_modules/playwright-core/index.mjs"));
  const { chromium } = await import(playwright.href);
  const browser = await chromium.launch();
  const hook = await listen();
  const report = { checks: [], errors: [], violations: [] };
  const check = (name, ok, detail) => report.checks.push({ name, ok: Boolean(ok), detail });
  const url = (path) => new URL(path, args.base_url).href;

  async function open(context, path) {
    const page = await context.newPage();
    page.on("pageerror", (error) => report.errors.push(`${path}: ${String(error.message).split("\n")[0]}`));
    page.on("console", (message) => {
      if (message.type() === "error" || message.type() === "warning") {
        const text = message.text();
        if (/alpine/i.test(text)) report.errors.push(`${path}: ${text.split("\n")[0]}`);
      }
    });
    await page.addInitScript(collectReports);
    await page.goto(url(path), { waitUntil: "networkidle" });
    return page;
  }

  async function reports(page, label) {
    const found = await page.evaluate(() => window.__papaiaCsp || []);
    report.violations.push(...found.map((v) => `${label}: ${v}`));
  }

  async function lastToast(page, role) {
    await page.waitForFunction((r) => document.querySelector(`#pk-toasts > [role="${r}"] > *`), role);
    const shown = await page.evaluate(toasts, role);
    return shown[shown.length - 1];
  }

  async function clearToasts(page) {
    await page.evaluate(() => {
      for (const region of document.querySelectorAll("#pk-toasts > [role]")) region.replaceChildren();
    });
  }

  // Clicks and waits for the reload the page does after a saved change.
  async function reloadedBy(page, action) {
    await Promise.all([page.waitForEvent("load", { timeout: 10_000 }), action()]);
    await page.waitForLoadState("networkidle");
  }

  const isOpen = (page, id) => page.evaluate((i) => document.getElementById(i).open, id);

  try {
    const context = await browser.newContext({
      storageState: args.storage_state,
      locale: "en-GB",
      reducedMotion: "reduce",
      colorScheme: "light",
    });

    // -- Connections --------------------------------------------------------------------
    let page = await open(context, "/connections");
    await page.waitForSelector("#connection-modal", { state: "attached" });
    await page.click("text=New vector database");
    check("the page action opens the dialog", await isOpen(page, "connection-modal"));
    check("the dialog asks for a new connection",
      (await page.textContent("#connection-modal h3")) === "New vector database");
    check("the type field stays hidden with one type",
      await page.evaluate(() => getComputedStyle(document.querySelector('#connection-modal select').closest(".form-control")).display === "none"));
    await page.fill('#connection-modal input[maxlength="64"]', "archive");
    await page.fill('#connection-modal input[inputmode="url"]', "http://127.0.0.1:9");
    const optional = await page.evaluate(() =>
      [...document.querySelectorAll("#connection-modal .label-text")].map((l) => l.textContent.trim()).filter(Boolean));
    check("the optional fields are labelled through the component", optional.some((t) => t.endsWith("(optional)")), optional);
    await page.click('#connection-modal button:has-text("Test")');
    await page.waitForFunction(() => {
      const shown = [...document.querySelectorAll("#connection-modal p.text-error, #connection-modal p.text-success")]
        .find((p) => p.offsetParent !== null && p.textContent.trim());
      return Boolean(shown);
    });
    const tested = await page.evaluate(() =>
      [...document.querySelectorAll("#connection-modal p.text-error")].map((p) => p.textContent.trim()).filter(Boolean));
    check("a test in the dialog shows its result", tested.some((t) => t.includes("not reachable")), tested);
    await page.click('#connection-modal button[type="submit"]');
    await page.waitForFunction(() => !document.getElementById("connection-modal").open);
    check("saving tells it in a toast", (await lastToast(page, "status")) === "Connection archive created");
    await page.waitForSelector('[data-connection="archive"]');
    check("the list shows the new connection", true);

    await clearToasts(page);
    await page.click('button[data-connection="archive"]');
    const rowTest = await lastToast(page, "alert");
    check("a test from the row reports the result with the name",
      rowTest.startsWith("archive: ") && rowTest.includes("not reachable"), rowTest);
    check("the row's button is enabled again",
      await page.evaluate(() => !document.querySelector('button[data-connection="archive"]').disabled));

    await clearToasts(page);
    await page.click('button:has-text("Edit")');
    await page.waitForFunction(() => document.getElementById("connection-modal").open);
    check("Edit opens the dialog with the stored values",
      (await page.textContent("#connection-modal h3")) === "Edit vector database"
        && (await page.inputValue('#connection-modal input[maxlength="64"]')) === "archive"
        && (await page.inputValue('#connection-modal input[inputmode="url"]')) === "http://127.0.0.1:9");
    await page.fill('#connection-modal input[inputmode="url"]', "http://127.0.0.1:10");
    await page.click('#connection-modal button[type="submit"]');
    await page.waitForFunction(() => !document.getElementById("connection-modal").open);
    check("an edit is saved", (await lastToast(page, "status")) === "Connection archive saved");
    await page.waitForFunction(() => document.body.textContent.includes("http://127.0.0.1:10"));

    await page.evaluate(() => window.dispatchEvent(new CustomEvent("connection-reset", { detail: { name: "papaia" } })));
    await page.waitForFunction(() => document.getElementById("reset-connection-modal").open);
    check("the reset dialog names the connection",
      (await page.textContent("#reset-connection-modal h3")).trim() === "Reset papaia?");
    await page.keyboard.press("Escape");

    await clearToasts(page);
    await page.focus('[aria-label="More actions for connection archive"]');
    await page.click('#connection-list a.text-error:has-text("Delete")');
    await page.waitForFunction(() => document.getElementById("delete-connection-modal").open);
    check("Delete asks in its dialog with the name",
      (await page.textContent("#delete-connection-modal h3")).trim() === "Delete archive?");
    await page.click('#delete-connection-modal button[type="submit"]');
    await page.waitForFunction(() => !document.getElementById("delete-connection-modal").open);
    check("a deletion is told", (await lastToast(page, "status")) === "Connection archive deleted");
    await page.waitForFunction(() => !document.querySelector('[data-connection="archive"]'));
    check("the deleted connection leaves the list", true);
    await reports(page, "/connections");
    await page.close();

    // A list that cannot be loaded says so instead of spinning.
    await context.route("**/partials/connections", (route) =>
      route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "The store is <b>away</b>." }) }),
    );
    page = await open(context, "/connections");
    await page.waitForFunction(() => document.querySelector("#connection-list p.text-lg"));
    const failed = await page.evaluate(() =>
      [...document.querySelectorAll("#connection-list p")].map((p) => p.textContent.trim()));
    check("a failed load names the reason as text",
      failed[0] === "Connections are not available" && failed[1].includes("The store is <b>away</b>."), failed);
    await reports(page, "/connections failed");
    await page.close();
    await context.unroute("**/partials/connections");

    // -- Notifications ------------------------------------------------------------------
    page = await open(context, "/mail");
    await page.click("text=Add recipient");
    check("the page action opens the dialog", await isOpen(page, "recipient-modal"));
    await page.click('#recipient-modal button[type="submit"]');
    check("a recipient needs a name",
      (await page.textContent('#recipient-modal p[role="alert"]')).trim() === "Enter a name.");
    const kind = await page.evaluate(() =>
      document.querySelector('#recipient-modal button[aria-pressed="true"]').textContent.trim());
    if (kind !== "Webhook") await page.click('#recipient-modal button:has-text("Webhook")');
    await page.fill("#recipient-name", "Ops <team>");
    await page.fill("#recipient-target", `http://127.0.0.1:${hook.port}/papaia`);
    await page.click('#recipient-modal button:has-text("Select all")');
    check("Select all ticks every event",
      await page.evaluate(() => [...document.querySelectorAll('#recipient-modal fieldset input[type="checkbox"]')].every((b) => b.checked)));
    await page.click('#recipient-modal button:has-text("Clear all")');
    await page.click('#recipient-modal fieldset label:first-of-type input');
    await reloadedBy(page, () => page.click('#recipient-modal button[type="submit"]'));
    const row = await page.evaluate(() => {
      const tr = document.querySelector("tr[data-recipient]");
      return tr ? { id: tr.dataset.recipient, text: tr.textContent.replace(/\s+/g, " ") } : null;
    });
    check("the new recipient is listed with its name as text", row && row.text.includes("Ops <team>"), row);

    const second = await page.evaluate(() => document.querySelectorAll("tr[data-recipient] input[data-event]")[1].dataset.event);
    await page.click(`tr[data-recipient] input[data-event="${second}"]`);
    await page.waitForFunction(() => document.getElementById("notify-saved").textContent === "Saved");
    check("a tick saves at once", true);
    await page.reload({ waitUntil: "networkidle" });
    check("the tick is still there after a reload",
      await page.isChecked(`tr[data-recipient] input[data-event="${second}"]`));

    await page.click('tr[data-recipient] button:has-text("Edit")');
    await page.waitForFunction(() => document.getElementById("recipient-modal").open);
    check("Edit opens the dialog with the stored values",
      (await page.textContent("#recipient-modal h3")) === "Edit recipient"
        && (await page.inputValue("#recipient-name")) === "Ops <team>"
        && (await page.inputValue("#recipient-target")) === "");
    await page.fill("#recipient-name", "Ops team");
    await reloadedBy(page, () => page.click('#recipient-modal button[type="submit"]'));
    check("the edit is saved", (await page.textContent("tr[data-recipient] p.font-semibold")).trim() === "Ops team");

    const before = hook.posts.length;
    await reloadedBy(page, () => page.click("tr[data-recipient] [data-recipient-test]"));
    check("a test reaches the webhook", hook.posts.length === before + 1 && hook.posts[before].method === "POST", hook.posts.length);
    check("the passed test is shown next to the recipient",
      (await page.textContent("tr[data-recipient]")).includes("Test passed"));

    await page.fill("#set-interval", "7");
    await page.click("#set-save");
    check("the event settings are saved", (await lastToast(page, "status")) === "Settings saved.");
    await page.reload({ waitUntil: "networkidle" });
    check("the saved interval comes back", (await page.inputValue("#set-interval")) === "7");

    // The row menu floats (overflow_menu(float=True)) and closes on any scroll, so the row is
    // brought into view before its trigger is clicked.
    await page.locator("tr[data-recipient]").scrollIntoViewIfNeeded();
    await page.waitForTimeout(100);
    await page.click('[aria-label="More actions for Ops team"]');
    await page.waitForSelector("tr[data-recipient] a.text-error", { state: "visible" });
    await page.click('tr[data-recipient] a.text-error');
    await page.waitForFunction(() => document.getElementById("recipient-delete-modal").open);
    check("Delete asks in its dialog with the name",
      (await page.textContent("#recipient-delete-modal h3")).trim() === "Delete Ops team?");
    await reloadedBy(page, () => page.click('#recipient-delete-modal button.btn-error'));
    check("the deleted recipient leaves the list", !(await page.$("tr[data-recipient]")));
    await reports(page, "/mail");
    await page.close();
    await context.close();

    // -- the dark theme -----------------------------------------------------------------
    const dark = await browser.newContext({
      storageState: args.storage_state,
      locale: "en-GB",
      reducedMotion: "reduce",
      colorScheme: "dark",
    });
    for (const path of ["/connections", "/mail"]) {
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
    hook.server.close();
  }
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  const ok = report.checks.every((c) => c.ok) && !report.errors.length && !report.violations.length;
  process.exit(ok ? 0 : 1);
}

main();
