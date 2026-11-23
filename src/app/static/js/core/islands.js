// Data islands: server values for page scripts without inline script (T-UI-03).
//
// The macro data_island(id, value) of templates/partials/_csp.html writes a value as
// <script type="application/json" id="...">, which the browser never executes. readIsland()
// parses it back; a missing or malformed island is an error rather than undefined, so a page
// never runs on half its data.

export function readIsland(id, doc = document) {
  const element = doc.getElementById(id);
  if (!element) throw new Error(`no data island "${id}"`);
  if (element.tagName !== "SCRIPT" || element.getAttribute("type") !== "application/json") {
    throw new Error(`element "${id}" is not a data island`);
  }
  try {
    return JSON.parse(element.textContent);
  } catch (error) {
    throw new Error(`data island "${id}" is not valid JSON: ${error.message}`);
  }
}
