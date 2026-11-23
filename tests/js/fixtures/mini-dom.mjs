// A small document for the page module tests (node --test): elements with attributes,
// children and text, islands by id, and listeners that can be fired by hand. Just enough of
// the DOM for what the page modules touch; nothing here parses markup.

export class Node {
  constructor(doc) {
    this.ownerDocument = doc;
    this.childNodes = [];
    this.parentNode = null;
  }

  appendChild(child) {
    if (child instanceof Fragment) {
      for (const inner of [...child.childNodes]) this.appendChild(inner);
      child.childNodes = [];
      return child;
    }
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }

  replaceChildren(...nodes) {
    this.childNodes = [];
    for (const node of nodes) this.appendChild(node);
  }

  get textContent() {
    return this.childNodes.map((child) => child.textContent).join("");
  }
}

export class Text extends Node {
  constructor(doc, data) {
    super(doc);
    this.data = data;
  }

  get textContent() {
    return this.data;
  }
}

export class Fragment extends Node {}

export class Element extends Node {
  constructor(doc, tag, attrs = {}) {
    super(doc);
    this.tagName = tag.toUpperCase();
    this.attributes = new Map(Object.entries(attrs));
    this.className = attrs.class || "";
    this.id = attrs.id || "";
    this.listeners = {};
    this.value = attrs.value || "";
    this.innerHTML = "";
    this.disabled = false;
    const properties = {};
    this.style = {
      properties,
      setProperty(name, value) {
        properties[name] = value;
      },
    };
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }

  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }

  // The element itself or the first ancestor that has `attribute` ("[name]" selectors only).
  closest(selector) {
    const name = selector.replace(/^\[|\]$/g, "");
    for (let el = this; el && el instanceof Element; el = el.parentNode) {
      if (el.hasAttribute(name)) return el;
    }
    return null;
  }

  // "[attr]" selectors only.
  matches(selector) {
    return this.hasAttribute(selector.replace(/^\[|\]$/g, ""));
  }

  // "[attr]" selectors only, over the descendants in document order.
  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => {
      for (const child of node.childNodes) {
        if (child instanceof Element) {
          if (child.matches(selector)) found.push(child);
          walk(child);
        }
      }
    };
    walk(this);
    return found;
  }

  // "#id[attr]" or "#parent [attr]" selectors, over the descendants.
  querySelector(selector) {
    const all = [];
    const walk = (node) => {
      for (const child of node.childNodes) {
        if (child instanceof Element) {
          all.push(child);
          walk(child);
        }
      }
    };
    walk(this);
    const scoped = selector.match(/^#([\w-]+) \[([\w-]+)\]$/);
    if (scoped) {
      const parent = all.find((el) => el.id === scoped[1]);
      return parent ? parent.querySelector(`[${scoped[2]}]`) : null;
    }
    const match = selector.match(/^(?:#([\w-]+))?(?:\[([\w-]+)\])?$/);
    return all.find((el) => (!match[1] || el.id === match[1]) && (!match[2] || el.hasAttribute(match[2]))) || null;
  }
}

export class MiniDocument {
  constructor() {
    this.documentElement = new Element(this, "html", { lang: "en" });
    this.documentElement.lang = "en";
    this.body = new Element(this, "body");
    this.listeners = {};
    this.islands = {};
  }

  createElement(tag) {
    return new Element(this, tag);
  }

  createTextNode(data) {
    return new Text(this, data);
  }

  createDocumentFragment() {
    return new Fragment(this);
  }

  element(tag, attrs = {}, parent = this.body) {
    const el = new Element(this, tag, attrs);
    parent.appendChild(el);
    return el;
  }

  island(id, value) {
    this.islands[id] = value;
  }

  getElementById(id) {
    if (Object.prototype.hasOwnProperty.call(this.islands, id)) {
      const value = this.islands[id];
      return {
        tagName: "SCRIPT",
        getAttribute: (name) => (name === "type" ? "application/json" : null),
        textContent: JSON.stringify(value),
      };
    }
    if (this.body.id === id) return this.body;
    return this.body.querySelector("#" + id);
  }

  querySelector(selector) {
    return this.body.querySelector(selector);
  }

  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }

  fire(type, event) {
    for (const fn of this.listeners[type] || []) fn(event);
  }
}

