// Browser path of Settings -> Overlay and Mail -> Server on the CSP build of Alpine (T-UI-03),
// driven by tests/test_settings_mail_browser.py against the manager in the stand-in world
// (tests/design_serve.py --mail-relay).
//
// In each theme (light, dark): the overlay editor opens the selected file, a draft is typed and
// checked, the effective result is previewed, the overlay is applied through the dialog, the
// job is followed to its end and the page opens the target again; a configuration job that
// ends while the page is open reloads it. On the Mail page the connection mode drives the port
// and the login fields, the mail server is probed, a test mail is sent and the settings are
// saved and followed to the reload. The core is not there, so its answers (overlay files,
// preview, writes, jobs, probe, test mail) are routed in the browser; the pages, the job
// strips on load, the CSRF token and the scripts are the manager's own. Every page must stay
// free of policy reports (enforced header), refused Alpine expressions and script errors.
//
// Usage: node tests/settings_mail_browser.mjs --base-url URL --storage-state FILE [--shots DIR]
// Prints a JSON report; exit 0 when every check holds, 1 otherwise, 2 on usage errors.
// --shots DIR writes both pages at 1440 and 390 px in both themes into DIR.
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const OVERLAY_FILE = {
  content: "endpoints: {}\n",
  exists: true,
  lines: 1,
  revision: "0123456789abcdef0123",
  size: 14,
  versions: [{ id: "v-1", when: "2026-11-20T10:00:00Z", size: 9, revision: "aaaabbbb", event: "apply" }],
  preview_available: true,
};
const PREVIEW = {
  ok: true,
  changed: true,
  added: 1,
  removed: 0,
  diff: [
    { kind: "hunk", text: "@@ -1 +1,2 @@" },
    { kind: "ctx", text: "endpoints: {}" },
    { kind: "add", text: "interface: {}" },
  ],
};

function usage(message) {
  process.stderr.write(`settings-mail-check: ${message}\n`);
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

// Runs in the page before any of its scripts: the policy's reports and the job strips' events.
function watchPage() {
  window.__papaiaCsp = [];
  window.__papaiaJobs = [];
  document.addEventListener("securitypolicyviolation", (event) => {
    const report = { page: location.pathname, directive: event.effectiveDirective, blocked: event.blockedURI, sample: event.sample };
    window.__papaiaCsp.push(report);
    window.__papaiaReportCsp(report);
  });
  for (const name of ["papaia-config-job", "papaia-mail-job"]) {
    window.addEventListener(name, (event) => window.__papaiaJobs.push([name, event.detail.active]));
  }
}

function strip(id, event, active) {
  return `<div id="${id}" hx-get="/partials/${id === "mail-job-status" ? "mail" : "config"}/job-status" ` +
    `hx-trigger="${active ? "every 2s" : "every 10s"}" hx-swap="outerHTML" data-announce="${event}" ` +
    `data-active="${active}">${active ? '<p class="text-sm font-semibold">running</p>' : ""}</div>`;
}

const json = (route, body, status = 200) =>
  route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

async function openPage(context, url, watch) {
  const page = await context.newPage();
  page.on("console", (message) => {
    const text = message.text();
    if (text.startsWith("Alpine Expression Error") || text.startsWith("Alpine Warning")) watch.alpine.push(text.split("\n")[0]);
  });
  page.on("pageerror", (error) => watch.errors.push(String(error.message).split("\n")[0]));
  await page.goto(url, { waitUntil: "networkidle" });
  return page;
}

const toasts = (page) =>
  page.evaluate(() => [...document.querySelectorAll("#pk-toasts .pk-toast")].map((t) => [t.dataset.pkTone, t.textContent.trim()]));

async function overlayPath(context, base, theme, check) {
  const watch = { alpine: [], errors: [] };
  const sent = [];
  await context.route("**/api/v1/config/overlay/**", (route) => {
    const request = route.request();
    const url = new URL(request.url());
    sent.push({ method: request.method(), path: url.pathname + url.search, body: request.postDataJSON(), csrf: request.headers()["x-csrf-token"] || "" });
    if (request.method() === "GET") return json(route, OVERLAY_FILE);
    if (url.pathname.endsWith("/check")) return json(route, { problems: [], summary: "2 keys" });
    if (url.pathname.endsWith("/preview")) return json(route, PREVIEW);
    if (request.method() === "PUT") return json(route, { job_id: "job-overlay" }, 202);
    return json(route, { detail: "unexpected" }, 400);
  });
  await context.route("**/api/v1/jobs/job-overlay", (route) => json(route, { id: "job-overlay", status: "succeeded" }));
  const page = await openPage(context, new URL("/settings/overlay", base).href, watch);
  const label = `overlay (${theme})`;

  await page.waitForFunction(() => document.querySelector("textarea[aria-label]")?.value === "endpoints: {}\n");
  check(`${label}: the selected file is opened and checked`,
    sent.length >= 2 && sent[0].method === "GET" && sent[0].path === "/api/v1/config/overlay/librechat" &&
    sent[1].path === "/api/v1/config/overlay/librechat/check", sent.slice(0, 2));
  check(`${label}: the strip on load announces no job`,
    (await page.evaluate(() => window.__papaiaJobs)).some(([n, a]) => n === "papaia-config-job" && a === false),
    await page.evaluate(() => window.__papaiaJobs));
  check(`${label}: the gutter numbers the lines`,
    (await page.locator("pre[x-ref=gutter]").textContent()) === "1\n2");

  await page.locator("textarea[aria-label]").fill("endpoints: {}\ninterface: {}\n");
  await page.waitForFunction(() => document.querySelector("pre[x-ref=gutter]").textContent === "1\n2\n3");
  await page.waitForTimeout(700);
  const lastCheck = sent.filter((s) => s.path.endsWith("/check")).at(-1);
  check(`${label}: an edit is checked after the pause`,
    lastCheck && lastCheck.body.content === "endpoints: {}\ninterface: {}\n" && lastCheck.csrf.length > 0, lastCheck);
  check(`${label}: the check's summary is shown`,
    await page.getByText("YAML valid · 2 keys").isVisible());
  if (args.shots) await shoot(page, "overlay", theme);

  await page.getByRole("button", { name: "Preview effective result" }).click();
  await page.waitForFunction(() => [...document.querySelectorAll("div.whitespace-pre")].some((d) => d.textContent.includes("interface: {}")));
  const sentence = page.locator("p span[x-show]").filter({ visible: true });
  check(`${label}: the effective result names the path of the selected target`,
    (await sentence.count()) === 1 &&
    (await sentence.locator("span.font-mono").textContent()) === "ai/librechat/librechat.yaml",
    await sentence.allTextContents());
  check(`${label}: the diff shows the added line`,
    await page.getByText("+ interface: {}").isVisible());

  await page.getByRole("button", { name: "Apply overlay" }).filter({ visible: true }).first().click();
  const dialog = page.locator("dialog[open]");
  check(`${label}: the dialog names the target`,
    (await dialog.locator("h3").textContent()) === "Apply overlay to LibreChat?");
  await context.route("**/partials/config/job-status", (route) =>
    route.fulfill({ status: 200, contentType: "text/html", body: strip("config-job-status", "papaia-config-job", true) }));
  const navigated = page.waitForURL("**/settings/overlay?target=librechat", { timeout: 10_000 });
  await dialog.getByRole("button", { name: "Apply overlay" }).click();
  const put = await (async () => {
    for (let i = 0; i < 50 && !sent.some((s) => s.method === "PUT"); i++) await page.waitForTimeout(50);
    return sent.find((s) => s.method === "PUT");
  })();
  check(`${label}: apply writes the draft against the revision opened`,
    put && put.path === "/api/v1/config/overlay/librechat" && put.body.content === "endpoints: {}\ninterface: {}\n" &&
    put.body.revision === OVERLAY_FILE.revision && put.csrf.length > 0, put);
  await page.waitForFunction(() => window.__papaiaJobs.some(([n, a]) => n === "papaia-config-job" && a === true));
  check(`${label}: the refreshed strip announces the running job`, true);
  const shown = await toasts(page);
  check(`${label}: the job is announced and its end told`,
    shown.some(([t, x]) => t === "info" && x.includes("Applying the overlay …")) &&
    shown.some(([t, x]) => t === "success" && x.includes("Done")), shown);
  await navigated;
  check(`${label}: the page opens the target again`, page.url().endsWith("/settings/overlay?target=librechat"));
  await context.unroute("**/partials/config/job-status");
  await page.close();

  // A configuration job that ends while the page is open (another tab): one reload.
  let strips = 0;
  await context.route("**/partials/config/job-status", (route) => {
    strips += 1;
    return route.fulfill({ status: 200, contentType: "text/html", body: strip("config-job-status", "papaia-config-job", strips === 1) });
  });
  const other = await openPage(context, new URL("/settings/overlay", base).href, watch);
  let loads = 0;
  other.on("load", () => (loads += 1));
  await other.waitForFunction(() => window.__papaiaJobs.some(([, a]) => a === true));
  await other.waitForEvent("load", { timeout: 8000 }).catch(() => null);
  check(`${label}: a job that ends elsewhere reloads the page`, loads >= 1, { loads, strips });
  await context.unroute("**/partials/config/job-status");
  await other.close();

  await context.unroute("**/api/v1/config/overlay/**");
  await context.unroute("**/api/v1/jobs/job-overlay");
  return watch;
}

async function mailPath(context, base, theme, check) {
  const watch = { alpine: [], errors: [] };
  const sent = [];
  let testAnswer = { ok: true };
  await context.route(/\/api\/v1\/mail(\/.*)?$/, (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "GET") return route.continue();
    sent.push({ method: request.method(), path: url.pathname, body: request.postDataJSON(), csrf: request.headers()["x-csrf-token"] || "" });
    if (url.pathname.endsWith("/probe")) return json(route, { ok: true, login: "ok" });
    if (url.pathname.endsWith("/test-message")) return json(route, testAnswer);
    if (request.method() === "PUT") return json(route, { job_id: "job-mail" }, 202);
    return json(route, { detail: "unexpected" }, 400);
  });
  await context.route("**/api/v1/jobs/job-mail", (route) => json(route, { id: "job-mail", status: "succeeded" }));
  const page = await openPage(context, new URL("/mail/server", base).href, watch);
  const label = `mail (${theme})`;
  const state = () => page.evaluate(() => {
    const v = (id) => document.getElementById(id);
    return {
      port: v("mail-port").value, login: v("mail-login").checked, loginOff: v("mail-login").disabled,
      user: v("mail-user").disabled, password: v("mail-password").disabled,
    };
  });
  const revision = await page.evaluate(() => JSON.parse(document.getElementById("mail-init").textContent).mail.revision);

  check(`${label}: the strip on load announces no job`,
    (await page.evaluate(() => window.__papaiaJobs)).some(([n, a]) => n === "papaia-mail-job" && a === false),
    await page.evaluate(() => window.__papaiaJobs));
  let now = await state();
  check(`${label}: a stored login opens the login fields`, now.login && !now.user && !now.password, now);
  await page.selectOption("#mail-security", "none");
  now = await state();
  check(`${label}: without encryption there is no login and the port follows`,
    now.port === "25" && !now.login && now.loginOff && now.user && now.password, now);
  await page.selectOption("#mail-security", "tls");
  await page.check("#mail-login");
  now = await state();
  check(`${label}: TLS takes its port and the login can be given again`,
    now.port === "465" && now.login && !now.loginOff && !now.user && !now.password, now);

  await page.getByRole("button", { name: "Test mail server" }).click();
  await page.waitForFunction(() => document.getElementById("mail-probe-result").textContent.includes("answered"));
  check(`${label}: the probe sends the form and shows the answer`,
    (await page.locator("#mail-probe-result").textContent()) === "The mail server answered (login accepted). No mail was sent." &&
    sent[0].path === "/api/v1/mail/probe" && sent[0].body.port === 465 && sent[0].body.security === "tls" && sent[0].csrf.length > 0,
    sent[0]);

  await page.fill("#mail-test-to", "you@example.org");
  await page.click("#mail-test-send");
  await page.waitForFunction(() => document.getElementById("mail-test-result").textContent.length > 0);
  check(`${label}: a test mail is sent to the address typed`,
    sent.at(-1).path === "/api/v1/mail/test-message" && sent.at(-1).body.to === "you@example.org" &&
    (await page.locator("#mail-test-result").textContent()).startsWith("The relay accepted the message."), sent.at(-1));
  testAnswer = { ok: false, reason: "<b>refused</b> by the relay" };
  await page.click("#mail-test-send");
  await page.waitForFunction(() => document.getElementById("mail-test-result").textContent.includes("refused"));
  check(`${label}: a refusal is shown as text`,
    (await page.locator("#mail-test-result").textContent()) === "<b>refused</b> by the relay" &&
    (await page.locator("#mail-test-result b").count()) === 0);
  if (args.shots) await shoot(page, "mail", theme);

  await context.route("**/partials/mail/job-status", (route) =>
    route.fulfill({ status: 200, contentType: "text/html", body: strip("mail-job-status", "papaia-mail-job", true) }));
  const reloaded = page.waitForEvent("load", { timeout: 10_000 });
  await page.getByRole("button", { name: "Save and apply" }).click();
  await page.waitForFunction(() => window.__papaiaJobs.some(([n, a]) => n === "papaia-mail-job" && a === true));
  const put = sent.find((s) => s.method === "PUT");
  check(`${label}: save sends the form against the page's revision`,
    put && put.path === "/api/v1/mail" && put.body.revision === revision && put.body.port === 465 &&
    put.body.security === "tls" && put.body.host === "smtp.example.org" && !("password" in put.body) && put.csrf.length > 0, put);
  const shown = await toasts(page);
  check(`${label}: the job is announced and its end told`,
    shown.some(([t, x]) => t === "info" && x.includes("Applying the mail settings …")) &&
    shown.some(([t, x]) => t === "success" && x.includes("Mail settings applied")), shown);
  await reloaded;
  check(`${label}: the page loads again after the job`, true);
  await context.unroute("**/partials/mail/job-status");
  await page.close();
  await context.unroute(/\/api\/v1\/mail(\/.*)?$/);
  await context.unroute("**/api/v1/jobs/job-mail");
  return watch;
}

let args;
let browser;

async function shoot(page, name, theme) {
  mkdirSync(args.shots, { recursive: true });
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(150);
    await page.screenshot({ path: resolve(args.shots, `${name}-${width}-${theme}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 900 });
}

async function main() {
  args = parseArgs(process.argv.slice(2));
  const playwright = pathToFileURL(resolve(REPO, ".pk/node_modules/playwright-core/index.mjs"));
  const { chromium } = await import(playwright.href);
  browser = await chromium.launch();
  const report = { checks: [], pages: {} };
  const check = (name, ok, detail) => report.checks.push({ name, ok: Boolean(ok), detail: ok ? undefined : detail });
  try {
    for (const theme of ["light", "dark"]) {
      const context = await browser.newContext({
        storageState: args.storage_state,
        locale: "en-GB",
        colorScheme: theme,
        reducedMotion: "reduce",
        viewport: { width: 1440, height: 900 },
      });
      // Every report of every page of this context, as it happens.
      const reports = [];
      await context.exposeFunction("__papaiaReportCsp", (report) => reports.push(report));
      await context.addInitScript(watchPage);
      const overlay = await overlayPath(context, args.base_url, theme, check);
      const mail = await mailPath(context, args.base_url, theme, check);
      for (const [name, watch] of [["overlay", overlay], ["mail", mail]]) {
        report.pages[`${name}-${theme}`] = watch;
        check(`${name} (${theme}): no refused Alpine expression`, watch.alpine.length === 0, watch.alpine);
        check(`${name} (${theme}): no script error`, watch.errors.length === 0, watch.errors);
      }
      check(`${theme}: no report of the content security policy`, reports.length === 0, reports);
      await context.close();
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
    process.stderr.write(`settings-mail-check: ${error.stack || error}\n`);
    process.exit(2);
  },
);
