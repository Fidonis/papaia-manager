// Texts of the shared page modules: filling a sentence with values, escaping, plurals.
//
// The sentences come from the data island of base.html in the language of the page, a whole
// sentence each, never pieces glued together: the order of the pieces differs between
// languages. `{name}` marks a value; fmtHtml() fills a sentence that carries markup and
// escapes the values.

const ENTITIES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ENTITIES[c]);
}

function fill(template, values, convert) {
  return String(template).replace(/\{(\w+)\}/g, (match, key) =>
    Object.prototype.hasOwnProperty.call(values, key) ? convert(values[key]) : match,
  );
}

export function fmt(template, values) {
  return fill(template, values || {}, (value) => value);
}

export function fmtHtml(template, values) {
  return fill(template, values || {}, escapeHtml);
}

// Picks between the two messages of a counted noun with the browser's plural rules for the
// page's language ("one" or everything else) and fills `{count}`. A language with more plural
// forms than that needs this extended, together with its catalogue.
export function fmtPlural(count, one, other, values, html, lang) {
  const language = lang || (globalThis.document && document.documentElement.lang) || undefined;
  const category = new Intl.PluralRules(language).select(count);
  return (html ? fmtHtml : fmt)(category === "one" ? one : other, { count, ...(values || {}) });
}

// The status row's "checked" time (partials/service_status_chip.html): the server sends UTC,
// the reader wants their own clock.
export function localizeCheckedAt(root, justNow) {
  for (const element of root.querySelectorAll("[data-checked-at]")) {
    const when = new Date(element.dataset.checkedAt);
    element.textContent = Number.isNaN(when.getTime()) ? justNow : when.toLocaleTimeString();
  }
}
