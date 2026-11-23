// Sentences with markup of their own, filled without `x-html` (T-UI-03).
//
// A few sentences of the pages carry markup in their translation, e.g.
// `Replace <span class="font-mono">{collection}</span>?`. fillMarked() builds them as DOM nodes:
// the sentence's own tags become elements (only the plain inline tags below, with a `class` and
// nothing else), every `{name}` becomes a text node of its value. A value is never parsed as
// markup, and a tag the list does not know stays text.

const TAGS = new Set(["b", "code", "em", "span", "strong"]);
const TOKEN = /<(\/?)([a-z]+)(?:\s+class="([^"<>]*)")?\s*>|\{(\w+)\}/g;
const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'", nbsp: "\u00a0" };

// The text of a stretch of the sentence, with the few entities a translation uses decoded.
function decode(text) {
  return text.replace(/&(amp|lt|gt|quot|#39|nbsp);/g, (_, name) => ENTITIES[name]);
}

// The parts of `template`: {text}, {open, tag, className}, {close, tag} and {value, name}.
export function markedParts(template) {
  const parts = [];
  const source = String(template);
  let last = 0;
  for (const match of source.matchAll(TOKEN)) {
    const [whole, closing, tag, className, name] = match;
    if (tag !== undefined && !TAGS.has(tag)) continue;
    if (match.index > last) parts.push({ text: decode(source.slice(last, match.index)) });
    if (name !== undefined) parts.push({ value: true, name });
    else if (closing) parts.push({ close: true, tag });
    else parts.push({ open: true, tag, className: className || "" });
    last = match.index + whole.length;
  }
  if (last < source.length) parts.push({ text: decode(source.slice(last)) });
  return parts;
}

// Replaces the content of `el` with `template` filled from `values`.
export function fillMarked(el, template, values = {}) {
  const doc = el.ownerDocument;
  const root = doc.createDocumentFragment();
  const stack = [root];
  for (const part of markedParts(template)) {
    const parent = stack[stack.length - 1];
    if (part.open) {
      const child = doc.createElement(part.tag);
      if (part.className) child.className = part.className;
      parent.appendChild(child);
      stack.push(child);
    } else if (part.close) {
      if (stack.length > 1) stack.pop();
    } else if (part.value) {
      const value = Object.prototype.hasOwnProperty.call(values, part.name) ? values[part.name] : "";
      parent.appendChild(doc.createTextNode(value === null || value === undefined ? "" : String(value)));
    } else {
      parent.appendChild(doc.createTextNode(part.text));
    }
  }
  el.replaceChildren(root);
}
