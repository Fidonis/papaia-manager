// Browser path of the ingest lists on the CSP build of Alpine (T-UI-03), run by
// tests/test_ingest_browser.py against tests/design_serve.py --with-ingest.
//
// In Chromium (playwright-core of .pk/), in one colour scheme: list and filter the jobs, disable
// and enable one, start a run and abort it, filter the runs and delete runs of a job, switch the
// tabs of a job, delete a job, remove a leftover, save the defaults of the job file and check
// the file, and see the panel of a list that cannot be loaded. The pages, their partials and the
// manager's API are the real ones; behind them answers the fake ingester of the page tests. Every
// step is checked, and the run fails on any report of the policy, refused Alpine expression or
// script error on any page it opened. With --shots DIR it first renders each page of the group
// at 1440 and 390 pixels into DIR.
//
// Usage (from the repository root):
//   node tests/ingest_browser.mjs --base-url URL --storage-state FILE --scheme light|dark
//     --report FILE [--shots DIR]
// Exit: 0 every step passed, 1 a step or the policy failed, 2 usage.
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const STEP_MS = 10000;
const FLAGS = ["--base-url", "--storage-state", "--scheme", "--report", "--shots"];

function usage(message) {
  process.stderr.write(`ingest-browser: ${message}\n`);
  process.exit(2);
}

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 2) {
    const [flag, value] = [argv[i], argv[i + 1]];
    if (!FLAGS.includes(flag) || value === undefined) usage(`bad argument ${flag}`);
    args[flag.slice(2).replace("-", "_")] = value;
  }
  if (!args.base_url || !args.storage_state || !args.report) {
    usage("--base-url, --storage-state and --report are required");
  }
  if (!["light", "dark"].includes(args.scheme)) usage("--scheme must be light or dark");
  if (!["127.0.0.1", "localhost"].includes(new URL(args.base_url).hostname)) {
    usage("the base URL must be the loopback");
  }
  return args;
}

// In every document: reports of the policy go to the test through a binding, so they survive
// the navigations of the walk.
function collectViolations() {
  document.addEventListener("securitypolicyviolation", (event) => {
    window.__ingestReport({ page: location.pathname, directive: event.effectiveDirective, sample: event.sample });
  });
}

function check(condition, message) {
  if (!condition) throw new Error(message);
}

async function toast(page, text) {
  await page.locator("#pk-toasts").getByText(text).first().waitFor({ timeout: STEP_MS });
}

async function open(page, base, path) {
  await page.goto(new URL(path, base).href, { waitUntil: "networkidle" });
  const converted = await page.locator('meta[name="papaia-csp"][content="strict"]').count();
  check(converted === 1, `${path} is not marked converted`);
  check(await page.evaluate(() => Boolean(window.Alpine)), `Alpine did not start on ${path}`);
}

// The row of a job in the list, and its kebab menu opened.
function jobRow(page, id) {
  return page.locator(`#ingest-list tr[data-group]`, { has: page.locator(`a[href="/ingest/jobs/${id}"]`) });
}

async function openRowMenu(page, id) {
  await jobRow(page, id).locator('.dropdown [role="button"]').focus();
}

async function visibleRows(page) {
  return page.locator("#ingest-list tr[data-group]").evaluateAll((rows) =>
    rows.filter((row) => row.style.display !== "none").length);
}

async function walk(page, base, sent, steps) {
  const step = async (name, run) => {
    await run();
    steps.push(name);
  };
  page.on("request", (request) => {
    if (request.method() === "POST" && /\/api\/v1\/rag\/ingest\/jobs\/[^/]+\/run$/.test(request.url())) {
      sent.run = request.postDataJSON();
    }
  });

  await step("list and filter the jobs", async () => {
    await open(page, base, "/ingest/jobs");
    await page.locator("#ingest-list tr[data-group]").nth(2).waitFor({ timeout: STEP_MS });
    check(await visibleRows(page) === 3, "the list does not show three jobs");
    await page.fill("#ingest-search", "hand");
    check(await visibleRows(page) === 1, "the search does not narrow the list");
    await page.fill("#ingest-search", "nothing-like-this");
    await page.locator("#ingest-none").waitFor({ state: "visible", timeout: STEP_MS });
    await page.fill("#ingest-search", "");
    await page.locator('[data-chip="disabled"]').click();
    check(await visibleRows(page) === 1, "the chip does not narrow the list");
    check(await page.locator('[data-chip="disabled"].btn-active').count() === 1, "the chip is not marked");
    await page.locator('[data-chip="all"]').click();
    check(await visibleRows(page) === 3, "All does not show every job");
    check(await page.locator("#ingest-none").isHidden(), "No job matches stays shown");
  });

  await step("disable and enable a job", async () => {
    await openRowMenu(page, "handbook");
    await jobRow(page, "handbook").locator('[data-ingest-action="disable"]').click();
    await toast(page, /Job handbook disabled/);
    await jobRow(page, "handbook").locator('[data-ingest-action="enable"]').waitFor({ state: "attached", timeout: STEP_MS });
    await openRowMenu(page, "handbook");
    await jobRow(page, "handbook").locator('[data-ingest-action="enable"]').click();
    await toast(page, /Job handbook enabled/);
    await jobRow(page, "handbook").locator('[data-ingest-action="disable"]').waitFor({ state: "attached", timeout: STEP_MS });
  });

  await step("start a run and abort it", async () => {
    await jobRow(page, "handbook").locator("button.btn-success").click();
    const dialog = page.locator("#ingest-run[open]");
    await dialog.waitFor({ timeout: STEP_MS });
    await dialog.getByText("Run handbook").waitFor({ timeout: STEP_MS });
    check((await page.locator("#ingest-run-mode option").first().textContent()).startsWith("As the job is set up: "),
      "the first mode does not name the job's own");
    await dialog.getByRole("button", { name: "More options" }).click();
    await dialog.getByText("Keep files that disappeared from the source").waitFor({ timeout: STEP_MS });
    await dialog.getByRole("button", { name: "Start run" }).click();
    await page.waitForURL(/\/ingest\/runs\/[0-9a-f-]+$/, { timeout: STEP_MS });
    check(JSON.stringify(sent.run) === JSON.stringify({
      dry_run: false, skip_sync: false, delete_vanished: true, confirm_rebuild: false, confirm_collection: false,
    }),
      `run request body ${JSON.stringify(sent.run)}`);
    await open(page, base, new URL(page.url()).pathname);
    const abort = page.locator('[data-ingest-action="abort"]');
    await abort.waitFor({ timeout: STEP_MS });
    await abort.click();
    await toast(page, "Abort requested. The run stops after the file it is working on.");
    await abort.waitFor({ state: "detached", timeout: STEP_MS });
  });

  await step("filter the runs and delete those of a job", async () => {
    await open(page, base, "/ingest/runs");
    await page.locator("#ingest-run-rows tbody tr").nth(1).waitFor({ timeout: STEP_MS });
    await page.selectOption("#runs-job", "handbook");
    await page.waitForURL(/\/ingest\/runs\?job=handbook$/, { timeout: STEP_MS });
    await page.getByRole("button", { name: "More actions for the runs" }).focus();
    await page.locator('[data-ingest-action="delete-runs"]').click();
    const dialog = page.locator("#ingest-delete-runs[open]");
    await dialog.waitFor({ timeout: STEP_MS });
    check(await page.inputValue("#ingest-delete-runs-job") === "handbook", "the dialog does not take the filtered job");
    await dialog.getByText("2 runs (3 log lines) will be deleted.").waitFor({ timeout: STEP_MS });
    await page.fill("#ingest-delete-runs-typed", "handbook");
    await dialog.getByRole("button", { name: "Delete 2 runs" }).click();
    await toast(page, "2 runs of handbook deleted");
    await page.getByText("No runs", { exact: true }).waitFor({ timeout: STEP_MS });
  });

  await step("switch the tabs of a job", async () => {
    await open(page, base, "/ingest/jobs/handbook");
    await page.getByText("What it does").waitFor({ timeout: STEP_MS });
    for (const [tab, text] of [["runs", "No runs"], ["files", "Files this job tracks"], ["overview", "What it does"]]) {
      await page.locator(`#job-tabs [data-tab="${tab}"]`).click();
      await page.locator("#job-tab").getByText(text, { exact: true }).waitFor({ timeout: STEP_MS });
      check(await page.locator(`#job-tabs [data-tab="${tab}"]`).getAttribute("aria-selected") === "true",
        `the ${tab} tab is not selected`);
      check(new URL(page.url()).hash === `#${tab}`, `the address does not name the ${tab} tab`);
    }
  });

  await step("delete a job", async () => {
    await open(page, base, "/ingest/jobs");
    await jobRow(page, "archive").waitFor({ timeout: STEP_MS });
    await openRowMenu(page, "archive");
    await jobRow(page, "archive").getByText("Delete", { exact: true }).click();
    const dialog = page.locator("#ingest-delete[open]");
    await dialog.waitFor({ timeout: STEP_MS });
    await dialog.getByText("Delete archive?").waitFor({ timeout: STEP_MS });
    await dialog.getByText("The job has no finished runs.").waitFor({ timeout: STEP_MS });
    await dialog.getByRole("button", { name: "Delete", exact: true }).click();
    await toast(page, "Job archive deleted");
    await page.locator("#ingest-list tr[data-group]").nth(1).waitFor({ timeout: STEP_MS });
    check(await jobRow(page, "archive").count() === 0, "the deleted job is still listed");
  });

  await step("remove a leftover", async () => {
    await open(page, base, "/ingest/orphans");
    await page.getByRole("button", { name: "Remove", exact: true }).first().click();
    const dialog = page.locator("#orphan-delete[open]");
    await dialog.waitFor({ timeout: STEP_MS });
    await dialog.getByText("Remove what retired left?").waitFor({ timeout: STEP_MS });
    await dialog.getByText("This deletes its points (1,200) from kb").waitFor({ timeout: STEP_MS });
    const remove = dialog.getByRole("button", { name: "Remove", exact: true });
    check(await remove.isDisabled(), "Remove is offered before the id is typed");
    await page.fill("#orphan-typed", "retired");
    await remove.click();
    await toast(page, "Removed 1200 points and 2 records of retired");
    await page.getByText("Nothing is left over").waitFor({ timeout: STEP_MS });
  });

  await step("save the defaults and check the file", async () => {
    await open(page, base, "/ingest/catalog");
    await page.fill("#catalog-embedding-model", "bge-m3");
    await page.getByRole("button", { name: "Save defaults" }).click();
    await toast(page, /Defaults saved/);
    await page.getByRole("button", { name: "Check", exact: true }).click();
    await page.getByText("This would load.").waitFor({ timeout: STEP_MS });
  });

  await step("a list that cannot be loaded says why", async () => {
    await page.route("**/partials/ingest/orphans", (route) =>
      route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "<b>down</b>" }) }));
    await open(page, base, "/ingest/orphans");
    await page.getByText("Leftovers are not available").waitFor({ timeout: STEP_MS });
    await page.locator("#orphan-list [data-reason]").getByText("<b>down</b>").waitFor({ timeout: STEP_MS });
    await page.unroute("**/partials/ingest/orphans");
  });
}

// The pages of the group, as they are before the walk changes anything.
const SHOTS = [
  ["jobs", "/ingest/jobs", "#ingest-list tr[data-group]"],
  ["job", "/ingest/jobs/handbook", "#job-tab dl"],
  ["runs", "/ingest/runs", "#ingest-run-rows"],
  ["orphans", "/ingest/orphans", "#orphan-list table"],
  ["catalog", "/ingest/catalog", "#catalog-embedding-model"],
];

async function shots(browser, args) {
  mkdirSync(args.shots, { recursive: true });
  for (const width of [1440, 390]) {
    const context = await browser.newContext({
      storageState: args.storage_state,
      colorScheme: args.scheme,
      viewport: { width, height: 900 },
    });
    const page = await context.newPage();
    for (const [name, path, ready] of SHOTS) {
      await page.goto(new URL(path, args.base_url).href, { waitUntil: "networkidle" });
      await page.locator(ready).first().waitFor({ timeout: STEP_MS });
      await page.screenshot({ path: resolve(args.shots, `${name}-${width}-${args.scheme}.png`), fullPage: true });
    }
    if (width === 1440) {
      await page.goto(new URL("/ingest/jobs", args.base_url).href, { waitUntil: "networkidle" });
      await jobRow(page, "handbook").locator("button.btn-success").click();
      await page.locator("#ingest-run[open]").waitFor({ timeout: STEP_MS });
      await page.locator("#ingest-run .modal-box").getByText("Run handbook").waitFor({ timeout: STEP_MS });
      // The modal fades in.
      await page.waitForTimeout(500);
      await page.screenshot({ path: resolve(args.shots, `run-dialog-${width}-${args.scheme}.png`) });
    }
    await context.close();
  }
}

async function run(browser, args) {
  const context = await browser.newContext({
    storageState: args.storage_state,
    colorScheme: args.scheme,
    viewport: { width: 1440, height: 900 },
  });
  const violations = [];
  await context.exposeBinding("__ingestReport", (_source, report) => violations.push(report));
  await context.addInitScript(collectViolations);
  const page = await context.newPage();
  const alpine = [];
  const errors = [];
  page.on("console", (message) => {
    const text = message.text();
    if (text.startsWith("Alpine Expression Error") || text.startsWith("Alpine Warning")) alpine.push(text.split("\n")[0]);
  });
  page.on("pageerror", (error) => errors.push(String(error.message).split("\n")[0]));
  const sent = {};
  const steps = [];
  let failure = null;
  try {
    await walk(page, args.base_url, sent, steps);
  } catch (error) {
    failure = String(error.message || error).split("\n").slice(0, 3).join(" | ");
  }
  await context.close();
  return { scheme: args.scheme, steps, failure, violations, alpine, errors, sent };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const playwright = pathToFileURL(resolve(REPO, ".pk/node_modules/playwright-core/index.mjs"));
  const { chromium } = await import(playwright.href);
  const browser = await chromium.launch();
  let result;
  try {
    if (args.shots) await shots(browser, args);
    result = await run(browser, args);
  } finally {
    await browser.close();
  }
  writeFileSync(args.report, JSON.stringify(result, null, 2) + "\n");
  const clean = !result.failure && !result.violations.length && !result.alpine.length && !result.errors.length;
  process.stdout.write(`${result.scheme}: ${result.steps.length} steps${clean ? ", clean" : ""}\n`);
  if (clean) return 0;
  if (result.failure) process.stderr.write(`  failed after "${result.steps.at(-1) || "load"}": ${result.failure}\n`);
  for (const v of result.violations) process.stderr.write(`  policy: ${v.page} ${v.directive} ${v.sample || ""}\n`);
  for (const a of result.alpine) process.stderr.write(`  alpine: ${a}\n`);
  for (const e of result.errors) process.stderr.write(`  error: ${e}\n`);
  return 1;
}

main().then(
  (code) => process.exit(code),
  (error) => {
    process.stderr.write(`ingest-browser: ${error.stack || error}\n`);
    process.exit(2);
  },
);
