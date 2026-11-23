// Browser check of the toasts (PLT-61-A10, PLT-62-A5; oberfl 2.1, UXB-P07), driven by
// tests/test_toast_browser.py against the manager in the stand-in world (tests/design_serve.py).
//
// On one page: the live regions `#pk-toasts > [role=status]` and `[role=alert]` exist and are
// empty before the first message; a success toast lands in the status region and leaves after
// 6 s, not while the pointer rests on it; an error toast lands in the alert region in three
// parts in fixed order with the correlation id, and stays until its labelled close button is
// pressed. Four refusals are produced: "input invalid" by the manager itself, "connection
// stopped", "right missing" and "session expired" by answers routed in the browser. axe runs
// on the open toasts. The page's clock is Playwright's, so no test waits 6 s.
//
// Usage: node tests/toast_browser.mjs --base-url URL --storage-state FILE --page PATH
// Prints a JSON report; exit 0 when every check holds, 1 otherwise, 2 on usage errors.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const HEADER = "X-Papaia-Correlation-Id";

function usage(message) {
  process.stderr.write(`toast-check: ${message}\n`);
  process.exit(2);
}

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 2) {
    const [flag, value] = [argv[i], argv[i + 1]];
    if (!["--base-url", "--storage-state", "--page"].includes(flag) || value === undefined) {
      usage(`bad argument ${flag}`);
    }
    args[flag.slice(2).replace("-", "_")] = value;
  }
  if (!args.base_url || !args.storage_state || !args.page) usage("all three arguments are required");
  if (!["127.0.0.1", "localhost"].includes(new URL(args.base_url).hostname)) usage("loopback only");
  return args;
}

// Runs in the page: the regions and what they hold.
function regions() {
  const box = document.getElementById("pk-toasts");
  const read = (role) => {
    const region = box && box.querySelector(`:scope > [role="${role}"]`);
    if (!region) return null;
    return [...region.children].map((toast) => ({
      tone: toast.dataset.pkTone,
      text: toast.querySelector(".pk-message-text").textContent.trim(),
      parts: [...toast.querySelectorAll("[data-pk-part]")]
        .filter((p) => p.dataset.pkPart !== "consequence")
        .map((p) => [p.dataset.pkPart, p.textContent.trim()]),
      close: (toast.querySelector("button[data-pk-dismiss]") || {}).ariaLabel || null,
    }));
  };
  return { status: read("status"), alert: read("alert") };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const playwright = pathToFileURL(resolve(REPO, ".pk/node_modules/playwright-core/index.mjs"));
  const { chromium } = await import(playwright.href);
  const axeSource = readFileSync(resolve(REPO, ".pk/node_modules/axe-core/axe.min.js"), "utf8");
  const browser = await chromium.launch();
  const report = { checks: [], errors: [], ids: {} };
  const check = (name, ok, detail) => report.checks.push({ name, ok: Boolean(ok), detail });
  try {
    const context = await browser.newContext({
      storageState: args.storage_state,
      locale: "en-GB",
      reducedMotion: "reduce",
    });
    const page = await context.newPage();
    page.on("pageerror", (error) => report.errors.push(String(error.message).split("\n")[0]));
    await page.clock.install();
    await page.goto(new URL(args.page, args.base_url).href, { waitUntil: "networkidle" });
    // From here on the page's time moves only when the check moves it.
    await page.clock.pauseAt((await page.evaluate(() => Date.now())) + 1000);

    const before = await page.evaluate(regions);
    check("regions exist and are empty before the first message",
      before.status !== null && before.alert !== null && before.status.length === 0 && before.alert.length === 0,
      before);

    // Success: status region, at least 6 s, longer while the pointer rests on it.
    // showToast is no global: the page's own instance comes from core/runtime.js.
    await page.evaluate(async () => {
      const { showToast } = await import("/static/js/core/runtime.js");
      showToast("success", "The catalog was saved.");
    });
    let now = await page.evaluate(regions);
    check("a success toast lands in the status region",
      now.status.length === 1 && now.status[0].tone === "success" && now.alert.length === 0, now);
    await page.clock.runFor(5900);
    check("it still stands after 5.9 s", (await page.evaluate(regions)).status.length === 1);
    const toast = page.locator('#pk-toasts [role="status"] .pk-toast').first();
    await toast.hover();
    await page.clock.runFor(10_000);
    check("it stays while the pointer rests on it", (await page.evaluate(regions)).status.length === 1);
    await page.mouse.move(5, 5);
    check("it leaves once the pointer has left", (await page.evaluate(regions)).status.length === 0);

    // input invalid: the manager itself refuses a malformed group request. The shared actions
    // are no globals; the page's own instances come from core/runtime.js, which main.js loaded
    // under the same URL.
    const runAction = (name, ...values) => page.evaluate(async ([n, v]) => {
      const { actions } = await import("/static/js/core/runtime.js");
      return actions[n](...v);
    }, [name, values]);
    await runAction("stackGroupAction", "start", "not-a-list");
    // connection stopped, right missing, session expired: answers routed in the browser.
    const routed = {
      "**/api/v1/catalogs/routed-stopped/refresh": [503, { detail: "upstream down" }, "0190f3a2-0000-7000-8000-000000000503"],
      "**/api/v1/catalogs/routed-forbidden/refresh": [403, { detail: "role papaia_admin required" }, "0190f3a2-0000-7000-8000-000000000403"],
      "**/api/v1/catalogs/routed-session/refresh": [401, { detail: "session expired" }, "0190f3a2-0000-7000-8000-000000000401"],
    };
    for (const [pattern, [status, body, id]] of Object.entries(routed)) {
      await page.route(pattern, (route) =>
        route.fulfill({ status, contentType: "application/json", headers: { [HEADER]: id }, body: JSON.stringify(body) }),
      );
    }
    for (const name of ["routed-stopped", "routed-forbidden", "routed-session"]) {
      await runAction("catalogAction", name, "refresh");
    }
    now = await page.evaluate(regions);
    const errors = now.alert;
    report.errors_shown = errors;
    check("four error toasts in the alert region, none in the status region",
      errors.length === 4 && errors.every((e) => e.tone === "error") && now.status.length === 0, now);
    check("each error has three parts in fixed order",
      errors.every((e) => e.parts.map(([p]) => p).join(",") === "what,todo,id"), errors.map((e) => e.parts));
    const expected = [
      ["input invalid", "The details are incomplete or do not fit.", null],
      ["connection stopped", "A service involved is not reachable right now.", "0190f3a2-0000-7000-8000-000000000503"],
      ["right missing", "The content does not exist or is not shared with you.", "0190f3a2-0000-7000-8000-000000000403"],
      ["session expired", "You are no longer signed in.", "0190f3a2-0000-7000-8000-000000000401"],
    ];
    expected.forEach(([label, what, id], i) => {
      const parts = Object.fromEntries(errors[i].parts);
      const reference = /^Reference: ([A-Za-z0-9-]{1,64})$/.exec(parts.id);
      // Part 1 is what happened, followed by the consequence sentence where there is one.
      check(`${label}: what happened`, parts.what === what || parts.what.startsWith(`${what} `), parts);
      check(`${label}: reference`, reference && (id === null || reference[1] === id), parts.id);
      if (reference) report.ids[label] = reference[1];
      check(`${label}: no status code alone, stack trace or key name`,
        !/(?<![\w.,])[45]\d\d(?![\w,]|[.,]\d)|traceback|\b[A-Za-z][A-Za-z0-9]*(?:[_.][A-Za-z0-9]+)+\b/i
          .test(`${parts.what} ${parts.todo}`), parts);
      check(`${label}: labelled close button`, errors[i].close === "Close message", errors[i].close);
    });
    const sessionLink = await page.locator('#pk-toasts [role="alert"] .pk-toast').nth(3)
      .locator('[data-pk-part="todo"] a').getAttribute("href");
    check("session expired: part 2 links to the page, which signs in again",
      sessionLink === new URL(args.page, args.base_url).href, sessionLink);

    // axe waits on timers of its own: the page's time flows again.
    await page.clock.resume();
    // The page enforces its policy, which refuses an inline script tag; the source is evaluated
    // through the browser's debugging protocol instead, which the policy does not govern.
    await page.evaluate(axeSource);
    const axe = await page.evaluate(async () => {
      const result = await window.axe.run("#pk-toasts", { resultTypes: ["violations"] });
      return result.violations.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.length }));
    });
    report.axe = axe;
    check("axe: no critical or serious finding on the open toasts",
      !axe.some((v) => v.impact === "critical" || v.impact === "serious"), axe);

    await page.clock.runFor(10 * 60 * 1000);
    check("errors stay until closed", (await page.evaluate(regions)).alert.length === 4);
    await page.locator('#pk-toasts [role="alert"] .pk-toast').first().locator("button[data-pk-dismiss]").click();
    await page.clock.runFor(1000);
    check("the close button removes the error", (await page.evaluate(regions)).alert.length === 3);
    check("no script error on the page", report.errors.length === 0, report.errors);
    await context.close();
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(report, null, 2) + "\n");
  return report.checks.every((c) => c.ok) ? 0 : 1;
}

main().then(
  (code) => process.exit(code),
  (error) => {
    process.stderr.write(`toast-check: ${error.stack || error}\n`);
    process.exit(2);
  },
);
