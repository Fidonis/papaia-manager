// Nodes (nodes.html) and a node's page (node_detail.html), T-UI-03.
//
// Both pages offer their writes only after a fresh sign-in; then the module adds the
// listeners the elements used to carry as on* attributes: the form "Add a node", and on a
// node's page the buttons marked `data-node-action` (pinKey with the fingerprint in
// `data-fingerprint`, sendBundle, removeNode). The node's name and the sentences come from the
// data island `nodes-init`. Everything the page talks to comes in as an argument; the bottom
// of this file wires it up.
import { csrfToken } from "../core/csrf.js";
import { readIsland } from "../core/islands.js";

export function installNodesPage(doc, { init, fetchImpl, alert, confirm, navigate, reload }) {
  const texts = init.texts;
  const name = init.name;
  const headers = () => ({
    "Content-Type": "application/json",
    "X-CSRF-Token": csrfToken(doc),
  });

  function addNode(event) {
    event.preventDefault();
    const form = event.target;
    return fetchImpl("/api/v1/nodes", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({
        name: form.elements["name"].value,
        address: form.elements["address"].value,
        ssh_user: form.elements["ssh_user"].value,
        ssh_port: Number(form.elements["ssh_port"].value) || 0,
      }),
    }).then(async function (r) {
      let data = {};
      try { data = await r.json(); } catch { /* no body */ }
      if (!r.ok) {
        alert(texts.addFailed + " " + (data.detail || r.status));
        return;
      }
      navigate("/nodes/" + encodeURIComponent(data.name));
    });
  }

  function nodeRequest(method, url, body) {
    return fetchImpl(url, {
      method: method,
      headers: headers(),
      body: body ? JSON.stringify(body) : undefined,
    }).then(async function (r) {
      let data = {};
      try { data = await r.json(); } catch { /* no body */ }
      if (r.ok) return data;
      alert(texts.failed + " " + (data.detail || r.status));
      return null;
    });
  }

  const nodeUrl = () => "/api/v1/nodes/" + encodeURIComponent(name);

  function removeNode() {
    if (!confirm(texts.removeQuestion)) return Promise.resolve();
    return nodeRequest("DELETE", nodeUrl()).then(function (done) {
      if (!done) return;
      if (done.notes && done.notes.length) alert(done.notes.join("\n\n"));
      navigate("/nodes");
    });
  }

  function sendBundle() {
    return nodeRequest("POST", nodeUrl() + "/bundle").then(function (done) {
      if (done) reload();
    });
  }

  function pinKey(fingerprint) {
    return nodeRequest("POST", nodeUrl() + "/pin", { fingerprint: fingerprint }).then(function (ok) {
      if (ok) reload();
    });
  }

  const form = doc.getElementById("add-node");
  if (form) form.addEventListener("submit", addNode);
  const verbs = {
    pinKey: (button) => pinKey(button.dataset.fingerprint),
    sendBundle: () => sendBundle(),
    removeNode: () => removeNode(),
  };
  doc.querySelectorAll("[data-node-action]").forEach((button) => {
    const verb = verbs[button.dataset.nodeAction];
    if (!verb) throw new Error(`unknown node action ${button.dataset.nodeAction}`);
    button.addEventListener("click", () => verb(button));
  });
}

// The page: module scripts run once the document is parsed, so the form and the buttons are
// there. Without a fresh sign-in neither the island nor this module is rendered.
if (globalThis.document && document.getElementById("nodes-init")) {
  installNodesPage(document, {
    init: readIsland("nodes-init"),
    fetchImpl: (...args) => window.fetch(...args),
    alert: (text) => window.alert(text),
    confirm: (text) => window.confirm(text),
    navigate: (url) => {
      window.location.href = url;
    },
    reload: () => window.location.reload(),
  });
}
