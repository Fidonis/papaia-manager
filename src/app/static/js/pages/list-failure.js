// What a list of the Users and Roles pages shows when its request is refused. htmx does not
// swap a 4xx or 5xx answer, which would leave the page spinning; this says why instead. A 401
// is handled by the session guard of the page. Built from DOM nodes with textContent, so the
// server's reason is never read as markup.
import { ragDetail } from "../core/actions.js";

// texts: listFailed (the fallback reason), listUnavailable (the heading), reloadPage (the link).
export function listFailureHandler(doc, targetId, href, texts) {
  return function (event) {
    const detail = event.detail || {};
    if (!detail.target || detail.target.id !== targetId) return;
    if (detail.xhr && detail.xhr.status === 401) return;
    let reason = texts.listFailed;
    try { reason = ragDetail(JSON.parse(detail.xhr.responseText), detail.xhr.status); } catch (_) {}
    const box = doc.createElement("div");
    box.className = "flex flex-col items-center py-24 gap-3 text-pk-text-muted text-center";
    const title = doc.createElement("p");
    title.className = "text-lg font-semibold";
    title.textContent = texts.listUnavailable;
    const why = doc.createElement("p");
    why.className = "max-w-xl text-sm";
    why.textContent = reason;
    const link = doc.createElement("a");
    link.className = "link";
    link.href = href;
    link.textContent = texts.reloadPage;
    box.append(title, why, link);
    detail.target.replaceChildren(box);
  };
}
