// Browser paths of the upgrade pages (T-UI-03, oberfl 3.3), driven by
// tests/test_upgrade_scripts.py against the manager in the stand-in world
// (tests/design_serve.py), once per theme.
//
// Release upgrade (/upgrade): opening the page checks for upgrades, the header button names
// the release on offer, the check button checks again; the dialog builds its sentences and
// starts the upgrade with the chosen options; the runner card follows the run to its end, the
// button says why it waits, "Reload the page" reloads, Dismiss clears the result; the release
// picker re-runs the check; outdated images are selected, listed in the dialog and removed.
// Add-on upgrade (/addons/NAME/upgrade): the plan is drawn, the button waits for the value the
// new version needs, the upgrade is applied with it and its job followed to the end.
//
// The stand-in world has no git checkout, no Docker and no catalog, so the partials it cannot
// fill come from --fixtures (rendered from the real templates by the test) and the API calls
// are answered by routes in the browser; the add-on page is the server's own, with the island
// saying that this core can upgrade add-ons. Every page records the reports of the
// enforced policy, refused Alpine expressions and uncaught script errors. --shots DIR also
// writes screenshots at 1440 and 390 px per theme.
//
// Usage: node tests/upgrade_browser.mjs --base-url URL --storage-state FILE --fixtures DIR
//          --addon NAME [--shots DIR]
// Prints a JSON report; exit 0 when every check holds, 1 otherwise, 2 on usage errors.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const FLAGS = ["--base-url", "--storage-state", "--fixtures", "--addon", "--shots"];
const IMAGE_A = "sha256:" + "a".repeat(64);
const IMAGE_B = "sha256:" + "b".repeat(64);

function usage(message) {
  process.stderr.write(`upgrade-check: ${message}\n`);
  process.exit(2);
}

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 2) {
    const [flag, value] = [argv[i], argv[i + 1]];
    if (!FLAGS.includes(flag) || value === undefined) usage(`bad argument ${flag}`);
    args[flag.slice(2).replace("-", "_")] = value;
  }
  for (const flag of ["base_url", "storage_state", "fixtures", "addon"]) {
    if (!args[flag]) usage(`--${flag.replace("_", "-")} is required`);
  }
  if (!["127.0.0.1", "localhost"].includes(new URL(args.base_url).hostname)) usage("loopback only");
  if (!/^[a-z0-9][a-z0-9-]{0,62}$/.test(args.addon)) usage("bad add-on name");
  return args;
}

// Runs in the page before its scripts: collects the reports of the enforced policy.
function collectReports() {
  window.__papaiaCsp = [];
  document.addEventListener("securitypolicyviolation", (event) => {
    window.__papaiaCsp.push(`${event.violatedDirective} ${event.blockedURI || "inline"}`);
  });
}

function toasts(role) {
  const region = document.querySelector(`#pk-toasts > [role="${role}"]`);
  return region ? [...region.children].map((t) => t.textContent.replace(/\s+/g, " ").trim()) : [];
}

const PLAN = {
  revision: "rev-1",
  form: [{ key: "NEW_MAIL", label: "Sender address", hint: "Where mail comes from", is_secret: false, default: "", placeholder: "" }],
  changelog: [{ version: "1.1.0", date: "2026-11-20", body: "- Sends mail." }],
  plan: {
    current: { version: "1.0.0" },
    candidate: { version: "1.1.0" },
    direction: "upgrade",
    blocked: false,
    in_place: false,
    needs_input: ["NEW_MAIL"],
    compat: { warnings: [], reason: "" },
    components: { added: [], removed: [], changed: [{ id: "web", name: "web", from: "1.0", to: "1.1" }] },
    env: { new: [{ key: "NEW_MAIL", label: "Sender address", class: "change_me" }], removed: [], default_changed: [] },
    manifest: {
      addon_api: { from: [1], to: [1, 2] },
      attach: { added: ["mail"], removed: [] },
      keycloak: { clients_added: [], mappers_added: { "sample-client": ["groups", "roles"] } },
      replace_secrets_added: [],
    },
  },
};

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const playwright = pathToFileURL(resolve(REPO, ".pk/node_modules/playwright-core/index.mjs"));
  const { chromium } = await import(playwright.href);
  const fixture = (name) => readFileSync(resolve(args.fixtures, name), "utf-8");
  const browser = await chromium.launch();
  const report = { checks: [], errors: [], violations: [], alpine: [] };
  const check = (name, ok, detail) => report.checks.push({ name, ok: Boolean(ok), detail });
  const url = (path) => new URL(path, args.base_url).href;

  async function open(context, path, label) {
    const page = await context.newPage();
    page.on("pageerror", (error) => report.errors.push(`${label}: ${String(error.message).split("\n")[0]}`));
    page.on("console", (message) => {
      const text = message.text();
      if (text.startsWith("Alpine Expression Error") || text.startsWith("Alpine Warning")) {
        report.alpine.push(`${label}: ${text.split("\n").slice(0, 3).join(" | ")}`);
      }
    });
    await page.addInitScript(collectReports);
    await page.goto(url(path), { waitUntil: "networkidle" });
    return page;
  }

  async function reports(page, label) {
    const found = await page.evaluate(() => window.__papaiaCsp || []);
    report.violations.push(...found.map((v) => `${label}: ${v}`));
  }

  async function shoot(page, name, theme) {
    if (!args.shots) return;
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await page.waitForTimeout(200);
      await page.screenshot({ path: resolve(args.shots, `${name}-${width}-${theme}.png`), fullPage: true });
    }
    await page.setViewportSize({ width: 1440, height: 900 });
  }

  async function until(page, fn, arg, timeout = 10_000) {
    try {
      await page.waitForFunction(fn, arg, { timeout });
      return true;
    } catch {
      return false;
    }
  }

  async function upgradePath(context, theme) {
    const label = `/upgrade ${theme}`;
    const state = { runner: "runner-none.html" };
    const sent = [];
    const html = (name) => ({ status: 200, contentType: "text/html; charset=utf-8", body: fixture(name) });
    const json = (status, body) => ({ status, contentType: "application/json", body: JSON.stringify(body) });
    const answers = {
      "/partials/upgrade/status": () => html("status.html"),
      "/partials/upgrade/check": () => html("check.html"),
      "/partials/upgrade/runner": () => html(state.runner),
      "/partials/upgrade/images": () => html("images.html"),
      "/api/v1/upgrade/check": () => json(200, {}),
      "/api/v1/upgrade": () => {
        state.runner = "runner-running.html";
        return json(202, { started: true });
      },
      "/api/v1/upgrade/runner/clear": () => {
        state.runner = "runner-none.html";
        return json(200, { images: { removed: [{ name: "postgres:14" }], reclaimed_human: "1.0 GB" } });
      },
      "/api/v1/upgrade/images/prune": () =>
        json(200, { removed: [{ name: "postgres:15" }], failed: [], reclaimed_human: "650.0 MB" }),
    };
    await context.route(
      (u) => Object.prototype.hasOwnProperty.call(answers, u.pathname),
      async (route) => {
        const request = route.request();
        const path = new URL(request.url()).pathname;
        if (request.method() === "POST") {
          sent.push({ path, body: request.postData() ? JSON.parse(request.postData()) : null, csrf: request.headers()["x-csrf-token"] || "" });
        }
        await route.fulfill(answers[path]());
      },
    );
    const posts = (path) => sent.filter((s) => s.path === path);
    const install = '[x-data="upgradeActions"] .btn-primary';

    const page = await open(context, "/upgrade", label);
    check(`${theme}: opening the page checks for the newest release`,
      posts("/api/v1/upgrade/check").some((s) => s.body.version === null && s.csrf !== ""), sent);
    check(`${theme}: the button names the release on offer`,
      await until(page, (sel) => {
        const b = document.querySelector(sel);
        return b && !b.disabled && b.textContent.trim() === "Install 1.2.0";
      }, install));
    check(`${theme}: hidden parts stay hidden`, await page.evaluate(() =>
      [...document.querySelectorAll("[x-cloak], [x-show]")].length > 0
        && getComputedStyle(document.querySelector('[x-data="upgradeActions"] .loading')).display === "none"));
    await shoot(page, "upgrade", theme);

    const checks = posts("/api/v1/upgrade/check").length;
    await page.click('[aria-label="Check for upgrades"]');
    check(`${theme}: the check button checks again`,
      await until(page, (sel) => !document.querySelector(sel).disabled, '[aria-label="Check for upgrades"]')
        && posts("/api/v1/upgrade/check").length === checks + 1);

    await page.click(install);
    const dialog = await page.evaluate(() => {
      const modal = document.querySelector('dialog[x-ref="upgradeModal"]');
      const text = (key) => document.querySelector(`[data-sentence="${key}"]`).textContent.trim();
      const mono = (key) => [...document.querySelectorAll(`[data-sentence="${key}"] .font-mono`)].map((s) => s.textContent);
      return {
        open: modal.open,
        title: text("upgradeTitle"),
        intro: mono("upgradeIntro"),
        checkout: mono("upgradeCheckout"),
        logs: mono("upgradeLogs"),
        start: modal.querySelector(".modal-action .btn-primary").disabled,
      };
    });
    check(`${theme}: the dialog builds its sentences`,
      dialog.open && dialog.title === "Upgrade to 1.2.0?" && dialog.intro.join() === "1.1.0,1.2.0"
        && dialog.checkout.join() === "v1.2.0" && dialog.logs.join() === "docker logs papaia-upgrade-1.2.0"
        && dialog.start, dialog);
    await page.check('input[x-model="confirmed"]');
    await page.click('dialog[x-ref="upgradeModal"] .modal-action .btn-primary');
    check(`${theme}: start sends the chosen options`,
      await until(page, () => document.querySelector("#upgrade-runner .badge-warning") !== null)
        && JSON.stringify(posts("/api/v1/upgrade").map((s) => s.body))
          === JSON.stringify([{ version: "1.2.0", force: false, no_backup: false, prune_images: true }]),
      posts("/api/v1/upgrade"));
    check(`${theme}: the start is announced`,
      (await page.evaluate(toasts, "status")).some((t) => t.includes("Upgrade started")));
    check(`${theme}: the button waits for the running upgrade`,
      await until(page, (sel) => {
        const b = document.querySelector(sel);
        return b.disabled && b.title === "An upgrade is already in flight";
      }, install));

    state.runner = "runner-done.html";
    check(`${theme}: the runner follows the run to its end`,
      await until(page, () => (document.querySelector("#upgrade-runner .badge-success") || {}).textContent === "upgrade complete"));
    check(`${theme}: the button names the result to dismiss`,
      await until(page, (sel) => document.querySelector(sel).textContent.trim() === "Dismiss the previous upgrade first", install));
    await Promise.all([page.waitForEvent("load"), page.click('[data-upgrade-action="reload"]')]);
    await page.waitForLoadState("networkidle");
    check(`${theme}: "Reload the page" reloads it`,
      await until(page, () => document.querySelector('[data-upgrade-action="dismiss"]') !== null));
    await page.click('[data-upgrade-action="dismiss"]');
    check(`${theme}: Dismiss clears the result and tells about the image pass`,
      await until(page, () => !document.querySelector("#upgrade-runner .card"))
        && posts("/api/v1/upgrade/runner/clear").length === 1
        && (await page.evaluate(toasts, "status")).some((t) => t.includes("Dismissed; removed 1 more image, 1.0 GB reclaimed")));
    check(`${theme}: the button offers the release again`,
      await until(page, (sel) => document.querySelector(sel).textContent.trim() === "Install 1.2.0", install));

    await page.selectOption("#upgrade-version", "1.3.0");
    check(`${theme}: the picker re-runs the check for the chosen release`,
      await until(page, () => true) && (await page.waitForTimeout(300), posts("/api/v1/upgrade/check").some((s) => s.body.version === "1.3.0")));

    await page.check(".prune-image-check >> nth=0");
    const selection = await page.evaluate(() => ({
      label: document.getElementById("prune-selected-button").textContent.trim(),
      disabled: document.getElementById("prune-selected-button").disabled,
      indeterminate: document.getElementById("prune-master-check").indeterminate,
    }));
    check(`${theme}: a selected image arms the bulk button`,
      selection.label === "Remove selected (1)" && !selection.disabled && selection.indeterminate, selection);
    await page.click('[data-upgrade-action="prune-selected"]');
    const chosen = await page.evaluate(() => ({
      open: document.getElementById("prune-images-modal").open,
      title: document.getElementById("prune-images-title").textContent,
      list: [...document.querySelectorAll("#prune-images-list li")].map((li) => li.textContent),
    }));
    check(`${theme}: the dialog lists the selected image`,
      chosen.open && chosen.title === "Remove 1 image?" && chosen.list.join() === "postgres:15", chosen);
    await page.click('[data-upgrade-action="prune-cancel"]');
    check(`${theme}: Cancel closes the dialog`,
      !(await page.evaluate(() => document.getElementById("prune-images-modal").open)));
    await page.check("#prune-master-check");
    check(`${theme}: the master box selects every image`,
      await page.evaluate(() => [...document.querySelectorAll(".prune-image-check")].every((b) => b.checked)));
    await page.click('[data-upgrade-action="prune-all"]');
    check(`${theme}: Remove all lists every image`,
      (await page.textContent("#prune-images-title")) === "Remove 2 images?");
    await page.click('[data-upgrade-action="prune-confirm"]');
    check(`${theme}: the removal names exactly the listed images`,
      await until(page, () => true) && (await page.waitForTimeout(300),
        JSON.stringify(posts("/api/v1/upgrade/images/prune").map((s) => s.body)) === JSON.stringify([{ images: [IMAGE_A, IMAGE_B] }])),
      posts("/api/v1/upgrade/images/prune"));
    check(`${theme}: the removal is reported`,
      await until(page, () => [...document.querySelectorAll('#pk-toasts > [role="status"] > *')]
        .some((t) => t.textContent.includes("Removed 1 image, 650.0 MB reclaimed"))));
    check(`${theme}: every write carried the CSRF token`, sent.every((s) => s.csrf !== ""));
    await reports(page, label);
    await page.close();
  }

  async function addonPath(context, theme) {
    const label = `/addons/${args.addon}/upgrade ${theme}`;
    const base = `/api/v1/addons/${args.addon}`;
    const sent = [];
    await context.route((u) => u.pathname === `/addons/${args.addon}/upgrade`, async (route) => {
      const response = await route.fetch();
      const body = (await response.text()).replace('"supported": false', '"supported": true');
      await route.fulfill({ response, body });
    });
    const answers = {
      [`${base}/upgrade-plan`]: () => PLAN,
      [`${base}/upgrade`]: () => ({ job_id: "j-1" }),
      "/api/v1/jobs/j-1": () => ({ status: "succeeded" }),
      "/api/v1/jobs/j-1/log": () => ({ log: "upgraded to 1.1.0" }),
    };
    await context.route(
      (u) => Object.prototype.hasOwnProperty.call(answers, u.pathname),
      async (route) => {
        const request = route.request();
        const path = new URL(request.url()).pathname;
        if (request.method() === "POST") {
          sent.push({ path, body: JSON.parse(request.postData() || "null"), csrf: request.headers()["x-csrf-token"] || "" });
        }
        await route.fulfill({
          status: path.endsWith("/upgrade") ? 202 : 200,
          contentType: "application/json",
          body: JSON.stringify(answers[path]()),
        });
      },
    );

    const page = await open(context, `/addons/${args.addon}/upgrade`, label);
    const apply = ".card-body .btn-primary";
    check(`${theme}: the plan is drawn`,
      await until(page, () => document.body.textContent.includes("1.0.0") && document.querySelector(".badge-accent") !== null));
    const drawn = await page.evaluate((sel) => ({
      label: document.querySelector(sel).textContent.trim(),
      disabled: document.querySelector(sel).disabled,
      missing: getComputedStyle([...document.querySelectorAll("p.text-error")].pop()).display !== "none",
      changelog: document.body.textContent.includes("- Sends mail."),
      mappers: document.body.textContent.includes("sample-client: groups, roles"),
      api: document.body.textContent.includes("1 → 1, 2"),
      failed: getComputedStyle([...document.querySelectorAll(".text-error")].find((e) => e.textContent.includes("did not finish"))).display,
    }), apply);
    check(`${theme}: the button waits for the value the new version needs`,
      drawn.label === "Upgrade to 1.1.0" && drawn.disabled && drawn.missing, drawn);
    check(`${theme}: changelog, mappers and API change are shown, the job card is hidden`,
      drawn.changelog && drawn.mappers && drawn.api && drawn.failed === "none", drawn);
    await shoot(page, "addon-upgrade", theme);
    await page.fill('input[aria-label="Sender address"]', " ops@example.org ");
    check(`${theme}: a value arms the button`, !(await page.isDisabled(apply)));
    await page.uncheck('input[x-model="backup"]');
    await page.click(apply);
    check(`${theme}: the upgrade is applied with the entered value`,
      await until(page, () => {
        const done = document.querySelector(".text-success[x-show]");
        return done && getComputedStyle(done).display !== "none" && document.querySelector('pre[x-text="log"]').textContent !== "";
      })
        && JSON.stringify(sent.filter((s) => s.path === `${base}/upgrade`).map((s) => s.body))
          === JSON.stringify([{ env: { NEW_MAIL: "ops@example.org" }, revision: "rev-1", backup: false }]),
      sent);
    check(`${theme}: the job log is followed`,
      (await page.textContent('pre[x-text="log"]')) === "upgraded to 1.1.0",
      await page.textContent('pre[x-text="log"]'));
    check(`${theme}: every request carried the CSRF token`, sent.length >= 2 && sent.every((s) => s.csrf !== ""));
    await reports(page, label);
    await page.close();
  }

  try {
    for (const theme of ["light", "dark"]) {
      const context = await browser.newContext({
        storageState: args.storage_state,
        locale: "en-GB",
        reducedMotion: "reduce",
        colorScheme: theme,
        viewport: { width: 1440, height: 900 },
      });
      await upgradePath(context, theme);
      await addonPath(context, theme);
      await context.close();
    }
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(report, null, 2) + "\n");
  const ok = report.checks.every((c) => c.ok) && !report.errors.length && !report.violations.length && !report.alpine.length;
  process.exit(ok ? 0 : 1);
}

main().catch((error) => {
  process.stderr.write(`upgrade-check: ${error.stack || error}\n`);
  process.exit(2);
});
