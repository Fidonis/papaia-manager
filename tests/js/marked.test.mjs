// Unit tests of core/marked.js (node --test): sentences with markup of their own, built as
// nodes, with every value as text.
import assert from "node:assert/strict";
import { test } from "node:test";

import { fillMarked, markedParts } from "../../src/app/static/js/core/marked.js";
import { MiniDocument } from "./fixtures/mini-dom.mjs";

function render(template, values) {
  const doc = new MiniDocument();
  const el = doc.element("span");
  fillMarked(el, template, values);
  return el;
}

test("a sentence becomes text, its tags elements and its values text nodes", () => {
  const el = render('Replace <span class="font-mono">{collection}</span>?', { collection: "kb" });

  assert.equal(el.textContent, "Replace kb?");
  const [before, span, after] = el.childNodes;
  assert.equal(before.textContent, "Replace ");
  assert.equal(span.tagName, "SPAN");
  assert.equal(span.className, "font-mono");
  assert.equal(span.childNodes[0].textContent, "kb");
  assert.equal(after.textContent, "?");
});

test("a value is never markup", () => {
  const el = render("<b>{name}</b> deleted", { name: '<img src=x onerror="alert(1)">' });

  assert.equal(el.childNodes.length, 2);
  assert.equal(el.childNodes[0].tagName, "B");
  assert.equal(el.childNodes[0].childNodes.length, 1);
  assert.equal(el.childNodes[0].textContent, '<img src=x onerror="alert(1)">');
});

test("a tag outside the list and an attribute other than class stay text", () => {
  assert.deepEqual(markedParts('<a href="x">go</a>'), [{ text: '<a href="x">go</a>' }]);
  assert.deepEqual(markedParts('<span onclick="x">y</span>'), [
    { text: '<span onclick="x">y' },
    { close: true, tag: "span" },
  ]);
});

test("missing and empty values are empty text, numbers are text", () => {
  assert.equal(render("({points} points)", { points: 1200 }).textContent, "(1200 points)");
  assert.equal(render("{a}|{b}|{c}", { a: null, b: undefined }).textContent, "||");
});

test("entities of the translation are decoded", () => {
  assert.equal(render("A &amp; B&nbsp;·&lt;c&gt;").textContent, "A & B ·<c>");
});

test("filling again replaces what was there", () => {
  const doc = new MiniDocument();
  const el = doc.element("span");
  fillMarked(el, "{n}", { n: "one" });
  fillMarked(el, "{n}", { n: "two" });

  assert.equal(el.textContent, "two");
});
