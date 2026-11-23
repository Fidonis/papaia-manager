// Models & Usage (models.html with partials/models_body.html).
//
// The chart of the body is the Alpine component `usageChart`: `i` is the day under the
// pointer or the keyboard (arrow keys while the chart has focus), -1 for none. Its tooltips
// come from the chart's `data-tips` attribute, one per day: {title, rows: [{name, value,
// slot, muted}], total}. Tooltip text is set with x-text, never as markup.
//
// Loaded after core/main.js and before Alpine (base.html, block `page_modules`), on the CSP
// build of Alpine: the template calls plain methods of the component.

export function createUsageChart(tips = null) {
  return {
    i: -1,
    tips: tips || [],

    // Without tips handed in, the chart reads them from its own element.
    init() {
      if (tips === null) this.tips = chartTips(this.$el);
    },

    // The chart got the focus: start at the last day unless a day is shown already.
    focusChart() {
      if (this.i < 0) this.i = this.tips.length - 1;
    },
    leave() {
      this.i = -1;
    },
    show(index) {
      this.i = index;
    },
    previous() {
      this.i = Math.max(0, this.i - 1);
    },
    next() {
      this.i = Math.min(this.tips.length - 1, this.i + 1);
    },
    columnClass(index) {
      return this.i === index ? "bg-base-200" : "";
    },
    tip() {
      return this.i >= 0 && this.i < this.tips.length ? this.tips[this.i] : null;
    },
    tipTitle() {
      const tip = this.tip();
      return tip ? tip.title : "";
    },
    tipRows() {
      const tip = this.tip();
      return (tip && tip.rows) || [];
    },
    tipTotal() {
      const tip = this.tip();
      return tip ? tip.total : "";
    },
    // Left of the middle the tooltip opens to the right of the day, else to its left. An
    // object, so Alpine sets the property through the CSSOM rather than a style attribute.
    tipPosition() {
      const count = this.tips.length;
      if (this.i < count / 2) return { left: `calc(${((this.i + 1) / count) * 100}% + 6px)`, right: "" };
      return { left: "", right: `calc(${((count - this.i) / count) * 100}% + 6px)` };
    },
    rowClass(row) {
      return (row.slot < 0 ? "chart-so" : "chart-s" + row.slot) + (row.muted ? " chart-muted" : "");
    },
    // The visually hidden line that repeats the day for a screen reader.
    announcement() {
      const tip = this.tip();
      return tip ? tip.title + ": " + tip.total : "";
    },
  };
}

// The tooltips of a chart, from its `data-tips` attribute; none when it is missing.
export function chartTips(el) {
  const raw = el.getAttribute("data-tips");
  if (!raw) return [];
  const tips = JSON.parse(raw);
  if (!Array.isArray(tips)) throw new Error("data-tips is not a list");
  return tips;
}

// The geometry of the charts: `data-pct-bottom`, `data-pct-height` and `data-pct-width` in
// percent, `data-chart-stroke` the chart slot of a sparkline. Set through the CSSOM, which the
// policy allows, instead of inline `style` attributes, which it refuses.
const PERCENT_PROPERTIES = ["bottom", "height", "width"];
const SLOT = /^(other|\d{1,2})$/;

export function applyChartGeometry(root) {
  if (!root || typeof root.querySelectorAll !== "function") return 0;
  let set = 0;
  const within = (selector) => [
    ...(typeof root.matches === "function" && root.matches(selector) ? [root] : []),
    ...root.querySelectorAll(selector),
  ];
  for (const property of PERCENT_PROPERTIES) {
    for (const el of within(`[data-pct-${property}]`)) {
      const value = Number(el.getAttribute(`data-pct-${property}`));
      if (!Number.isFinite(value)) continue;
      el.style.setProperty(property, `${value}%`);
      set++;
    }
  }
  for (const el of within("[data-chart-stroke]")) {
    const slot = el.getAttribute("data-chart-stroke");
    if (!SLOT.test(slot)) continue;
    el.style.setProperty("stroke", `var(--chart-${slot})`);
    set++;
  }
  return set;
}

export function installModels(win, doc) {
  // The body arrives through htmx (models.html); each swap brings its charts.
  doc.addEventListener("htmx:load", (event) => {
    applyChartGeometry(event.detail && event.detail.elt ? event.detail.elt : event.target);
  });
  doc.addEventListener("alpine:init", () => {
    win.Alpine.data("usageChart", () => createUsageChart());
  });
}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  installModels(window, document);
}
