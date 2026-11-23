// Browser check of the content security policy (T-UI-03), run by scripts/csp-check.sh.
//
// Opens every page in Chromium (playwright-core of .pk/, the version of the kit's
// contract/specimen.yaml), in the light and the dark colour scheme, and records per page what
// the enforced policy refuses (`securitypolicyviolation` events), the expressions Alpine
// refuses ("Alpine Expression Error") and uncaught script errors. Any of them fails the check,
// and so does a page that answers without an enforced `Content-Security-Policy` header.
// `converted` in the report says whether the page carries the marker of base.html
// (<meta name="papaia-csp" content="strict">).
//
// --candidate LABEL=FILE measures an Alpine build: every Alpine expression of every page
// (attributes of the rendered page and of its <template> elements) is evaluated by that build
// in a separate blank page, in a scope where every plain name exists but the browser's global
// objects (window, document, Math, JSON, ...) do not, as on every page. An expression
// the build cannot evaluate there -- a refusal, a parse error, a name it reads as a path --
// is one rewrite that page needs on that build. Used for the choice of the CSP build and, in
// CI, to show what is left on the shipped one.
//
// Usage (from the repository root):
//   node tests/csp_browser.mjs --base-url URL --page PATH [--page PATH ...]
//     [--storage-state FILE] [--candidate LABEL=FILE ...] [--report FILE] [--summary FILE]
// Exit: 0 clean, 1 findings on a page, 2 usage or a page that does not load.
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SETTLE_MS = 400;
const SCHEMES = ["light", "dark"];
// Global objects a page's expressions cannot reach: the component has to offer
// what it needs from them.
const GLOBALS = [
  "window", "document", "globalThis", "self", "Math", "JSON", "console", "localStorage",
  "sessionStorage", "navigator", "location", "history", "Intl", "Number", "String", "Object",
  "Array", "Date", "Promise", "Reflect", "setTimeout", "clearTimeout", "setInterval",
  "clearInterval", "fetch", "alert", "confirm", "prompt", "encodeURIComponent",
  "decodeURIComponent", "parseInt", "parseFloat", "isNaN", "htmx", "Alpine",
];

function usage(message) {
  process.stderr.write(`csp-check: ${message}\n`);
  process.exit(2);
}

function parseArgs(argv) {
  const args = { pages: [], candidates: [] };
  const single = new Set(["--base-url", "--storage-state", "--report", "--summary"]);
  for (let i = 0; i < argv.length; i += 2) {
    const [flag, value] = [argv[i], argv[i + 1]];
    if (value === undefined) usage(`${flag} needs a value`);
    if (flag === "--page") {
      if (!value.startsWith("/") || value.startsWith("//")) usage(`page must be a path: ${value}`);
      args.pages.push(value);
    } else if (flag === "--candidate") {
      const at = value.indexOf("=");
      if (at < 1) usage(`candidate must be LABEL=FILE: ${value}`);
      args.candidates.push({ label: value.slice(0, at), file: value.slice(at + 1) });
    } else if (single.has(flag)) {
      args[flag.slice(2).replace("-", "_")] = value;
    } else {
      usage(`unknown argument ${flag}`);
    }
  }
  if (!args.base_url) usage("--base-url is required");
  const base = new URL(args.base_url);
  if (!["127.0.0.1", "localhost"].includes(base.hostname)) usage("the base URL must be the loopback");
  if (args.pages.length === 0) usage("at least one --page is required");
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
      line: event.lineNumber,
    });
  });
}

// Runs in the page: every Alpine attribute with an expression, distinct, as [name, expression].
function collectExpressions() {
  const seen = new Map();
  const skip = /^x-(ref|cloak|ignore|transition|teleport|id)([.:]|$)/;
  const visit = (root) => {
    for (const element of root.querySelectorAll("*")) {
      for (const { name, value } of element.attributes) {
        if (!/^(x-|@|:)/.test(name) || skip.test(name) || !value.trim()) continue;
        let expression = value;
        if (/^x-for([.:]|$)/.test(name)) {
          const match = /\s(?:in|of)\s([\s\S]+)$/.exec(value);
          expression = match ? match[1] : value;
        }
        seen.set(`${name}\u0000${expression}`, [name, expression]);
      }
      if (element.tagName === "TEMPLATE") visit(element.content);
    }
  };
  visit(document);
  return [...seen.values()];
}

// Runs in the blank probe page that carries only the candidate build.
function probeExpressions([expressions, globals]) {
  const absent = new Set(globals);
  const ident = /^[A-Za-z_$][\w$]*$/;
  const known = (key) => typeof key === "string" && ident.test(key) && !absent.has(key);
  const value = new Proxy(function () {}, {
    get(_, key) {
      if (key === Symbol.toPrimitive) return () => 0;
      if (key === Symbol.iterator) return function* () {};
      if (key === "then") return undefined;
      return typeof key === "string" && ident.test(key) ? value : undefined;
    },
    apply: () => value,
    construct: () => value,
    set: () => true,
    has: () => true,
  });
  const scope = new Proxy({}, {
    has: (_, key) => known(key),
    get: (_, key) => (known(key) ? value : undefined),
    set: () => true,
  });
  const element = document.createElement("div");
  document.body.append(element);
  const capture = (run) => {
    let problem = null;
    const [warn, error] = [console.warn, console.error];
    console.warn = console.error = (message) => {
      problem ??= String(message).split("\n")[0];
    };
    try {
      run();
    } catch (thrown) {
      problem ??= String(thrown && thrown.message ? thrown.message : thrown).split("\n")[0];
    } finally {
      console.warn = warn;
      console.error = error;
    }
    return problem;
  };
  // x-html is a directive, not an expression form: try it once on a tree of its own.
  const htmlProblem = capture(() => {
    const tree = document.createElement("div");
    tree.setAttribute("x-data", "{ markup: 'a' }");
    tree.innerHTML = '<p x-html="markup"></p>';
    document.body.append(tree);
    window.Alpine.initTree(tree);
  });
  return expressions.map(([name, expression]) =>
    /^x-html([.:]|$)/.test(name) && htmlProblem
      ? htmlProblem
      : capture(() => window.Alpine.evaluate(element, expression, { scope })),
  );
}

async function measure(browser, candidate, expressions) {
  const page = await browser.newPage();
  await page.setContent("<!doctype html><html><body></body></html>");
  await page.addScriptTag({ content: readFileSync(candidate.file, "utf8") });
  await page.waitForFunction(() => window.Alpine !== undefined);
  const problems = await page.evaluate(probeExpressions, [expressions, GLOBALS]);
  await page.close();
  return problems;
}

function expressionOf(text) {
  const match = /Expression: "([\s\S]*?)"\s*$/m.exec(text);
  return match ? match[1] : text.split("\n")[0];
}

async function checkPage(context, baseUrl, path, scheme) {
  const page = await context.newPage();
  const alpine = new Set();
  const errors = [];
  page.on("console", (message) => {
    const text = message.text();
    if (text.startsWith("Alpine Expression Error") || text.startsWith("Alpine Warning")) {
      alpine.add(expressionOf(text));
    }
  });
  page.on("pageerror", (error) => errors.push(String(error.message).split("\n")[0]));
  const response = await page.goto(new URL(path, baseUrl).href, { waitUntil: "networkidle" });
  await page.waitForTimeout(SETTLE_MS);
  const headers = response ? await response.allHeaders() : {};
  const result = {
    page: path,
    scheme,
    status: response ? response.status() : 0,
    // The enforced policy of the answer, "" without one (a report-only header does not count).
    policy: headers["content-security-policy"] || "",
    converted: await page.evaluate(
      () => document.querySelector('meta[name="papaia-csp"]')?.getAttribute("content") === "strict",
    ),
    violations: await page.evaluate(() => window.__papaiaCsp || []),
    // The version of the Alpine build that started on the page, null without one.
    alpine_version: await page.evaluate(() => (window.Alpine ? String(window.Alpine.version) : null)),
    alpine: [...alpine].sort(),
    errors,
    expressions: await page.evaluate(collectExpressions),
    candidates: {},
  };
  await page.close();
  return result;
}

function summary(results, candidates) {
  const labels = candidates.map((c) => c.label);
  const lines = [
    "### Content security policy (enforced)",
    "",
    `| Page | Scheme | Enforced | Policy reports | Alpine refusals | Script errors |${labels.map((l) => ` Rewrites on ${l} |`).join("")}`,
    `|---|---|---|---:|---:|---:|${labels.map(() => "---:|").join("")}`,
  ];
  for (const r of results) {
    const mark = r.policy ? "yes" : "no";
    const counts = labels.map((l) => ` ${r.candidates[l].length} |`).join("");
    lines.push(`| \`${r.page}\` | ${r.scheme} | ${mark} | ${r.violations.length} | ${r.alpine.length} | ${r.errors.length} |${counts}`);
  }
  const sum = (pick) => results.reduce((n, r) => n + pick(r).length, 0);
  const totals = labels.map((l) => ` ${sum((r) => r.candidates[l])} |`).join("");
  lines.push(
    `| **Total** | | | ${sum((r) => r.violations)} | ${sum((r) => r.alpine)} | ${sum((r) => r.errors)} |${totals}`,
  );
  for (const l of labels) {
    const distinct = new Set(results.flatMap((r) => r.candidates[l].map((x) => x.expression)));
    lines.push("", `Distinct expressions to rewrite on ${l}: ${distinct.size}`);
  }
  return lines.join("\n") + "\n";
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const playwright = pathToFileURL(resolve(REPO, ".pk/node_modules/playwright-core/index.mjs"));
  const { chromium } = await import(playwright.href);
  const browser = await chromium.launch();
  const results = [];
  try {
    for (const scheme of SCHEMES) {
      const context = await browser.newContext({
        ...(args.storage_state ? { storageState: args.storage_state } : {}),
        colorScheme: scheme,
      });
      await context.addInitScript(collectViolations);
      for (const path of args.pages) results.push(await checkPage(context, args.base_url, path, scheme));
      await context.close();
    }
    for (const candidate of args.candidates) {
      const all = results.flatMap((r) => r.expressions);
      const problems = await measure(browser, candidate, all);
      let at = 0;
      for (const r of results) {
        r.candidates[candidate.label] = r.expressions
          .map(([name, expression]) => ({ name, expression, problem: problems[at++] }))
          .filter((x) => x.problem !== null);
      }
    }
  } finally {
    await browser.close();
  }

  const text = summary(results, args.candidates);
  if (args.report) writeFileSync(args.report, JSON.stringify({ results }, null, 2) + "\n");
  if (args.summary) appendFileSync(args.summary, text);
  process.stdout.write(text);

  const broken = results.filter((r) => r.status !== 200);
  for (const r of broken) process.stderr.write(`csp-check: ${r.page} answered ${r.status}\n`);
  if (broken.length) return 2;
  const unenforced = results.filter((r) => !r.policy);
  for (const r of unenforced) {
    process.stderr.write(`csp-check: page ${r.page} (${r.scheme}) answered without the enforced policy\n`);
  }
  const failed = results.filter((r) => r.violations.length || r.alpine.length || r.errors.length);
  for (const r of failed) {
    process.stderr.write(`csp-check: page ${r.page} (${r.scheme}) is not clean:\n`);
    for (const v of r.violations) process.stderr.write(`  policy: ${v.directive} ${v.blocked} ${v.sample || ""}\n`);
    for (const a of r.alpine) process.stderr.write(`  alpine: ${a}\n`);
    for (const e of r.errors) process.stderr.write(`  error: ${e}\n`);
  }
  return failed.length || unenforced.length ? 1 : 0;
}

main().then(
  (code) => process.exit(code),
  (error) => {
    process.stderr.write(`csp-check: ${error.stack || error}\n`);
    process.exit(2);
  },
);
