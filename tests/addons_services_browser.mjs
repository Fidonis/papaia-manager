// Browser path of add-ons, catalogs and services on the CSP build of Alpine (T-UI-03), driven by
// tests/test_addons_services_scripts.py against the manager in the stand-in world
// (tests/design_serve.py).
//
// In each theme (light, dark): on the add-ons page an add-on is installed through its dialog,
// one is started and one stopped, and the catalogs are refreshed from the header; on an
// add-on's page the add-on is removed through the overflow menu and its dialog; on the
// catalogs page a catalog is added and one is edited; on the services page a group is ticked,
// the bulk bar counts it, a restart with clean-up is confirmed, the stack dialog names its
// verb and the stack runner's strip is dismissed. The stand-in world has no add-ons, catalogs
// or containers, so the partials htmx loads are the manager's own templates rendered with
// sample data by the test (--fragments DIR) and the API's answers are routed in the browser;
// the pages, the CSRF token and the scripts are the manager's own. Every page must stay free
// of policy reports (enforced header), refused Alpine expressions and script errors.
//
// Usage: node tests/addons_services_browser.mjs --base-url URL --storage-state FILE
//          --fragments DIR [--shots DIR]
// Prints a JSON report; exit 0 when every check holds, 1 otherwise, 2 on usage errors.
// --shots DIR writes the pages at 1440 and 390 px in both themes into DIR.
import { mkdirSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..");
let args;
let browser;

function usage(message) {
  process.stderr.write(`addons-services-check: ${message}\n`);
  process.exit(2);
}

function parseArgs(argv) {
  const parsed = {};
  for (let i = 0; i < argv.length; i += 2) {
    const [flag, value] = [argv[i], argv[i + 1]];
    if (!["--base-url", "--storage-state", "--fragments", "--shots"].includes(flag) || value === undefined) {
      usage(`bad argument ${flag}`);
    }
    parsed[flag.slice(2).replace("-", "_")] = value;
  }
  if (!parsed.base_url || !parsed.storage_state || !parsed.fragments) {
    usage("--base-url, --storage-state and --fragments are required");
  }
  if (!["127.0.0.1", "localhost"].includes(new URL(parsed.base_url).hostname)) usage("loopback only");
  return parsed;
}

// Runs in the page before any of its scripts: the policy's reports.
function watchPage() {
  document.addEventListener("securitypolicyviolation", (event) => {
    window.__papaiaReportCsp({ page: location.pathname, directive: event.effectiveDirective, blocked: event.blockedURI, sample: event.sample });
  });
}

const json = (route, body, status = 200) =>
  route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
const html = (route, name) =>
  route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: readFileSync(resolve(args.fragments, name), "utf8") });

async function routeWorld(context, sent) {
  const record = (request) => {
    const url = new URL(request.url());
    sent.push({ method: request.method(), path: url.pathname, body: request.postDataJSON(), csrf: request.headers()["x-csrf-token"] || "" });
  };
  await context.route("**/partials/addons", (route) => html(route, "addons.html"));
  await context.route("**/partials/addons/wiki", (route) => html(route, "addon-wiki.html"));
  await context.route("**/partials/catalogs", (route) => html(route, "catalogs.html"));
  await context.route("**/partials/services", (route) => html(route, "services.html"));
  await context.route("**/partials/stack-runner", (route) => html(route, "stack-runner.html"));
  await context.route("**/api/v1/addons/**", (route) => {
    record(route.request());
    return json(route, { job_id: "job-a" }, 202);
  });
  await context.route("**/api/v1/catalogs**", (route) => {
    const request = route.request();
    record(request);
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET") return json(route, [{ name: "main", type: "git", enabled: true }, { name: "disk", type: "local", enabled: true }]);
    if (path.endsWith("/refresh")) return json(route, { job_id: "job-c" }, 202);
    return json(route, {}, request.method() === "POST" ? 201 : 200);
  });
  await context.route("**/api/v1/stack/**", (route) => {
    record(route.request());
    if (route.request().url().endsWith("/runner/clear")) return route.fulfill({ status: 204 });
    return json(route, { job_id: "job-s" }, 202);
  });
  await context.route(/\/api\/v1\/jobs\/job-[acs]$/, (route) => {
    const id = new URL(route.request().url()).pathname.split("/").pop();
    return json(route, { id, status: "succeeded" });
  });
}

async function openPage(context, path, watch) {
  const page = await context.newPage();
  page.on("console", (message) => {
    const text = message.text();
    if (text.startsWith("Alpine Expression Error") || text.startsWith("Alpine Warning")) watch.alpine.push(text.split("\n")[0]);
  });
  page.on("pageerror", (error) => watch.errors.push(String(error.message).split("\n")[0]));
  await page.goto(new URL(path, args.base_url).href, { waitUntil: "networkidle" });
  return page;
}

async function shoot(page, name, theme) {
  if (!args.shots) return;
  mkdirSync(args.shots, { recursive: true });
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(150);
    await page.screenshot({ path: resolve(args.shots, `${name}-${width}-${theme}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 900 });
}

const last = (sent, method, path) => sent.filter((s) => s.method === method && s.path === path).at(-1);
const card = (page, name) => page.locator("#addon-gallery .card").filter({ has: page.locator(`p.font-semibold:text-is("${name}")`) });

async function addonsPath(context, theme, check, watch, sent) {
  const label = `addons (${theme})`;
  const page = await openPage(context, "/addons", watch);
  await card(page, "demo").waitFor();
  await shoot(page, "addons", theme);

  const demo = card(page, "demo");
  await demo.getByRole("button", { name: "Install", exact: true }).first().click();
  const dialog = demo.locator("dialog[open]");
  check(`${label}: the install dialog opens`, await dialog.isVisible());
  await dialog.locator('[data-env-key="DEMO_URL"]').fill("https://other.example.test");
  await dialog.locator('[name="start"]').uncheck();
  await dialog.getByRole("button", { name: "Install" }).click();
  await page.waitForFunction(() => !document.querySelector("dialog[open]"));
  await page.waitForTimeout(300);
  const install = last(sent, "POST", "/api/v1/addons/demo/install");
  check(`${label}: install sends the fields and the start choice`,
    install && install.csrf.length > 0 && install.body.catalog === "main" && install.body.start === false &&
    JSON.stringify(install.body.env) === JSON.stringify({ DEMO_URL: "https://other.example.test" }), install);

  await card(page, "chat").getByRole("button", { name: "Start" }).click();
  await card(page, "wiki").getByRole("button", { name: "Stop" }).click();
  await page.waitForTimeout(300);
  check(`${label}: start and stop run on their add-on`,
    last(sent, "POST", "/api/v1/addons/chat/start") && last(sent, "POST", "/api/v1/addons/wiki/stop"),
    sent.map((s) => s.path));

  await page.getByRole("button", { name: "Refresh catalogs" }).click();
  await page.waitForTimeout(500);
  check(`${label}: refresh catalogs refreshes each enabled git catalog`,
    last(sent, "POST", "/api/v1/catalogs/main/refresh") && !last(sent, "POST", "/api/v1/catalogs/disk/refresh"),
    sent.map((s) => s.path));
  await page.close();

  const detail = await openPage(context, "/addons/wiki", watch);
  await detail.locator("#addon-actions").getByRole("button", { name: "Stop" }).waitFor();
  await shoot(detail, "addon-detail", theme);
  await detail.locator('#addon-actions [role="button"][aria-label^="More actions"]').click();
  await detail.locator("#addon-actions .dropdown-content a.text-error").getByText("Remove", { exact: true }).click();
  const removeDialog = detail.locator("#addon-actions dialog[open]");
  check(`${label}: remove asks in its dialog first`,
    (await removeDialog.locator("h3").textContent()).trim() === "Remove wiki" && !last(sent, "POST", "/api/v1/addons/wiki/remove"));
  await removeDialog.getByRole("button", { name: "Remove" }).click();
  await detail.waitForTimeout(300);
  check(`${label}: the dialog's Remove runs it`, last(sent, "POST", "/api/v1/addons/wiki/remove"), sent.map((s) => s.path));
  await detail.close();
}

async function catalogsPath(context, theme, check, watch, sent) {
  const label = `catalogs (${theme})`;
  const page = await openPage(context, "/catalogs", watch);
  await page.locator("#catalog-list .card").first().waitFor();
  await shoot(page, "catalogs", theme);

  await page.getByRole("button", { name: "Add catalog" }).click();
  const add = page.locator("#add-catalog-modal");
  check(`${label}: the add dialog opens`, await add.isVisible());
  await page.locator("#catalog-type-select").selectOption("local");
  check(`${label}: local asks for a path`, await page.locator("#add-path").isVisible() && !(await page.locator("#add-url").isVisible()));
  await page.locator("#catalog-type-select").selectOption("git");
  await page.locator("#add-name").fill("extra");
  await page.locator("#add-url").fill("https://git.example.test/extra.git");
  await add.getByRole("button", { name: "Add" }).click();
  await page.waitForTimeout(300);
  const added = last(sent, "POST", "/api/v1/catalogs");
  check(`${label}: add posts the catalog`,
    added && added.csrf.length > 0 &&
    JSON.stringify(added.body) === JSON.stringify({ name: "extra", type: "git", url: "https://git.example.test/extra.git", ref: "main" }), added);
  check(`${label}: the add dialog closes`, (await page.locator("#add-catalog-modal[open]").count()) === 0, await add.evaluate((d) => d.open));

  await page.locator('#catalog-list [role="button"][aria-label^="More actions"]').first().click();
  await page.locator("#catalog-list").getByText("Edit", { exact: true }).click();
  const edit = page.locator("#edit-catalog-modal");
  check(`${label}: the edit dialog is filled from the row`,
    (await page.locator("#edit-name").inputValue()) === "main" && (await page.locator("#edit-ref").inputValue()) === "main");
  await page.locator("#edit-ref").fill("stable");
  await edit.getByRole("button", { name: "Save" }).click();
  await page.waitForTimeout(300);
  const saved = last(sent, "PUT", "/api/v1/catalogs/main");
  check(`${label}: the edit dialog saves through PUT`,
    saved && JSON.stringify(saved.body) === JSON.stringify({ enabled: true, url: "https://git.example.test/addons.git", ref: "stable" }), saved);

  await page.getByRole("button", { name: "Refresh" }).first().click();
  await page.waitForTimeout(400);
  check(`${label}: a row refreshes its catalog`, last(sent, "POST", "/api/v1/catalogs/main/refresh"));
  await page.close();
}

async function servicesPath(context, theme, check, watch, sent) {
  const label = `services (${theme})`;
  const page = await openPage(context, "/services", watch);
  await page.getByRole("checkbox", { name: "select rag" }).waitFor();
  const bar = page.locator(".sticky.bottom-4");
  check(`${label}: no bulk bar without a selection`, !(await bar.isVisible()));
  await page.getByRole("checkbox", { name: "select rag" }).check();
  check(`${label}: the bulk bar counts the selection`,
    (await bar.locator("span[x-text]").textContent()) === "1 group selected · 1 module",
    await bar.locator("span[x-text]").textContent());
  await shoot(page, "services", theme);
  await bar.getByRole("button", { name: "Restart" }).click();
  const confirm = page.locator("#service-controls dialog[open]");
  check(`${label}: the dialog names the verb and the count`,
    (await confirm.locator("h3").textContent()).trim() === "Restart 1 service group?");
  await confirm.getByRole("checkbox").check();
  await confirm.getByRole("button", { name: "Restart groups" }).click();
  await page.waitForTimeout(300);
  const restart = last(sent, "POST", "/api/v1/stack/groups/restart");
  check(`${label}: a group restart sends the selection`,
    restart && JSON.stringify(restart.body) === JSON.stringify({ groups: ["rag"], clean_up: true }), restart);

  await page.getByRole("button", { name: "Stack", exact: true }).click();
  await page.getByText("Stop stack", { exact: true }).first().click();
  const stackDialog = page.locator("dialog[open]");
  check(`${label}: the stack dialog names its verb`,
    (await stackDialog.locator("h3").textContent()).trim() === "Stop the whole stack?");
  await stackDialog.getByRole("button", { name: "Cancel" }).click();
  check(`${label}: cancel sends nothing`, !last(sent, "POST", "/api/v1/stack/stop"));

  await page.locator("#stack-runner").getByRole("button", { name: "Dismiss" }).click();
  await page.waitForTimeout(300);
  check(`${label}: dismiss clears the runner`, last(sent, "POST", "/api/v1/stack/runner/clear"));
  await page.close();
}

async function main() {
  args = parseArgs(process.argv.slice(2));
  const playwright = pathToFileURL(resolve(REPO, ".pk/node_modules/playwright-core/index.mjs"));
  const { chromium } = await import(playwright.href);
  browser = await chromium.launch();
  const report = { checks: [], pages: {} };
  const check = (name, ok, detail) => report.checks.push({ name, ok: Boolean(ok), detail: ok ? undefined : detail });
  try {
    for (const theme of ["light", "dark"]) {
      const context = await browser.newContext({
        storageState: args.storage_state,
        locale: "en-GB",
        colorScheme: theme,
        reducedMotion: "reduce",
        viewport: { width: 1440, height: 900 },
      });
      const reports = [];
      await context.exposeFunction("__papaiaReportCsp", (r) => reports.push(r));
      await context.addInitScript(watchPage);
      const sent = [];
      await routeWorld(context, sent);
      const watch = { alpine: [], errors: [] };
      for (const path of [addonsPath, catalogsPath, servicesPath]) {
        try {
          await path(context, theme, check, watch, sent);
        } catch (error) {
          check(`${theme}: ${path.name} ran to its end`, false, String(error.message).split("\n")[0]);
        }
      }
      check(`${theme}: no report of the content security policy`, reports.length === 0, reports);
      check(`${theme}: no refused Alpine expression`, watch.alpine.length === 0, watch.alpine);
      check(`${theme}: no script error`, watch.errors.length === 0, watch.errors);
      report.pages[theme] = { reports, ...watch };
      await context.close();
    }
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(report, null, 2) + "\n");
  return report.checks.every((c) => c.ok) ? 0 : 1;
}

main().then(
  (code) => process.exit(code),
  (error) => {
    process.stderr.write(`addons-services-check: ${error.stack || error}\n`);
    process.exit(2);
  },
);
