// Unit tests of the chart of Models & Usage (src/app/static/js/pages/models.js, node --test).
import assert from "node:assert/strict";
import { test } from "node:test";

import { applyChartGeometry, chartTips, createUsageChart } from "../../src/app/static/js/pages/models.js";
import { MiniDocument } from "./fixtures/mini-dom.mjs";

const TIPS = [
  { title: "Thu, Oct 22", rows: [], total: "0" },
  { title: "Fri, Oct 23", rows: [{ name: "qwen", value: "1,200", slot: 1, muted: false }], total: "1,200" },
  { title: "Sat, Oct 24", rows: [{ name: "Other", value: "3", slot: -1, muted: true }], total: "3" },
  { title: "Sun, Oct 25", rows: [], total: "0" },
];

test("the focus starts at the last day and the arrow keys stay inside the window", () => {
  const chart = createUsageChart(TIPS);
  chart.focusChart();
  assert.equal(chart.i, 3);
  chart.next();
  assert.equal(chart.i, 3);
  chart.previous();
  chart.previous();
  chart.previous();
  chart.previous();
  assert.equal(chart.i, 0);
  chart.focusChart();
  assert.equal(chart.i, 0, "a day already shown stays");
  chart.leave();
  assert.equal(chart.i, -1);
});

test("the tooltip shows the day under the pointer", () => {
  const chart = createUsageChart(TIPS);
  assert.equal(chart.tipTitle(), "");
  assert.deepEqual(chart.tipRows(), []);
  assert.equal(chart.announcement(), "");

  chart.show(1);
  assert.equal(chart.columnClass(1), "bg-base-200");
  assert.equal(chart.columnClass(2), "");
  assert.equal(chart.tipTitle(), "Fri, Oct 23");
  assert.equal(chart.tipTotal(), "1,200");
  assert.equal(chart.tipRows()[0].name, "qwen");
  assert.equal(chart.announcement(), "Fri, Oct 23: 1,200");
  assert.equal(chart.rowClass(chart.tipRows()[0]), "chart-s1");
  chart.show(2);
  assert.equal(chart.rowClass(chart.tipRows()[0]), "chart-so chart-muted");
});

test("the tooltip opens away from the edge, as a property object", () => {
  const chart = createUsageChart(TIPS);
  chart.show(0);
  assert.deepEqual(chart.tipPosition(), { left: "calc(25% + 6px)", right: "" });
  chart.show(3);
  assert.deepEqual(chart.tipPosition(), { left: "", right: "calc(25% + 6px)" });
});

test("the chart reads its tips from data-tips when none are handed in", () => {
  const doc = new MiniDocument();
  const el = doc.element("div", { "data-tips": JSON.stringify(TIPS) });
  const chart = createUsageChart();
  chart.$el = el;
  chart.init();

  assert.equal(chart.tips.length, 4);
  assert.deepEqual(chartTips(doc.element("div")), []);
  assert.throws(() => chartTips(doc.element("div", { "data-tips": "{}" })), /not a list/);
});

test("the chart geometry is set as style properties, never from a value that is not a number", () => {
  const doc = new MiniDocument();
  const body = doc.element("div", { "data-pct-width": "40.0" });
  const tick = doc.element("span", { "data-pct-bottom": "33.3" }, body);
  const bar = doc.element("div", { "data-pct-height": "12.50" }, body);
  const spark = doc.element("polyline", { "data-chart-stroke": "2" }, body);
  const other = doc.element("polyline", { "data-chart-stroke": "other" }, body);
  const bad = doc.element("div", { "data-pct-height": "1;color:red" }, body);
  const evil = doc.element("polyline", { "data-chart-stroke": "0);background:url(x" }, body);

  assert.equal(applyChartGeometry(body), 5);
  assert.deepEqual(body.style.properties, { width: "40%" });
  assert.deepEqual(tick.style.properties, { bottom: "33.3%" });
  assert.deepEqual(bar.style.properties, { height: "12.5%" });
  assert.deepEqual(spark.style.properties, { stroke: "var(--chart-2)" });
  assert.deepEqual(other.style.properties, { stroke: "var(--chart-other)" });
  assert.deepEqual(bad.style.properties, {});
  assert.deepEqual(evil.style.properties, {});
  assert.equal(applyChartGeometry(null), 0);
});
