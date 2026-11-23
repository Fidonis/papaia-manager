// Floating row menus.
//
// A menu marked `.dropdown-float` (overflow_menu(float=True), for the rows of a table that
// scrolls sideways) is `position: fixed`, so the scroll container neither clips it nor grows a
// scrollbar for it. It still opens on focus, like every daisyUI dropdown; here it is put next
// to its trigger, right-aligned, and above the trigger when there is no room below, because a
// fixed menu cannot be scrolled to. Scrolling or resizing closes it: it would stay behind.
// The position is set through the CSSOM, which the page policy allows.

const GAP = 4;
const EDGE = 8;

export function floatPosition(trigger, size, viewport) {
  let top = trigger.bottom + GAP;
  if (top + size.height > viewport.height - EDGE) {
    top = trigger.top - GAP - size.height >= EDGE
      ? trigger.top - GAP - size.height
      : Math.max(EDGE, viewport.height - size.height - EDGE);
  }
  const left = Math.max(EDGE, Math.min(trigger.right - size.width, viewport.width - size.width - EDGE));
  return { top, left };
}

export function placeFloatMenu(dropdown, win = window) {
  const trigger = dropdown.querySelector('[role="button"]');
  const menu = dropdown.querySelector(".dropdown-content");
  if (!trigger || !menu) return;
  const { top, left } = floatPosition(
    trigger.getBoundingClientRect(),
    { width: menu.offsetWidth, height: menu.offsetHeight },
    { width: win.innerWidth, height: win.innerHeight },
  );
  menu.style.top = `${top}px`;
  menu.style.left = `${left}px`;
  menu.style.right = "auto";
  menu.style.bottom = "auto";
}

export function closeFloatMenu(doc = document) {
  const active = doc.activeElement;
  if (active && active.blur && active.closest && active.closest(".dropdown-float")) active.blur();
}

export function installFloatMenus(win = window, doc = document) {
  doc.addEventListener("focusin", (event) => {
    const dropdown = event.target.closest && event.target.closest(".dropdown-float");
    if (dropdown) placeFloatMenu(dropdown, win);
  });
  win.addEventListener("scroll", () => closeFloatMenu(doc), true);
  win.addEventListener("resize", () => closeFloatMenu(doc));
}
