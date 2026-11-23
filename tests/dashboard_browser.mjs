// Browser path of the dashboard and its tile editor on the CSP build of Alpine (T-UI-03,
// oberfl 3.3), driven by tests/test_dashboard_browser.py against the manager in the stand-in
// world (tests/design_serve.py, signed-in test administrator).
//
// In both colour schemes: the gallery loads its tiles; the editor opens from the gallery,
// adds a link tile through its dialog, moves it with the tile menu, removes a tile and brings
// it back with the undo strip, removes it again and saves; the gallery comes back with the
// saved order, which a reload confirms. Once, a widget is added and its card loads and
// refreshes on its own interval. Throughout, the page must log no report of the content
// security policy against a script, no Alpine expression error and no script error. Reports
// against `style` attributes are counted per view: the editor's partials still carry inline
// `style` attributes, which the ticket that enforces the policy moves into classes.
//
// Usage: node tests/dashboard_browser.mjs --base-url URL --storage-state FILE [--shots DIR]
// Prints a JSON report; exit 0 when every check holds, 1 otherwise, 2 on usage errors.
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SCHEMES = ["light", "dark"];
const WIDTHS = [1440, 390];

function usage(message) {
  process.stderr.write(`dashboard-check: ${message}\n`);
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

// Runs in the page: the names of the cards of the first group, gallery or editor.
function firstGroupNames() {
  const editor = document.querySelector("[data-tile-list]");
  if (editor) {
    return [...editor.querySelectorAll(":scope > [data-tile-id]")].map((card) =>
      card.querySelector("p.font-semibold").textContent.trim());
  }
  const grid = document.querySelector("#tile-gallery section .grid");
  if (!grid) return null;
  return [...grid.children].map((card) => {
    const name = card.querySelector("p.font-semibold");
    return name ? name.textContent.trim() : "";
  });
}

function exactly(text) {
  return new RegExp(`^\\s*${text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*$`);
}

function tileCard(page, name) {
  return page.locator("[data-tile-list]").first().locator(":scope > [data-tile-id]")
    .filter({ has: page.locator("p.font-semibold", { hasText: exactly(name) }) });
}

async function tileMenu(page, name, item) {
  const card = tileCard(page, name);
  await card.locator('[aria-label="Tile actions"]').click();
  await card.locator("ul.menu a", { hasText: item }).click();
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

async function runScheme(browser, args, scheme, withWidget, report) {
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
  const widgetLoads = [];
  page.on("console", (message) => {
    const text = message.text();
    if (text.startsWith("Alpine Expression Error") || text.startsWith("Alpine Warning")) alpine.push(text.split("\n")[0]);
  });
  page.on("pageerror", (error) => errors.push(String(error.message).split("\n")[0]));
  report.alpine[scheme] = alpine;
  report.errors[scheme] = errors;
  page.on("dialog", (dialog) => dialog.accept());
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (/^\/partials\/widgets\/[^/]+$/.test(path) && !path.includes("/preview/")) widgetLoads.push(path);
  });
  const violations = async () => page.evaluate(() => window.__papaiaCsp.splice(0));
  const scriptReports = (list) => list.filter((v) => !v.directive.startsWith("style-src"));
  const styleReports = (list) => list.filter((v) => v.directive.startsWith("style-src")).length;

  await page.clock.install();
  await page.goto(new URL("/", args.base_url).href, { waitUntil: "networkidle" });
  await page.locator("#tile-gallery section").first().waitFor();
  const strict = await page.evaluate(() => ({
    marker: document.querySelector('meta[name="papaia-csp"]')?.getAttribute("content") || null,
    alpine: window.Alpine ? String(window.Alpine.version) : null,
    store: Boolean(window.Alpine && window.Alpine.store("tiles")),
  }));
  check("the dashboard runs on the CSP build with the editor's store", strict.marker === "strict" && strict.alpine && strict.store, strict);
  const initial = await page.evaluate(firstGroupNames);
  check("the gallery loads its tiles", Array.isArray(initial) && initial.length >= 1, initial);
  let seen = await violations();
  report.views.push({ scheme, view: "gallery", style_reports: styleReports(seen) });
  check("gallery: no report of the policy", seen.length === 0, seen);
  report.screenshots.push(...(await shoot(page, args, "dashboard", scheme)));

  // The editor, opened from the gallery's own button.
  await page.locator("#dashboard-actions button", { hasText: "Edit overview" }).click();
  await page.locator("[data-group-list] [data-tile-id]").first().waitFor();
  check("the editor shows the same tiles", JSON.stringify(await page.evaluate(firstGroupNames)) === JSON.stringify(initial),
    await page.evaluate(firstGroupNames));
  report.screenshots.push(...(await shoot(page, args, "editor", scheme)));

  // Once: a group with a widget card, written through the raw file editor (JSON is YAML).
  // The stand-in world registers widget types it cannot serve, so the widget dialog offers
  // none to pick; the raw editor takes any registered type, as the server checks it.
  let widget = null;
  if (withWidget) {
    widget = await page.evaluate(() => {
      const type = window.Alpine.store("tiles").widgetTypes[0];
      return type ? { id: type.id, name: type.name } : null;
    });
    report.widget = widget;
    check("the editor knows the registered widget types", widget !== null, widget);
    await page.locator('#dashboard-actions [aria-label="More dashboard actions"]').click();
    await page.locator("#dashboard-actions ul.menu a", { hasText: "Edit tiles.yaml" }).click();
    const rawModal = page.locator("#tiles-raw-modal");
    await rawModal.locator("textarea").waitFor();
    const text = await page.evaluate((id) => {
      const store = window.Alpine.store("tiles");
      const groups = store.groups.map((group) => ({
        name: group.name,
        tiles: group.tiles.map((t) => ({ name: t.name, href: t.href, description: t.description, icon: t.icon || null, visibility: t.visibility })),
      }));
      groups.push({ name: "State", tiles: [{ type: "widget", widget: id, size: "s", visibility: "admin" }] });
      return JSON.stringify({ version: 1, groups }, null, 2);
    }, widget.id);
    await rawModal.locator("textarea").fill(text);
    report.screenshots.push(...(await shoot(page, args, "raw-dialog", scheme)));
    await rawModal.locator(".modal-action button", { hasText: "Save file" }).click();
    await page.locator('#pk-toasts [role="status"]', { hasText: "tiles.yaml saved." }).waitFor();
    const reloaded = await page.waitForFunction(() => {
      const inputs = document.querySelectorAll('[data-group-list] input[aria-label="Group name"]');
      return inputs.length && inputs[inputs.length - 1].value === "State";
    }, null, { timeout: 10_000 }).then(() => true, () => false);
    check("the raw file is saved and the editor reloads with the widget group", reloaded);
  }

  // Add a link tile through the dialog.
  const added = `Docs ${scheme}`;
  await page.locator("[data-group-list] section").first().locator("button", { hasText: "+ Add link to" }).click();
  const dialog = page.locator("#tile-editor-modal");
  await dialog.locator("h3", { hasText: "Add tile" }).waitFor();
  await dialog.locator('input[placeholder="LibreChat"]').fill(added);
  await dialog.locator("input.font-mono").fill("https://docs.example.test/");
  await page.clock.runFor(400);
  await dialog.locator("p.font-mono", { hasText: "https://docs.example.test/" }).waitFor();
  report.screenshots.push(...(await shoot(page, args, "tile-dialog", scheme)));
  await dialog.locator(".modal-action button", { hasText: "Apply" }).click();
  check("the dialog closes", !(await dialog.evaluate((el) => el.open)));
  const last = initial[initial.length - 1];
  check("the tile is added to the group", JSON.stringify(await page.evaluate(firstGroupNames)) === JSON.stringify([...initial, added]),
    await page.evaluate(firstGroupNames));

  // Move it one place left with the tile menu.
  await tileMenu(page, added, "Move left");
  const moved = [...initial.slice(0, -1), added, last];
  check("the tile menu moves it left", JSON.stringify(await page.evaluate(firstGroupNames)) === JSON.stringify(moved),
    await page.evaluate(firstGroupNames));

  // Remove the tile it passed, undo, remove again.
  await tileMenu(page, last, "Remove tile");
  const undo = page.locator("button", { hasText: `Undo: removed “${last}”` });
  await undo.waitFor();
  const bar = await page.locator("div.sticky span.text-sm > span").first().evaluate((el) => ({
    text: el.textContent,
    bold: el.querySelector("span.font-semibold")?.textContent || null,
  }));
  check("the bar counts the unsaved changes with the number in bold", bar.text === "3 unsaved changes" && bar.bold === "3", bar);
  await undo.click();
  check("undo brings the tile back", JSON.stringify(await page.evaluate(firstGroupNames)) === JSON.stringify(moved),
    await page.evaluate(firstGroupNames));
  await tileMenu(page, last, "Remove tile");
  const expected = [...initial.slice(0, -1), added];
  check("the tile is removed", JSON.stringify(await page.evaluate(firstGroupNames)) === JSON.stringify(expected),
    await page.evaluate(firstGroupNames));

  // Once: the widget dialog lists the registered types, each with its reason when it cannot
  // be used here, and closes without a change.
  if (withWidget) {
    await page.locator("[data-group-list] section").first().locator("button", { hasText: "+ Add widget" }).click();
    const modal = page.locator("#widget-modal");
    await modal.locator("h3", { hasText: "Add a widget" }).waitFor();
    const listed = await modal.locator(".grid > button").evaluateAll((buttons) =>
      buttons.map((b) => b.querySelector("span.font-semibold").textContent.trim()));
    check("the widget dialog lists the registered types", listed.includes(widget.name), listed);
    report.screenshots.push(...(await shoot(page, args, "widget-dialog", scheme)));
    await modal.locator(".modal-action button", { hasText: "Cancel" }).click();
    check("the widget dialog closes", !(await modal.evaluate((el) => el.open)));
  }
  seen = await violations();
  report.views.push({ scheme, view: "editor", style_reports: styleReports(seen) });
  check("editor: no report of the policy against a script", scriptReports(seen).length === 0, scriptReports(seen));

  // Save: the gallery comes back with the saved order.
  const [saved] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith("/api/v1/tiles") && r.request().method() === "PUT"),
    page.locator("#dashboard-actions button.btn-primary", { hasText: "Save changes" }).click(),
  ]);
  check("the draft is saved", saved.status() === 200);
  await page.locator("#dashboard-actions button", { hasText: "Edit overview" }).waitFor();
  const gallery = await page.evaluate(firstGroupNames);
  const expectedGallery = expected;
  check("the gallery shows the saved order", JSON.stringify(gallery) === JSON.stringify(expectedGallery), gallery);
  const toast = await page.locator('#pk-toasts [role="status"]').textContent();
  check("the save is confirmed in the status region", toast.includes("Overview saved."), toast);

  if (widget) {
    // The card loads, then refreshes on its own interval (`every Ns` of its slot).
    const slot = page.locator("#tile-gallery [id^='widget-']").first();
    await slot.locator(".card").first().waitFor();
    const trigger = await slot.getAttribute("hx-trigger");
    const seconds = Number((/every (\d+)s/.exec(trigger) || [])[1]);
    const before = widgetLoads.length;
    await page.clock.runFor((seconds + 1) * 1000);
    await page.waitForTimeout(300);
    check("the widget card loads and refreshes on its interval", before >= 1 && widgetLoads.length > before,
      { trigger, before, after: widgetLoads.length });
  }

  await page.reload({ waitUntil: "networkidle" });
  await page.locator("#tile-gallery section").first().waitFor();
  check("a reload shows the saved order", JSON.stringify(await page.evaluate(firstGroupNames)) === JSON.stringify(expectedGallery),
    await page.evaluate(firstGroupNames));
  if (widget) report.screenshots.push(...(await shoot(page, args, "dashboard-saved", scheme)));
  seen = await violations();
  check("saved gallery: no report of the policy", seen.length === 0, seen);
  check("no Alpine expression error", alpine.length === 0, alpine);
  check("no script error", errors.length === 0, errors);
  await context.close();
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const playwright = pathToFileURL(resolve(REPO, ".pk/node_modules/playwright-core/index.mjs"));
  const { chromium } = await import(playwright.href);
  const browser = await chromium.launch();
  const report = { checks: [], views: [], screenshots: [], widget: null, alpine: {}, errors: {} };
  try {
    for (const [i, scheme] of SCHEMES.entries()) {
      try {
        await runScheme(browser, args, scheme, i === 0, report);
      } catch (error) {
        // A step that never came (a timeout) is a failed check, reported with the others.
        report.checks.push({ name: `${scheme}: the path runs to its end`, ok: false, detail: String(error.message).split("\n")[0] });
      }
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
    process.stderr.write(`dashboard-check: ${error.stack || error}\n`);
    process.exit(2);
  },
);
