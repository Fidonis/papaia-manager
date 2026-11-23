// Browser path of the knowledge map on the CSP build of Alpine (T-UI-03), run by
// tests/test_knowledge_map_browser.py.
//
// Opens /knowledge-map in Chromium (playwright-core of .pk/) once per theme and records what
// the report-only policy reports, the expressions Alpine refuses and uncaught script errors,
// as tests/csp_browser.mjs does. On the page it walks the browser path of the ticket: the map
// renders, "View as" dims what a role does not see, the search of the entries filters, a box
// opens its detail by click and by keyboard, and every tab renders. --shots DIR also writes
// screenshots at 1440 and 390 px per theme.
//
// Usage (from the repository root):
//   node tests/knowledge_map_browser.mjs --base-url URL --storage-state FILE
//     [--report FILE] [--shots DIR]
// Exit: 0 clean, 1 a finding or a failed step, 2 usage or a page that does not load.
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const PATH = "/knowledge-map";
const SETTLE_MS = 300;

function usage(message) {
  process.stderr.write(`knowledge-map-browser: ${message}\n`);
  process.exit(2);
}

function parseArgs(argv) {
  const args = {};
  const known = new Set(["--base-url", "--storage-state", "--report", "--shots"]);
  for (let i = 0; i < argv.length; i += 2) {
    const [flag, value] = [argv[i], argv[i + 1]];
    if (!known.has(flag)) usage(`unknown argument ${flag}`);
    if (value === undefined) usage(`${flag} needs a value`);
    args[flag.slice(2).replace("-", "_")] = value;
  }
  if (!args.base_url || !args.storage_state) usage("--base-url and --storage-state are required");
  const base = new URL(args.base_url);
  if (!["127.0.0.1", "localhost"].includes(base.hostname)) usage("the base URL must be the loopback");
  return args;
}

// Runs in the page before any of its scripts: collects the reports of the policy.
function collectViolations() {
  window.__papaiaCsp = [];
  document.addEventListener("securitypolicyviolation", (event) => {
    window.__papaiaCsp.push({
      directive: event.effectiveDirective,
      blocked: event.blockedURI,
      sample: event.sample,
    });
  });
}

async function walk(page) {
  const steps = [];
  const step = async (name, run) => {
    try {
      await run();
      steps.push({ name, ok: true });
    } catch (error) {
      steps.push({ name, ok: false, problem: String(error.message || error).split("\n")[0] });
    }
  };
  const expect = (condition, message) => {
    if (!condition) throw new Error(message);
  };
  const graph = page.locator("#km-graph");
  const detail = page.locator("section aside h3").first();
  const tab = (label) => page.locator('[role="tablist"] [role="tab"]', { hasText: label }).first();

  await step("the map renders four lanes and the boxes of the entries", async () => {
    await graph.locator("g.node").first().waitFor({ state: "visible", timeout: 10000 });
    const lanes = await graph.locator("text.lane").allTextContents();
    expect(lanes.length === 4, `lanes: ${lanes.join(", ")}`);
    const labels = await graph.locator("g.node").evaluateAll((gs) => gs.map((g) => g.getAttribute("aria-label")));
    for (const name of ["Processes", "Company wiki", "Quality binder"]) {
      expect(labels.includes(name), `no box "${name}" in ${labels.join(", ")}`);
    }
    expect((await graph.locator("path.edge").count()) > 0, "no edge");
  });

  await step("a click on a box opens its detail", async () => {
    await graph.locator('g.node[aria-label="Company wiki"]').click();
    await page.waitForTimeout(SETTLE_MS);
    expect((await detail.textContent()).trim() === "Company wiki", `detail: ${await detail.textContent()}`);
    expect(await graph.locator('g.node.sel[aria-label="Company wiki"]').count() === 1, "box not marked selected");
  });

  await step("the keyboard reaches a box and opens it with Enter and Space", async () => {
    const binder = graph.locator('g.node[aria-label="Quality binder"]');
    await binder.focus();
    expect(await binder.evaluate((g) => g === document.activeElement), "box not focusable");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(SETTLE_MS);
    expect((await detail.textContent()).trim() === "Quality binder", `after Enter: ${await detail.textContent()}`);
    await page.keyboard.press("Shift+Tab");
    const focused = await page.evaluate(() => document.activeElement.getAttribute("aria-label"));
    expect(focused && focused !== "Quality binder", "Shift+Tab did not move to another box");
    await page.keyboard.press(" ");
    await page.waitForTimeout(SETTLE_MS);
    const opened = (await page.locator("section aside").first().textContent()) || "";
    expect(opened.includes(focused === "Processes" ? "org.processes" : focused.split(" · ").pop()),
      `after Space on "${focused}": ${opened.trim().slice(0, 80)}`);
  });

  await step("viewing as a plain user dims what needs another role", async () => {
    const select = page.locator("section select").first();
    await select.selectOption("user");
    await page.waitForTimeout(SETTLE_MS);
    const dimmed = await graph.locator("g.node.dim").evaluateAll((gs) => gs.map((g) => g.getAttribute("aria-label")));
    expect(dimmed.some((l) => l.startsWith("RAG")), `dimmed: ${dimmed.join(", ")}`);
    await select.selectOption("*");
    await page.waitForTimeout(SETTLE_MS);
    expect(await graph.locator("g.node.dim").count() === 0, "boxes still dimmed for the administrator");
  });

  await step("the search of the entries filters, a row opens on the map", async () => {
    await tab("Entries").click();
    const rows = page.locator("table tbody tr");
    const search = page.locator('input[type="search"]');
    await search.fill("binder");
    await page.waitForTimeout(SETTLE_MS);
    expect(await rows.count() === 1, `rows for "binder": ${await rows.count()}`);
    await search.fill("no such entry");
    await page.waitForTimeout(SETTLE_MS);
    expect(await rows.count() === 0, "rows for a search without a match");
    await search.fill("wiki");
    await page.waitForTimeout(SETTLE_MS);
    await rows.first().click();
    await page.waitForTimeout(SETTLE_MS);
    expect(await graph.isVisible(), "the map tab did not open");
    expect((await detail.textContent()).trim() === "Company wiki", "the row did not select its entry");
  });

  await step("the routing test shows the ranked result", async () => {
    await tab("Test routing").click();
    await page.locator('input[maxlength="500"][required]').first().fill("escalation procedure");
    await page.locator("form button.btn-primary", { hasText: "Route" }).click();
    const card = page.locator("section .rounded-box", { hasText: "Company wiki" }).first();
    await card.waitFor({ state: "visible", timeout: 5000 });
    expect((await card.textContent()).includes("0.80"), `card: ${(await card.textContent()).trim()}`);
  });

  await step("usage shows the abstention rate", async () => {
    await tab("Usage").click();
    await page.getByText("25 %").first().waitFor({ state: "visible", timeout: 5000 });
  });

  await step("suggestions list what the map server mined", async () => {
    await tab("Suggestions").click();
    await page.getByText("Add an example").first().waitFor({ state: "visible", timeout: 5000 });
  });

  await step("an operator file opens and a draft is discarded", async () => {
    await tab("Files").click();
    const first = page.locator("ul.menu a").first();
    await first.waitFor({ state: "visible", timeout: 5000 });
    await first.click();
    const area = page.locator("textarea.font-mono");
    const saved = await area.inputValue();
    await area.fill(saved + "\n# draft");
    const discard = page.locator("button", { hasText: "Discard changes" });
    await discard.click();
    await page.waitForTimeout(SETTLE_MS);
    expect((await area.inputValue()) === saved, "the draft was not discarded");
  });

  await step("governance renders its views", async () => {
    await tab("Governance").click();
    const sub = page.locator(".tabs-boxed [role=tab]");
    await sub.first().waitFor({ state: "visible", timeout: 10000 });
    for (let i = 0; i < (await sub.count()); i++) {
      await sub.nth(i).click();
      await page.waitForTimeout(SETTLE_MS);
    }
  });

  await step("the editor opens for a new entry", async () => {
    await tab("Entries").click();
    await page.locator("button", { hasText: "New entry" }).click();
    await page.locator("button", { hasText: "A place without a tool" }).click();
    await page.waitForTimeout(SETTLE_MS);
    expect(await page.locator("aside h3", { hasText: "Quality" }).count() === 1, "no quality panel");
    await page.locator("form button", { hasText: "Cancel" }).first().click();
  });

  await tab("Map").click();
  await page.waitForTimeout(SETTLE_MS);
  return steps;
}

async function checkTheme(browser, args, theme) {
  const context = await browser.newContext({
    storageState: args.storage_state,
    colorScheme: theme,
    viewport: { width: 1440, height: 1000 },
  });
  await context.addInitScript(collectViolations);
  const page = await context.newPage();
  // A step that cannot find its element fails within seconds rather than at the default 30 s.
  page.setDefaultTimeout(5000);
  const alpine = new Set();
  const errors = [];
  page.on("console", (message) => {
    const text = message.text();
    if (text.startsWith("Alpine Expression Error") || text.startsWith("Alpine Warning")) {
      alpine.add(text.split("\n").slice(0, 3).join(" | "));
    }
  });
  page.on("pageerror", (error) => errors.push(String(error.message).split("\n")[0]));
  const response = await page.goto(new URL(PATH, args.base_url).href, { waitUntil: "networkidle" });
  if (!response || response.status() !== 200) {
    await context.close();
    return { theme, status: response ? response.status() : 0 };
  }
  const converted = await page.evaluate(
    () => document.querySelector('meta[name="papaia-csp"]')?.getAttribute("content") === "strict",
  );
  const steps = await walk(page);
  if (args.shots) {
    mkdirSync(args.shots, { recursive: true });
    await page.screenshot({ path: resolve(args.shots, `knowledge-map-1440-${theme}.png`), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(SETTLE_MS);
    await page.screenshot({ path: resolve(args.shots, `knowledge-map-390-${theme}.png`), fullPage: true });
  }
  const result = {
    theme,
    status: 200,
    converted,
    alpine_version: await page.evaluate(() => (window.Alpine ? String(window.Alpine.version) : null)),
    nodes: await page.locator("#km-graph g.node").count(),
    violations: await page.evaluate(() => window.__papaiaCsp || []),
    alpine: [...alpine].sort(),
    errors,
    steps: steps.length,
    passed: steps.filter((s) => s.ok).length,
    failed: steps.filter((s) => !s.ok),
  };
  await context.close();
  return result;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const playwright = pathToFileURL(resolve(REPO, ".pk/node_modules/playwright-core/index.mjs"));
  const { chromium } = await import(playwright.href);
  const browser = await chromium.launch();
  const results = [];
  try {
    for (const theme of ["light", "dark"]) results.push(await checkTheme(browser, args, theme));
  } finally {
    await browser.close();
  }
  if (args.report) writeFileSync(args.report, JSON.stringify({ results }, null, 2) + "\n");
  let code = 0;
  for (const r of results) {
    if (r.status !== 200) {
      process.stderr.write(`knowledge-map-browser: ${PATH} answered ${r.status} (${r.theme})\n`);
      return 2;
    }
    const clean = r.converted && !r.violations.length && !r.alpine.length && !r.errors.length;
    process.stdout.write(`${r.theme}: ${r.passed}/${r.steps} steps, ${r.nodes} boxes, ` +
      `${r.violations.length} policy reports, ${r.alpine.length} Alpine refusals, ${r.errors.length} script errors\n`);
    if (!clean || r.failed.length) {
      code = 1;
      if (!r.converted) process.stderr.write(`  ${r.theme}: the page is not marked converted\n`);
      for (const v of r.violations) process.stderr.write(`  ${r.theme} policy: ${v.directive} ${v.blocked} ${v.sample || ""}\n`);
      for (const a of r.alpine) process.stderr.write(`  ${r.theme} alpine: ${a}\n`);
      for (const e of r.errors) process.stderr.write(`  ${r.theme} error: ${e}\n`);
      for (const s of r.failed) process.stderr.write(`  ${r.theme} step "${s.name}": ${s.problem}\n`);
    }
  }
  return code;
}

main().then(
  (code) => process.exit(code),
  (error) => {
    process.stderr.write(`knowledge-map-browser: ${error.stack || error}\n`);
    process.exit(2);
  },
);
