// Page module of the knowledge map (knowledge_map.html, T-UI-03). It runs before Alpine
// (base.html, block `page_modules`) and registers the component `knowledgeMap` when Alpine
// starts; the component reads its texts from the data island `km-i18n` when the page
// creates it.
import { readIsland } from "../core/islands.js";
import { createKnowledgeMap } from "./knowledge-map/component.js";

document.addEventListener("alpine:init", () => {
  window.Alpine.data("knowledgeMap", () => createKnowledgeMap(readIsland("km-i18n")));
});
