// Every page settles for the kit's browser checks, and the job editor stays quiet (MGR-034).
// Run by tests/test_settle_browser.py against tests/design_serve.py.
//
// The kit's design check waits on each page for `settle` of the vendored kit
// (.pk/package/src/conform/collect.ts): the fonts, then the `finished` promise of every finite
// animation, without a limit. The hidden part of a closed <details> is not rendered, and
// Chromium never advances an animation there: the checkmark of a box Alpine ticks after the
// load, inside the closed section "Processing" of the job editor, never finished, and the
// design check hung on /ingest/new whenever the box's style was computed in that state.
//
// Settle: for each person and page, in Chromium (playwright-core of .pk/) with reduced motion
// (as pk-conform) and without: open the page, let it settle, then change the state of every
// box and option inside a closed section (ticked off and on again, as the page left it) and
// read the computed style of everything in there, as the design rules read every element.
// `settle` must then return within SETTLE_MS; a page where it does not is reported with the
// animations still running.
// Idle (--idle PATH): open PATH and leave it alone for IDLE_MS after the load; the page's own
// requests to /api/ are reported, the frame's partials are not.
//
// Usage (from the repository root):
//   node tests/settle_browser.mjs --base-url URL --pages FILE --report FILE [--idle PATH]
//   FILE of --pages: {"<person>": {"state": "<storage state>", "pages": ["/path", ...]}, ...}
// Exit: 0 the run finished (the report says what it found), 1 it could not run, 2 usage.
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const OPEN_MS = 30000;
const SETTLE_MS = 5000;
const IDLE_MS = 10000;
const MOTIONS = ["reduce", "no-preference"];
const FLAGS = ["--base-url", "--pages", "--report", "--idle"];

function usage(message) {
  process.stderr.write(`settle-browser: ${message}\n`);
  process.exit(2);
}

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 2) {
    const [flag, value] = [argv[i], argv[i + 1]];
    if (!FLAGS.includes(flag) || value === undefined) usage(`bad argument ${flag}`);
    args[flag.slice(2).replace("-", "_")] = value;
  }
  if (!args.base_url || !args.pages || !args.report) usage("--base-url, --pages and --report are required");
  if (!["127.0.0.1", "localhost"].includes(new URL(args.base_url).hostname)) usage("the base URL must be the loopback");
  return args;
}

// In the page: a state change inside every closed section, then a read of every style there.
function touchClosedSections() {
  let touched = 0;
  for (const details of document.querySelectorAll("details:not([open])")) {
    for (const input of details.querySelectorAll("input[type=checkbox], input[type=radio]")) {
      const was = input.checked;
      input.checked = !was;
      getComputedStyle(input).animationName;
      input.checked = was;
      touched += 1;
    }
    for (const el of details.querySelectorAll("*")) getComputedStyle(el).animationName;
  }
  return touched;
}

// In the page: the finite animations the page still holds, their state and where they are. One
// that waits in a closed section says "finished" and still never resolves its promise.
function heldAnimations() {
  return document.getAnimations()
    .filter((a) => a.effect && a.effect.getTiming().iterations !== Infinity)
    .map((a) => {
      const target = a.effect.target;
      const where = target ? `${target.tagName.toLowerCase()}${target.className ? "." + String(target.className).trim().split(/\s+/).join(".") : ""}` : "";
      return `${a.animationName || a.transitionProperty || "animation"} (${a.playState}) on ${where}`;
    });
}

async function within(ms, promise) {
  let timer;
  const limit = new Promise((resolveLimit) => { timer = setTimeout(() => resolveLimit("limit"), ms); });
  try {
    return await Promise.race([promise.then(() => "settled"), limit]);
  } finally {
    clearTimeout(timer);
  }
}

async function settlePages(browser, base, persons, settle, report) {
  for (const motion of MOTIONS) {
    for (const [person, { state, pages }] of Object.entries(persons)) {
      const context = await browser.newContext({ reducedMotion: motion, bypassCSP: true, storageState: state });
      try {
        for (const path of pages) {
          const page = await context.newPage();
          page.setDefaultTimeout(OPEN_MS);
          try {
            const response = await page.goto(base + path, { waitUntil: "load" });
            if (!response || response.status() !== 200) {
              report.errors.push(`${person} ${path}: HTTP ${response ? response.status() : "none"}`);
              continue;
            }
            if ((await within(SETTLE_MS, page.evaluate(settle))) !== "settled") {
              report.hung.push({ person, page: path, motion, step: "open", animations: await page.evaluate(heldAnimations) });
              continue;
            }
            report.touched += await page.evaluate(touchClosedSections);
            if ((await within(SETTLE_MS, page.evaluate(settle))) !== "settled") {
              report.hung.push({ person, page: path, motion, step: "closed sections", animations: await page.evaluate(heldAnimations) });
            }
            report.checked += 1;
          } finally {
            await page.close();
          }
        }
      } finally {
        await context.close();
      }
    }
  }
}

async function idlePage(browser, base, state, path, report) {
  const context = await browser.newContext({ storageState: state });
  try {
    const page = await context.newPage();
    const sent = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.pathname.startsWith("/api/")) sent.push(`${request.method()} ${url.pathname}${url.search}`);
    });
    await page.goto(base + path, { waitUntil: "load", timeout: OPEN_MS });
    await page.waitForTimeout(IDLE_MS);
    report.idle = sent;
  } finally {
    await context.close();
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const persons = JSON.parse(readFileSync(args.pages, "utf-8"));
  const report = { checked: 0, touched: 0, hung: [], errors: [], idle: null };
  const playwright = pathToFileURL(resolve(REPO, ".pk/node_modules/playwright-core/index.mjs"));
  const { chromium } = await import(playwright.href);
  const { settle } = await import(pathToFileURL(resolve(REPO, ".pk/package/src/conform/collect.ts")).href);
  const browser = await chromium.launch();
  try {
    if (args.idle) {
      const anna = persons.anna;
      if (!anna) usage("--idle needs anna in --pages");
      await idlePage(browser, args.base_url, anna.state, args.idle, report);
    } else {
      await settlePages(browser, args.base_url, persons, settle, report);
    }
  } finally {
    await browser.close();
  }
  writeFileSync(args.report, JSON.stringify(report, null, 2));
}

main().catch((error) => {
  process.stderr.write(`settle-browser: ${error.stack || error}\n`);
  process.exit(1);
});
