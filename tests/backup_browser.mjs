// Browser path of the Backup / Restore page on the CSP build of Alpine (T-UI-03), run by
// tests/test_backup_browser.py against tests/design_serve.py --with-backups.
//
// In Chromium (playwright-core of .pk/), once per colour scheme: start a backup and follow
// its job, change the schedule, list the restore points and open one, tick and clear the bulk
// selection, then start and confirm a restore. The page, its partials and the reading API
// calls are the manager's own; the calls that would start papaia-ctl or a Docker container
// (POST backup, POST restore, GET job) are answered by stand-ins in the browser, which also
// record what the page sent. Every step is checked, and the run fails on any report of the
// policy, refused Alpine expression or script error.
//
// Usage (from the repository root):
//   node tests/backup_browser.mjs --base-url URL --storage-state FILE --report FILE
// Exit: 0 every step passed, 1 a step or the policy failed, 2 usage.
import { writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const POINT = "2026-07-30_10-19-38";
const JOB = "0b5c4a1e-backup";
const STEP_MS = 10000;

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 2) {
    const [flag, value] = [argv[i], argv[i + 1]];
    if (!["--base-url", "--storage-state", "--report"].includes(flag) || value === undefined) {
      process.stderr.write(`backup-browser: bad argument ${flag}\n`);
      process.exit(2);
    }
    args[flag.slice(2).replace("-", "_")] = value;
  }
  if (!args.base_url || !args.storage_state || !args.report) {
    process.stderr.write("backup-browser: --base-url, --storage-state and --report are required\n");
    process.exit(2);
  }
  if (!["127.0.0.1", "localhost"].includes(new URL(args.base_url).hostname)) {
    process.stderr.write("backup-browser: the base URL must be the loopback\n");
    process.exit(2);
  }
  return args;
}

function collectViolations() {
  window.__papaiaCsp = [];
  document.addEventListener("securitypolicyviolation", (event) => {
    window.__papaiaCsp.push({ directive: event.effectiveDirective, sample: event.sample });
  });
}

function check(condition, message) {
  if (!condition) throw new Error(message);
}

const json = (body, status = 200) => ({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

async function toast(page, text) {
  await page.locator("#pk-toasts").getByText(text).first().waitFor({ timeout: STEP_MS });
}

async function walk(page, base, sent) {
  const steps = [];
  const step = async (name, run) => {
    await run();
    steps.push(name);
  };

  // The job of the backup: running on the first poll, finished on the second.
  let polls = 0;
  await page.route(`**/api/v1/jobs/${JOB}`, (route) => {
    polls += 1;
    return route.fulfill(json({ id: JOB, status: polls < 2 ? "running" : "succeeded" }));
  });
  // The manager's job strip, as the server shows it while that job runs.
  const strip = (id, action) =>
    '<div id="backup-job-status" hx-get="/partials/backup/job-status" hx-trigger="every 2s"' +
    ` hx-swap="outerHTML" data-job-active="true" data-job-id="${id}"` +
    ` data-job-action="${action}"><p>${action} running</p></div>`;
  await page.route("**/partials/backup/job-status", (route) =>
    sent.backup && polls < 2
      ? route.fulfill({ status: 200, contentType: "text/html", body: strip(JOB, "backup") })
      : route.fallback(),
  );
  await page.route("**/api/v1/maintenance/backup", (route) => {
    sent.backup = route.request().postDataJSON();
    return route.fulfill(json({ job_id: JOB, status: "queued" }, 202));
  });
  await page.route("**/api/v1/maintenance/restore", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    sent.restore = route.request().postDataJSON();
    return route.fulfill(json({ status: "started" }, 202));
  });

  await page.goto(new URL("/backup", base).href, { waitUntil: "networkidle" });
  const converted = await page.locator('meta[name="papaia-csp"][content="strict"]').count();
  check(converted === 1, "the page is not marked converted");
  check(await page.evaluate(() => Boolean(window.Alpine)), "Alpine did not start");

  const actions = page.locator('[x-data="backupActions"]');
  const create = actions.locator("button.btn-primary").first();

  await step("start a backup", async () => {
    check(await create.isEnabled(), "Create backup is not offered on an idle page");
    await create.click();
    await page.locator("#backup-modal[open]").waitFor({ timeout: STEP_MS });
    await page.fill("#retention-days", "7");
    await page.locator("#backup-modal .modal-action .btn-primary").click();
    await page.locator("#backup-modal[open]").waitFor({ state: "detached", timeout: STEP_MS });
    check(JSON.stringify(sent.backup) === JSON.stringify({ retention_days: 7 }),
      `backup request body ${JSON.stringify(sent.backup)}`);
  });

  await step("the job state reaches the header", async () => {
    // While the job runs the header shows its log link and offers no second backup.
    await actions.locator(`a[href="/jobs/${JOB}"]`).waitFor({ state: "visible", timeout: STEP_MS });
    check(await create.isDisabled(), "Create backup is offered while a backup runs");
    await toast(page, "Backup complete.");
    await actions.locator(`a[href="/jobs/${JOB}"]`).waitFor({ state: "hidden", timeout: STEP_MS });
    check(await create.isEnabled(), "Create backup stays blocked after the job ended");
  });

  await step("the job strip announces its state", async () => {
    // A job of another tab: the strip's data-job-* attributes reach the header button.
    await page.route("**/partials/backup/job-status", (route) =>
      route.fulfill({ status: 200, contentType: "text/html", body: strip("0b5c4a1e-other", "upgrade") }),
    );
    await actions.locator('a[href="/jobs/0b5c4a1e-other"]').waitFor({ state: "visible", timeout: 15000 });
    check(await create.isDisabled(), "Create backup is offered while another job runs");
    await page.unroute("**/partials/backup/job-status");
    await actions.locator('a[href="/jobs/0b5c4a1e-other"]').waitFor({ state: "hidden", timeout: 15000 });
    check(await create.isEnabled(), "the idle strip did not release the button");
  });

  await step("change the schedule", async () => {
    await page.locator('[data-backup-action="open-schedule"]').click();
    await page.locator("#schedule-modal[open]").waitFor({ timeout: STEP_MS });
    await page.locator("#schedule-save:enabled").waitFor({ timeout: STEP_MS });
    await page.selectOption("#schedule-mode", "hourly");
    await page.locator("#schedule-hours-row:not(.hidden)").waitFor({ timeout: STEP_MS });
    check(await page.locator("#schedule-time-row.hidden").count() === 1, "the time row stays shown");
    await page.locator("#schedule-save:enabled").waitFor({ timeout: STEP_MS });
    await page.locator("#schedule-save").click();
    await toast(page, "Schedule saved.");
    await page.locator("#schedule-modal[open]").waitFor({ state: "detached", timeout: STEP_MS });
  });

  await step("list the restore points and open one", async () => {
    await page.locator(`[data-backup-action="open-detail"][data-restore-point="${POINT}"]`).click();
    await page.locator("#restore-detail-modal[open]").waitFor({ timeout: STEP_MS });
    await page.locator("#detail-body table").getByText("papaia_librechat_mongo").waitFor({ timeout: STEP_MS });
    await page.locator('#restore-detail-modal [data-dialog-close="restore-detail-modal"]').click();
    await page.locator("#restore-detail-modal[open]").waitFor({ state: "detached", timeout: STEP_MS });
  });

  await step("select and clear restore points", async () => {
    const bar = page.getByText("1 restore point selected");
    check(!(await bar.isVisible()), "the bulk bar shows without a selection");
    await page.locator(`input[aria-label="select restore point ${POINT}"]`).check();
    await bar.waitFor({ state: "visible", timeout: STEP_MS });
    await page.getByRole("button", { name: "Clear", exact: true }).click();
    await bar.waitFor({ state: "hidden", timeout: STEP_MS });
  });

  await step("start and confirm a restore", async () => {
    const row = page.locator(".card", { has: page.locator(`[data-restore-point="${POINT}"]`) });
    await row.locator('[role="button"]').focus();
    await row.locator('[data-backup-action="open-restore"]').click();
    await page.locator("#restore-modal[open]").waitFor({ timeout: STEP_MS });
    check((await page.textContent("#restore-step-badge")).includes("1"), "the wizard does not start at step 1");
    await page.locator("#restore-next").click();
    await page.locator("#restore-step-2:not(.hidden)").waitFor({ timeout: STEP_MS });
    await page.locator("#restore-next").click();
    await page.locator("#restore-step-3:not(.hidden)").waitFor({ timeout: STEP_MS });
    check(await page.locator("#restore-go").isDisabled(), "Restore is offered before the confirmation");
    await page.locator("#restore-confirm").check();
    check(await page.locator("#restore-go").isEnabled(), "the confirmation does not enable Restore");
    await page.locator("#restore-go").click();
    await toast(page, "Restore started.");
    await page.locator("#restore-modal[open]").waitFor({ state: "detached", timeout: STEP_MS });
    check(JSON.stringify(sent.restore) === JSON.stringify({ restore_point: POINT, restart_clean: false }),
      `restore request body ${JSON.stringify(sent.restore)}`);
  });

  return steps;
}

async function run(browser, args, scheme) {
  const context = await browser.newContext({
    storageState: args.storage_state,
    colorScheme: scheme,
    viewport: { width: 1440, height: 900 },
  });
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
  let steps = [];
  let failure = null;
  try {
    steps = await walk(page, args.base_url, sent);
  } catch (error) {
    failure = String(error.message || error).split("\n")[0];
  }
  const violations = await page.evaluate(() => window.__papaiaCsp || []).catch(() => []);
  await context.close();
  return { scheme, steps, failure, violations, alpine, errors, sent };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const playwright = pathToFileURL(resolve(REPO, ".pk/node_modules/playwright-core/index.mjs"));
  const { chromium } = await import(playwright.href);
  const browser = await chromium.launch();
  const results = [];
  try {
    for (const scheme of ["light", "dark"]) results.push(await run(browser, args, scheme));
  } finally {
    await browser.close();
  }
  writeFileSync(args.report, JSON.stringify({ results }, null, 2) + "\n");
  let code = 0;
  for (const r of results) {
    const clean = !r.failure && !r.violations.length && !r.alpine.length && !r.errors.length;
    process.stdout.write(`${r.scheme}: ${r.steps.length} steps${clean ? ", clean" : ""}\n`);
    if (!clean) {
      code = 1;
      if (r.failure) process.stderr.write(`  failed after "${r.steps.at(-1) || "load"}": ${r.failure}\n`);
      for (const v of r.violations) process.stderr.write(`  policy: ${v.directive} ${v.sample || ""}\n`);
      for (const a of r.alpine) process.stderr.write(`  alpine: ${a}\n`);
      for (const e of r.errors) process.stderr.write(`  error: ${e}\n`);
    }
  }
  return code;
}

main().then(
  (code) => process.exit(code),
  (error) => {
    process.stderr.write(`backup-browser: ${error.stack || error}\n`);
    process.exit(2);
  },
);
