// Browser path of Embedding, Collections and Models & Usage on the CSP build of Alpine (T-UI-03,
// oberfl 3.3), driven by tests/test_rag_browser.py against the manager with the stand-in RAG
// system of tests/rag_serve.py (signed-in test administrator).
//
// In both colour schemes: a collection is created through its dialog and deleted again through
// the row menu and the typed confirmation; on the Embedding page the documents folder is picked,
// the collection is replaced with another embedding model after the typed confirmation, the run
// strip shows the run working, Abort ends it and the page takes the end from the strip; on Models
// & Usage the bars get their heights without inline styles and the chart's tooltip follows the
// pointer and the arrow keys. Throughout, the pages must log no report of the content security
// policy, no Alpine expression error and no script error.
//
// Usage: node tests/rag_browser.mjs --base-url URL --storage-state FILE [--shots DIR]
// Prints a JSON report; exit 0 when every check holds, 1 otherwise, 2 on usage errors.
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SCHEMES = ["light", "dark"];
const WIDTHS = [1440, 390];

function usage(message) {
  process.stderr.write(`rag-check: ${message}\n`);
  process.exit(2);
}

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 2) {
    const [flag, value] = [argv[i], argv[i + 1]];
    if (!["--base-url", "--storage-state", "--shots"].includes(flag) || value === undefined) {
      usage(`bad argument ${flag}`);
    }
    args[flag.slice(2).replace("-", "_")] = value;
  }
  if (!args.base_url || !args.storage_state) usage("--base-url and --storage-state are required");
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

async function strictState(page) {
  return page.evaluate(() => ({
    marker: document.querySelector('meta[name="papaia-csp"]')?.getAttribute("content") || null,
    alpine: window.Alpine ? String(window.Alpine.version) : null,
  }));
}

async function toastText(page) {
  return page.locator("#pk-toasts").innerText();
}

async function collections(page, args, scheme, check, report, violations) {
  const name = `itk-${scheme}-docs`;
  await page.goto(new URL("/collections", args.base_url).href, { waitUntil: "networkidle" });
  await page.locator("#collection-list [data-connection]").waitFor({ state: "attached" });
  const strict = await strictState(page);
  check("collections: the page runs on the CSP build", strict.marker === "strict" && strict.alpine, strict);
  report.screenshots.push(...(await shoot(page, args, "collections", scheme)));

  await page.locator("[data-collection-new]").click();
  const create = page.locator("#create-collection-modal");
  await create.locator('input[x-model="name"]').fill(name);
  await create.locator('input[x-model="vectorSize"]').fill("4");
  await create.locator('input[x-model="model"]').fill("bge-m3");
  await create.locator('button[type="submit"]').click();
  const card = page.locator("#collection-list .card", { has: page.locator("span.font-mono", { hasText: name }) });
  await card.first().waitFor();
  check("collections: a new collection is created and listed", (await card.count()) === 1, name);
  check("collections: the creation is confirmed", (await toastText(page)).includes(`Collection ${name} created`));

  await card.locator(`[aria-label="More actions for collection ${name}"]`).click();
  await card.locator("a.text-error", { hasText: "Delete" }).click();
  const remove = page.locator("#delete-collection-modal");
  await remove.locator(".modal-box").waitFor();
  const title = (await remove.locator("h3").innerText()).trim();
  check("collections: the delete dialog names the collection", title === `Delete ${name}?`, title);
  const mono = await remove.locator("h3 span.font-mono").count();
  check("collections: the name is set in its own element, not as markup", mono === 1, mono);
  report.screenshots.push(...(await shoot(page, args, "collections-delete", scheme)));
  const button = remove.locator('button[type="submit"]');
  check("collections: delete waits for the typed name", await button.isDisabled());
  await remove.locator('input[x-model="typed"]').fill(name);
  await button.click();
  await card.first().waitFor({ state: "detached" });
  check("collections: the collection is deleted and gone from the list", (await card.count()) === 0);
  const seen = await violations();
  check("collections: no report of the policy", seen.length === 0, seen);
}

async function embedding(page, args, scheme, check, report, violations) {
  await page.goto(new URL("/embedding?collection=kb", args.base_url).href, { waitUntil: "networkidle" });
  await page.locator("#embed-status[data-announce]").waitFor({ state: "attached" });
  const strict = await strictState(page);
  check("embedding: the page runs on the CSP build", strict.marker === "strict" && strict.alpine, strict);
  report.screenshots.push(...(await shoot(page, args, "embedding", scheme)));

  await page.getByRole("tab", { name: "Folder" }).click();
  await page.locator("#embed-folder-tree li").first().waitFor();
  await page.locator("label", { hasText: "The whole folder" }).locator("input").check();
  const model = page.locator('#embed-body input[x-model="model"]');
  check("embedding: add keeps the recorded model", (await model.getAttribute("readonly")) !== null);
  await page.locator('input[type="radio"][value="replace"]').check();
  check("embedding: replace unlocks the recorded model", (await model.getAttribute("readonly")) === null);
  await model.fill("bge-m3");
  await page.getByRole("button", { name: "Start embedding" }).click();
  const confirm = page.locator("#embed-confirm");
  await confirm.locator(".modal-box").waitFor();
  const title = (await confirm.locator("h3").innerText()).trim();
  check("embedding: the confirmation names the collection", title === "Replace kb?", title);
  const points = (await confirm.locator("p.mb-2").first().innerText()).replace(/\s+/g, " ");
  check("embedding: the confirmation names the points that go", points.includes("(12 points)"), points);
  report.screenshots.push(...(await shoot(page, args, "embedding-confirm", scheme)));
  const replace = confirm.locator('button[type="submit"]');
  check("embedding: replace waits for the typed name", await replace.isDisabled());
  await confirm.locator("#embedding-confirm-name").fill("kb");
  await replace.click();
  await page.locator('#embed-status[data-active="true"]').waitFor({ state: "attached" });
  check("embedding: the run is started and confirmed", (await toastText(page)).includes("Embedding started for 2 files"));
  const working = page.getByText("A run is working on this collection.");
  check("embedding: the page knows the run works", await working.isVisible());
  report.screenshots.push(...(await shoot(page, args, "embedding-running", scheme)));

  await page.locator("#embed-status").getByRole("button", { name: "Abort" }).click();
  await page.locator('#embed-status[data-active="false"]').waitFor({ state: "attached" });
  await working.waitFor({ state: "hidden" });
  check("embedding: the strip ends the run and the page takes it", !(await working.isVisible()));
  check("embedding: the abort is explained", (await toastText(page)).includes("Abort requested."));
  const seen = await violations();
  check("embedding: no report of the policy", seen.length === 0, seen);
}

async function models(page, args, scheme, check, report, violations) {
  await page.goto(new URL("/models", args.base_url).href, { waitUntil: "networkidle" });
  const chart = page.locator('[x-data="usageChart"] [role="group"]');
  await chart.waitFor();
  const strict = await strictState(page);
  check("models: the page runs on the CSP build", strict.marker === "strict" && strict.alpine, strict);
  const columns = chart.locator(".absolute.inset-0.flex > div");
  const count = await columns.count();
  const heights = await chart.locator("[data-pct-height]").evaluateAll((bars) =>
    bars.map((bar) => [bar.getAttribute("data-pct-height"), bar.style.height]));
  check("models: the bars get their heights", heights.length > 0 &&
    heights.every(([pct, height]) => height === `${Number(pct)}%`), heights);
  await columns.nth(count - 1).hover();
  const tip = page.locator('[x-data="usageChart"] p[x-text="tipTitle()"]');
  await tip.waitFor();
  const last = (await tip.innerText()).trim();
  check("models: the tooltip shows the day under the pointer", last.length > 0, last);
  report.screenshots.push(...(await shoot(page, args, "models", scheme)));
  await chart.focus();
  await page.keyboard.press("ArrowLeft");
  const previous = (await tip.innerText()).trim();
  check("models: the arrow keys move the tooltip", previous !== last && previous.length > 0, previous);
  await page.keyboard.press("Escape");
  await tip.waitFor({ state: "hidden" });
  const seen = await violations();
  check("models: no report of the policy", seen.length === 0, seen);
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
  const alpine = [];
  const errors = [];
  page.on("console", (message) => {
    const text = message.text();
    if (text.startsWith("Alpine Expression Error") || text.startsWith("Alpine Warning")) alpine.push(text.split("\n")[0]);
  });
  page.on("pageerror", (error) => errors.push(String(error.message).split("\n")[0]));
  report.alpine[scheme] = alpine;
  report.errors[scheme] = errors;
  const violations = async () => page.evaluate(() => window.__papaiaCsp.splice(0));
  for (const [label, step] of [["collections", collections], ["embedding", embedding], ["models", models]]) {
    try {
      await step(page, args, scheme, check, report, violations);
    } catch (error) {
      // A step that never came (a timeout) is a failed check, reported with the others.
      check(`${label}: the path runs to its end`, false, String(error.message).split("\n")[0]);
    }
  }
  check("no Alpine expression error", alpine.length === 0, alpine);
  check("no script error", errors.length === 0, errors);
  await context.close();
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const playwright = pathToFileURL(resolve(REPO, ".pk/node_modules/playwright-core/index.mjs"));
  const { chromium } = await import(playwright.href);
  const browser = await chromium.launch();
  const report = { checks: [], screenshots: [], alpine: {}, errors: {} };
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
    process.stderr.write(`rag-check: ${error.stack || error}\n`);
    process.exit(2);
  },
);
