// Data islands: server values for page scripts without inline script (T-UI-03).
//
// The macro data_island(id, value) of templates/partials/_csp.html writes a value as
// <script type="application/json" id="...">, which the browser never executes. A partial
// htmx swaps into a page uses swapped_island(id, value) instead, a
// <template id="..." data-island="application/json">: htmx drops every script element of a
// swapped fragment (allowScriptTags: false) but keeps a template. readIsland() parses either
// back; a missing or malformed island is an error rather than undefined, so a page never runs
// on half its data.

const JSON_TYPE = "application/json";

// The value of the island `element`, a JSON script element or a marked template element;
// `id` names it in an error.
export function islandValue(element, id = element && element.id ? element.id : "") {
  let text;
  if (element && element.tagName === "SCRIPT" && element.getAttribute("type") === JSON_TYPE) {
    text = element.textContent;
  } else if (element && element.tagName === "TEMPLATE" && element.getAttribute("data-island") === JSON_TYPE) {
    text = element.content.textContent;
  } else {
    throw new Error(`element "${id}" is not a data island`);
  }
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new Error(`data island "${id}" is not valid JSON: ${error.message}`);
  }
}

export function readIsland(id, doc = document) {
  const element = doc.getElementById(id);
  if (!element) throw new Error(`no data island "${id}"`);
  return islandValue(element, id);
}
