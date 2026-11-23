// Toasts in the live regions of the frame (shell contract, `#pk-toasts`; UXB-P07, PLT-61-A10).
//
// The frame renders `#pk-toasts` with one region `role="status"` and one `role="alert"` before
// the first message, so a screen reader is already listening when one arrives. A toast is the
// kit's (`toast()` of pk/components.html, rendered once per tone into the <template> elements
// of base.html) with the text put in. Success, info and warning stand at least
// SUCCESS_MIN_MS and as long as the pointer rests on them or the focus is inside; an error
// stays until it is closed with its labelled close button, which the kit's shell.js handles.

export const SUCCESS_MIN_MS = 6000;
export const TONES = Object.freeze(["success", "info", "warning", "error"]);

// How long a toast of this tone stands at least; null: until it is closed.
export function lifetime(tone) {
  return tone === "error" ? null : SUCCESS_MIN_MS;
}

// Removes `element` once `ms` have passed and neither the pointer rests on it nor the focus
// is inside, whichever comes last. Returns a function that cancels the removal.
export function scheduleRemoval(element, ms, clock = globalThis) {
  let due = false;
  let pointer = false;
  let focus = false;
  const settle = () => {
    if (due && !pointer && !focus) element.remove();
  };
  element.addEventListener("pointerenter", () => {
    pointer = true;
  });
  element.addEventListener("pointerleave", () => {
    pointer = false;
    settle();
  });
  element.addEventListener("focusin", () => {
    focus = true;
  });
  element.addEventListener("focusout", (event) => {
    if (event.relatedTarget && element.contains(event.relatedTarget)) return;
    focus = false;
    settle();
  });
  const timer = clock.setTimeout(() => {
    due = true;
    settle();
  }, ms);
  return () => clock.clearTimeout(timer);
}

// Starts the life of a toast that has just been shown: errors are left alone.
export function track(element, tone, clock = globalThis) {
  const ms = lifetime(tone);
  return ms === null ? null : scheduleRemoval(element, ms, clock);
}

function toneOf(type) {
  return TONES.includes(type) ? type : "info";
}

export function createToaster({ doc = document, clock = globalThis } = {}) {
  function region(tone) {
    const box = doc.getElementById("pk-toasts");
    if (!box) throw new Error("the page has no #pk-toasts");
    return box.querySelector(tone === "error" ? ':scope > [role="alert"]' : ':scope > [role="status"]');
  }

  function build(tone) {
    const template = doc.getElementById(`mgr-toast-${tone}`);
    if (!template) throw new Error(`no toast template for "${tone}"`);
    return template.content.firstElementChild.cloneNode(true);
  }

  function place(element, tone, key) {
    if (key) {
      element.dataset.toastKey = key;
      for (const old of region(tone).querySelectorAll("[data-toast-key]")) {
        if (old.dataset.toastKey === key) old.remove();
      }
    }
    region(tone).append(element);
    track(element, tone, clock);
    return element;
  }

  // `html` may contain markup (a "view log" link): callers pass sentences of the data island
  // filled with fmtHtml(), never free text as it came.
  function show(type, html, key) {
    const tone = toneOf(type);
    const element = build(tone);
    element.querySelector(".pk-message-text").innerHTML = html;
    return place(element, tone, key);
  }

  // A three-part message (errors.js, describeFailure). `href` makes part 2 a link, for a todo
  // that is an address ("Sign in again." reloads the page).
  function showError(parts, { key, href } = {}) {
    const element = build("error");
    const body = doc.createElement("div");
    body.className = "pk-message-text";
    const what = doc.createElement("p");
    what.dataset.pkPart = "what";
    what.textContent = parts.what;
    if (parts.consequence) {
      const consequence = doc.createElement("span");
      consequence.dataset.pkPart = "consequence";
      consequence.textContent = parts.consequence;
      what.append(" ", consequence);
    }
    const todo = doc.createElement("p");
    todo.dataset.pkPart = "todo";
    if (href) {
      const link = doc.createElement("a");
      link.href = href;
      link.textContent = parts.todo;
      todo.append(link);
    } else {
      todo.textContent = parts.todo;
    }
    const reference = doc.createElement("p");
    reference.dataset.pkPart = "id";
    const code = doc.createElement("code");
    code.textContent = parts.correlationId;
    reference.append(`${parts.referenceLabel}: `, code);
    body.append(what, todo, reference);
    element.querySelector(".pk-message-text").replaceWith(body);
    return place(element, "error", key);
  }

  return { show, showError };
}
