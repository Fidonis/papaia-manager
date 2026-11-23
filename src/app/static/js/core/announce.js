// What a fragment htmx swaps in announces to its page, without inline script (T-UI-03).
//
// A fragment names the window event in `data-announce` (a name of the form papaia-<words>)
// and its state in `data-active` ("true" or anything else). After every settle the module
// dispatches that event with {active}; the page decides what a change of it means. The job
// strip of the Modules and Overlay tabs (partials/config_job_status.html) uses it.

const NAME = /^papaia(-[a-z]+)+$/;

export function announceSettled(root, win) {
  if (!root || typeof root.matches !== "function") return;
  const found = root.matches("[data-announce]") ? [root] : [];
  if (typeof root.querySelectorAll === "function") found.push(...root.querySelectorAll("[data-announce]"));
  for (const element of found) {
    const name = element.dataset.announce;
    if (!NAME.test(name || "")) continue;
    win.dispatchEvent(new CustomEvent(name, { detail: { active: element.dataset.active === "true" } }));
  }
}

export function installAnnouncements(doc, win) {
  doc.addEventListener("htmx:afterSettle", (event) => announceSettled(event.target, win));
}
