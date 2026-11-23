// Browser path of Users, Roles and Audit log on the CSP build of Alpine (MGR-021, T-UI-03),
// run by tests/test_accounts_browser.py against tests/design_serve.py.
//
// In Chromium (playwright-core of .pk/), once per colour scheme: create a user, reset its
// password, assign it a role and disable it; create a role and delete it; then filter and
// page the audit log those writes filled, and preview a prune. Everything answers from the
// manager itself and the simulated Keycloak of the stand-in world. Every step is checked, and
// the run fails on any report of the policy, refused Alpine expression or script error.
//
// Usage (from the repository root):
//   node tests/accounts_browser.mjs --base-url URL --storage-state FILE --report FILE
// Exit: 0 every step passed, 1 a step or the policy failed, 2 usage.
import { writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const STEP_MS = 10000;

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 2) {
    const [flag, value] = [argv[i], argv[i + 1]];
    if (!["--base-url", "--storage-state", "--report"].includes(flag) || value === undefined) {
      process.stderr.write(`accounts-browser: bad argument ${flag}\n`);
      process.exit(2);
    }
    args[flag.slice(2).replace("-", "_")] = value;
  }
  if (!args.base_url || !args.storage_state || !args.report) {
    process.stderr.write("accounts-browser: --base-url, --storage-state and --report are required\n");
    process.exit(2);
  }
  if (!["127.0.0.1", "localhost"].includes(new URL(args.base_url).hostname)) {
    process.stderr.write("accounts-browser: the base URL must be the loopback\n");
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

async function toast(page, text) {
  await page.locator("#pk-toasts").getByText(text).first().waitFor({ timeout: STEP_MS });
}

async function open(page, base, path, collect) {
  await collect();
  await page.goto(new URL(path, base).href, { waitUntil: "networkidle" });
  const converted = await page.locator('meta[name="papaia-csp"][content="strict"]').count();
  check(converted === 1, `${path} is not marked converted`);
  check(await page.evaluate(() => Boolean(window.Alpine)), `Alpine did not start on ${path}`);
}

// Opens the row menu of `name` and picks `item`. The list may still be swapped in after the
// last change, so the menu is opened once the page is idle, and again if a swap closed it.
async function rowMenu(page, name, item) {
  const kebab = page.getByRole("button", { name: `More actions for ${name}`, exact: true });
  const entry = page.locator(".dropdown", { has: kebab }).getByText(item, { exact: true });
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await page.waitForLoadState("networkidle");
    await kebab.click();
    try {
      await entry.waitFor({ state: "visible", timeout: 3000 });
      await entry.click({ timeout: 3000 });
      return;
    } catch (error) {
      if (attempt === 2) throw error;
      await page.keyboard.press("Escape");
    }
  }
}

async function walk(page, base, name, collect, steps) {
  const step = async (label, run) => {
    await run();
    steps.push(label);
  };
  const user = `dora-${name}`;
  const role = `team-${name}`;

  await open(page, base, "/users", collect);
  await page.locator("#user-list table").waitFor({ timeout: STEP_MS });

  await step("create a user", async () => {
    await page.getByRole("button", { name: "New user", exact: true }).click();
    const dialog = page.locator("#new-user-modal[open]");
    await dialog.waitFor({ timeout: STEP_MS });
    await dialog.locator('input[type="checkbox"]').first().waitFor({ timeout: STEP_MS });
    await page.fill("#new-user-username", user);
    await dialog.getByRole("button", { name: "Create user" }).click();
    const secret = page.locator("#user-secret-modal[open]");
    await secret.waitFor({ timeout: STEP_MS });
    await secret.getByText(user, { exact: true }).waitFor({ timeout: STEP_MS });
    const password = await secret.locator("input[readonly]").inputValue();
    check(password.length > 0, "the temporary password is not shown");
    await secret.getByRole("button", { name: "Done" }).click();
    await secret.waitFor({ state: "detached", timeout: STEP_MS });
    // The password is shown once: the field is empty again once the dialog has closed.
    await page.waitForFunction(
      () => document.querySelector("#user-secret-modal input[readonly]").value === "",
      null,
      { timeout: STEP_MS },
    );
    await page.locator("#user-list").getByText(user, { exact: true }).waitFor({ timeout: STEP_MS });
  });

  await step("reset the password of the user", async () => {
    await rowMenu(page, user, "Reset password…");
    const dialog = page.locator("#user-password-modal[open]");
    await dialog.waitFor({ timeout: STEP_MS });
    await dialog.getByText(user, { exact: true }).waitFor({ timeout: STEP_MS });
    await dialog.getByRole("button", { name: "Generate" }).click();
    await page.locator("#user-secret-modal[open]").waitFor({ timeout: STEP_MS });
    await page.locator("#user-secret-modal").getByRole("button", { name: "Done" }).click();
    await page.locator("#user-secret-modal[open]").waitFor({ state: "detached", timeout: STEP_MS });
  });

  await step("assign a role to the user", async () => {
    const row = page.locator("#user-list tr", { has: page.getByText(user, { exact: true }) });
    await row.getByRole("button", { name: "Roles", exact: true }).click();
    const dialog = page.locator("#user-roles-modal[open]");
    await dialog.waitFor({ timeout: STEP_MS });
    const box = dialog.locator("label", { has: page.getByText("papaia-admin", { exact: true }) })
      .locator('input[type="checkbox"]');
    await box.check();
    await dialog.getByRole("button", { name: "Save roles" }).click();
    await toast(page, `Roles of ${user} saved.`);
    await row.getByText("papaia-admin").first().waitFor({ timeout: STEP_MS });
  });

  await step("disable the user", async () => {
    await rowMenu(page, user, "Disable account…");
    const dialog = page.locator("#user-toggle-modal[open]");
    await dialog.waitFor({ timeout: STEP_MS });
    await dialog.getByText("can no longer sign in", { exact: false }).waitFor({ timeout: STEP_MS });
    await dialog.getByRole("button", { name: "Disable", exact: true }).click();
    await toast(page, `Account ${user} disabled.`);
  });

  await step("search the users", async () => {
    await page.fill("#user-search", user);
    await page.locator("#user-list tbody tr").first().waitFor({ timeout: STEP_MS });
    await page.waitForFunction(
      (name) => [...document.querySelectorAll("#user-list tbody tr")].every((r) => r.textContent.includes(name)),
      user,
      { timeout: STEP_MS },
    );
  });

  await open(page, base, "/users/roles", collect);
  await page.locator("#role-list table").waitFor({ timeout: STEP_MS });

  await step("create a role", async () => {
    await page.getByRole("button", { name: "New role", exact: true }).click();
    const dialog = page.locator("#role-modal[open]");
    await dialog.waitFor({ timeout: STEP_MS });
    await dialog.locator('input[type="checkbox"]').first().waitFor({ timeout: STEP_MS });
    await page.fill("#role-name", role);
    await page.fill("#role-description", "A team");
    await dialog.getByRole("button", { name: "Create role" }).click();
    await toast(page, `Role ${role} created.`);
    await page.locator("#role-list").getByText(role, { exact: true }).waitFor({ timeout: STEP_MS });
  });

  await step("look at a built-in role", async () => {
    await page.locator("#role-list button", { hasText: "View" }).first().click();
    const dialog = page.locator("#role-modal[open]");
    await dialog.waitFor({ timeout: STEP_MS });
    await dialog.getByText("Built in", { exact: true }).waitFor({ timeout: STEP_MS });
    await dialog.getByRole("button", { name: "Close", exact: true }).first().click();
    await dialog.waitFor({ state: "detached", timeout: STEP_MS });
  });

  await step("delete the role", async () => {
    await rowMenu(page, role, "Delete…");
    const dialog = page.locator("#role-delete-modal[open]");
    await dialog.waitFor({ timeout: STEP_MS });
    await dialog.getByText("No account holds it directly.").waitFor({ timeout: STEP_MS });
    const go = dialog.getByRole("button", { name: "Delete", exact: true });
    check(await go.isDisabled(), "Delete is offered before the name was typed");
    await page.fill("#role-confirm-name", role);
    await go.click();
    await toast(page, `Role ${role} deleted.`);
  });

  await open(page, base, "/audit?limit=2", collect);
  await page.locator("#audit-list table").waitFor({ timeout: STEP_MS });

  await step("the filters offer what the log holds", async () => {
    await page.waitForFunction(
      () => document.getElementById("audit-filter-action").options.length > 2,
      null,
      { timeout: STEP_MS },
    );
    const actions = await page.locator("#audit-filter-action option").allTextContents();
    check(actions.includes("user.create"), `actions offered: ${actions.join(", ")}`);
  });

  // The position line under the list: "1–2 of 9".
  const position = async (pattern) => {
    const line = page.locator("#audit-list p.text-xs").last();
    await page.waitForFunction(
      ([source]) => {
        const lines = document.querySelectorAll("#audit-list p.text-xs");
        const text = lines.length ? lines[lines.length - 1].textContent.trim() : "";
        return new RegExp(source).test(text);
      },
      [pattern.source],
      { timeout: STEP_MS },
    ).catch(async () => {
      throw new Error(`the list says "${(await line.textContent()).trim()}", not ${pattern}`);
    });
  };

  await step("page the audit log", async () => {
    await position(/^1–2 of \d+$/);
    await page.locator("#audit-list").getByRole("button", { name: "Next" }).click();
    await position(/^3–4 of \d+$/);
  });

  await step("filter the audit log", async () => {
    await page.selectOption("#audit-filter-action", "user.create");
    await position(/^1–\d+ of \d+$/);
    const cells = await page.locator("#audit-list tbody tr td:nth-child(3)").allTextContents();
    check(cells.length > 0 && cells.every((c) => c.trim() === "user.create"), `rows: ${cells.join(", ")}`);
    const href = await page.getAttribute("#audit-export-csv", "href");
    check(href === "/api/v1/audit/export?format=csv&action=user.create", `export link ${href}`);
  });

  await step("preview a prune", async () => {
    await page.getByRole("button", { name: "More actions", exact: true }).click();
    await page.locator("#audit-prune-open").click();
    const dialog = page.locator("#audit-prune-modal[open]");
    await dialog.waitFor({ timeout: STEP_MS });
    check(await page.locator("#audit-prune-go").isDisabled(), "Delete is offered without a date");
    await page.fill("#audit-prune-date", "2000-01-01");
    await page.locator("#audit-prune-preview").getByText("No entries are older than this date.")
      .waitFor({ timeout: STEP_MS });
    await dialog.getByRole("button", { name: "30d" }).click();
    await page.waitForFunction(() => document.getElementById("audit-prune-date").value !== "2000-01-01");
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await dialog.waitFor({ state: "detached", timeout: STEP_MS });
  });

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
  const violations = [];
  page.on("console", (message) => {
    const text = message.text();
    if (text.startsWith("Alpine Expression Error") || text.startsWith("Alpine Warning")) alpine.push(text.split("\n")[0]);
  });
  page.on("pageerror", (error) => errors.push(String(error.message).split("\n")[0]));
  // The reports of a page are read before the next one is opened and at the end.
  const collect = async () => {
    violations.push(...(await page.evaluate(() => window.__papaiaCsp || []).catch(() => [])));
  };
  const steps = [];
  let failure = null;
  try {
    await walk(page, args.base_url, scheme, collect, steps);
  } catch (error) {
    failure = String(error.message || error).split("\n")[0];
  }
  await collect();
  await context.close();
  return { scheme, steps, failure, violations, alpine, errors };
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
    process.stderr.write(`accounts-browser: ${error.stack || error}\n`);
    process.exit(2);
  },
);
