// Transition: the names page templates still call as globals (inline scripts, Alpine
// expressions, on* attributes). Each is assigned to `window` by name, listed here; a page
// ticket that moves its page into a module removes the names it no longer needs, and the last
// one removes this file. tests/test_js_core.py checks that the list is exactly the names the
// templates still use.

export const LEGACY_NAMES = Object.freeze([
  "_authGuard",
  "_ragDetail",
  "_ragError",
  "_ragEscape",
  "_ragRequest",
  "csrfToken",
  "fmt",
  "fmtHtml",
  "fmtPlural",
  "showToast",
]);

export function installLegacy(target, api) {
  const missing = LEGACY_NAMES.filter((name) => !Object.prototype.hasOwnProperty.call(api, name));
  if (missing.length) throw new Error(`legacy names without a value: ${missing.join(", ")}`);
  for (const name of LEGACY_NAMES) target[name] = api[name];
}
