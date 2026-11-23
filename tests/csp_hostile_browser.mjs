// Browser check of the enforced content security policy with hostile values (T-UI-03, P4),
// driven by tests/test_csp_hostile_browser.py against the manager with the stand-in world of
// tests/rag_serve.py --hostile.
//
// A value that is markup and script at once (design_world.HOSTILE, passed as --value) sits in
// the name the frame shows for the signed-in administrator, the display name of an account,
// the name of a collection and the name of a notification recipient; the answer of a
// recipient's test is routed in the browser with the same value as its reason, so it reaches
// a toast. In both colour schemes every place must show the value as text: no dialog opens
// (the value calls alert), no element of the value appears, no report of the policy is
// raised, no script error, and every page answers with the enforced policy.
//
// The dashboard of that world also holds tiles whose icon is an address on another site
// (passed as --icon), as a tiles.yaml of an older release may: the policy loads images from the
// manager's own site only, so neither the dashboard nor the tile editor and its dialog may put
// that address into an image; the bundled icon or the lettered badge stands in.
//
// Usage: node tests/csp_hostile_browser.mjs --base-url URL --storage-state FILE --value TEXT
//          --icon URL [--shots DIR]
// Prints a JSON report; exit 0 when every check holds, 1 otherwise, 2 on usage errors.
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SCHEMES = ["light", "dark"];
const WIDTHS = [1440, 390];
const SETTLE_MS = 400;

function usage(message) {
  process.stderr.write(`csp-hostile: ${message}\n`);
  process.exit(2);
}

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 2) {
    const [flag, value] = [argv[i], argv[i + 1]];
    if (!["--base-url", "--storage-state", "--value", "--icon", "--shots"].includes(flag) || value === undefined) {
      usage(`bad argument ${flag}`);
    }
    args[flag.slice(2).replace("-", "_")] = value;
  }
  if (!args.base_url || !args.storage_state || !args.value || !args.icon) {
    usage("--base-url, --storage-state, --value and --icon are required");
  }
  if (!["127.0.0.1", "localhost"].includes(new URL(args.base_url).hostname)) usage("loopback only");
  return args;
}

// Runs in the page before any of its scripts: collects the reports of the policy.
function collectViolations() {
  window.__papaiaCsp = [];
  document.addEventListener("securitypolicyviolation", (event) => {
    window.__papaiaCsp.push({ directive: event.effectiveDirective, sample: event.sample });
  });
}

async function shoot(page, args, view, scheme) {
  if (!args.shots) return [];
  mkdirSync(args.shots, { recursive: true });
  const files = [];
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: 900 });
    await page.waitForTimeout(150);
    const file = resolve(args.shots, `${view}-${width}-${scheme}.png`);
    await page.screenshot({ path: file, fullPage: true });
    files.push(file);
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  return files;
}

// What the page made of the value inside `selector`: how often it stands there as text, and
// whether any element of it (the image, a script with its body) was built.
async function asText(page, selector, value) {
  return page.evaluate(
    ([sel, text]) => {
      const root = document.querySelector(sel);
      if (!root) return { found: false, times: 0, elements: 0 };
      const content = root.textContent || "";
      const times = content.split(text).length - 1;
      const images = root.querySelectorAll('img[src="x"]').length;
      const scripts = [...root.querySelectorAll("script")].filter((s) => s.textContent === "alert(2)").length;
      return { found: true, times, elements: images + scripts };
    },
    [selector, value],
  );
}

async function open(page, args, path) {
  const response = await page.goto(new URL(path, args.base_url).href, { waitUntil: "networkidle" });
  await page.waitForTimeout(SETTLE_MS);
  const headers = response ? await response.allHeaders() : {};
  return {
    status: response ? response.status() : 0,
    policy: headers["content-security-policy"] || "",
    reportOnly: headers["content-security-policy-report-only"] || "",
  };
}

function enforced(check, label, answer) {
  check(
    `${label}: the page answers with the enforced policy`,
    answer.status === 200 && answer.policy.includes("script-src 'self'") && !answer.reportOnly &&
      !/unsafe-(inline|eval)/.test(answer.policy),
    answer,
  );
}

async function displayName(page, args, scheme, check, report) {
  enforced(check, "users", await open(page, args, "/users"));
  const frame = await asText(page, "#pk-sidebar, .pk-shell", args.value);
  check("display name: the frame shows the signed-in name as text", frame.times >= 1 && frame.elements === 0, frame);
  const list = await asText(page, "#pk-main", args.value);
  check("display name: the account list shows the name as text", list.times >= 1 && list.elements === 0, list);
  report.screenshots.push(...(await shoot(page, args, "hostile-users", scheme)));
}

async function collectionName(page, args, scheme, check, report) {
  enforced(check, "collections", await open(page, args, "/collections"));
  await page.locator("#collection-list [data-connection]").waitFor({ state: "attached" });
  await page.waitForTimeout(SETTLE_MS);
  const list = await asText(page, "#collection-list", args.value);
  check("collection name: the list shows the name as text", list.times >= 1 && list.elements === 0, list);
  report.screenshots.push(...(await shoot(page, args, "hostile-collections", scheme)));
}

async function notificationText(page, args, scheme, check, report) {
  enforced(check, "notifications", await open(page, args, "/mail"));
  const row = await asText(page, "#pk-main", args.value);
  check("notification: the recipient list shows the name as text", row.times >= 1 && row.elements === 0, row);
  // The test of the recipient fails with the value as its reason; the answer never leaves the
  // browser, so nothing is sent anywhere.
  await page.route("**/api/v1/notifications/recipients/*/test", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: false, reason: args.value }),
    }),
  );
  await page.locator("tr[data-recipient] [data-recipient-test]").first().click();
  await page.locator("#pk-toasts [role='alert'] > *").first().waitFor();
  await page.waitForTimeout(SETTLE_MS);
  const toast = await asText(page, "#pk-toasts", args.value);
  check("notification: the toast shows name and reason as text", toast.times >= 2 && toast.elements === 0, toast);
  report.screenshots.push(...(await shoot(page, args, "hostile-notifications", scheme)));
  await page.unroute("**/api/v1/notifications/recipients/*/test");
}

// How many images on the page point at the icon on another site.
async function offSiteImages(page, icon) {
  return page.evaluate((src) => [...document.querySelectorAll("img")].filter((img) => img.getAttribute("src") === src).length, icon);
}

async function offSiteIcon(page, args, scheme, check, report) {
  enforced(check, "dashboard", await open(page, args, "/"));
  const elsewhere = page.locator("#tile-gallery section", { hasText: "Elsewhere" });
  await elsewhere.waitFor();
  check("off-site icon: the dashboard shows no image from another site", (await offSiteImages(page, args.icon)) === 0,
    await offSiteImages(page, args.icon));
  const gallery = await elsewhere.evaluate((section) => [...section.querySelectorAll("a.card")].map((card) => {
    const img = card.querySelector("img");
    return img ? img.getAttribute("src") : card.querySelector("span")?.textContent.trim();
  }));
  check("off-site icon: the tiles show the badge and the bundled icon instead",
    JSON.stringify(gallery) === JSON.stringify(["W", "/static/icons/keycloak.svg"]), gallery);
  report.screenshots.push(...(await shoot(page, args, "offsite-icon-dashboard", scheme)));

  await page.locator("#dashboard-actions button", { hasText: "Edit overview" }).click();
  const card = page.locator("[data-group-list] [data-tile-id]", { hasText: "Wiki" }).first();
  await card.waitFor();
  check("off-site icon: the editor shows no image from another site", (await offSiteImages(page, args.icon)) === 0,
    await offSiteImages(page, args.icon));
  await card.click();
  const dialog = page.locator("#tile-editor-modal");
  const problem = dialog.locator("span.text-error", { hasText: "this site" });
  await problem.waitFor();
  check("off-site icon: the tile dialog names the problem and shows no image from another site",
    (await offSiteImages(page, args.icon)) === 0, await offSiteImages(page, args.icon));
  report.screenshots.push(...(await shoot(page, args, "offsite-icon-dialog", scheme)));
}

async function runScheme(browser, args, scheme, report) {
  const check = (name, ok, detail) => report.checks.push({ name: `${scheme}: ${name}`, ok: Boolean(ok), detail });
  const context = await browser.newContext({
    storageState: args.storage_state,
    locale: "en-GB",
    colorScheme: scheme,
    reducedMotion: "reduce",
    viewport: { width: 1440, height: 900 },
  });
  await context.addInitScript(collectViolations);
  const page = await context.newPage();
  const dialogs = [];
  const errors = [];
  const violations = [];
  page.on("dialog", (dialog) => {
    dialogs.push(`${dialog.type()}: ${dialog.message()}`);
    dialog.dismiss().catch(() => {});
  });
  page.on("pageerror", (error) => errors.push(String(error.message).split("\n")[0]));
  for (const [label, step] of [
    ["display name", displayName],
    ["collection name", collectionName],
    ["notification", notificationText],
    ["off-site icon", offSiteIcon],
  ]) {
    try {
      await step(page, args, scheme, check, report);
    } catch (error) {
      check(`${label}: the path runs to its end`, false, String(error.message).split("\n")[0]);
    }
    violations.push(...(await page.evaluate(() => (window.__papaiaCsp || []).splice(0))));
  }
  check("no dialog opened", dialogs.length === 0, dialogs);
  check("no report of the policy", violations.length === 0, violations);
  check("no script error", errors.length === 0, errors);
  await context.close();
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const playwright = pathToFileURL(resolve(REPO, ".pk/node_modules/playwright-core/index.mjs"));
  const { chromium } = await import(playwright.href);
  const browser = await chromium.launch();
  const report = { checks: [], screenshots: [] };
  try {
    for (const scheme of SCHEMES) await runScheme(browser, args, scheme, report);
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(report, null, 2) + "\n");
  return report.checks.every((c) => c.ok) ? 0 : 1;
}

main().then(
  (code) => process.exit(code),
  (error) => {
    process.stderr.write(`csp-hostile: ${error.stack || error}\n`);
    process.exit(2);
  },
);
