// The actions the pages share: add-on, stack and catalog verbs, and the JSON request of the
// knowledge pages. Each request carries the CSRF token and a correlation id of its own; a
// refusal is told in three parts through `report` (errors.js), a session end through the
// session guard. Built by createActions() from what it talks to, so the tests can hand in
// stand-ins.
import { csrfToken } from "./csrf.js";
import { CORRELATION_HEADER, newCorrelationId, readFailure } from "./errors.js";
import { escapeHtml, fmtHtml } from "./format.js";
import { pollJob } from "./jobs.js";
import { SESSION_STATUS } from "./session.js";

// The error text of a refusal, for the pages that show it next to the input (the knowledge
// pages): the server's `detail`, a refused job's message, or the problems field by field.
export function ragDetail(body, status) {
  if (body && typeof body.detail === "string") return body.detail;
  if (body && body.detail && typeof body.detail.message === "string") return body.detail.message;
  if (body && Array.isArray(body.detail)) {
    return body.detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
  }
  return body ? JSON.stringify(body) : String(status);
}

// The request body of a stack or group verb. `clean_up` maps onto `papaia-ctl stop
// --clean-up`, so it rides along with anything that stops -- a restart included, where it
// turns the operation into a full recreate. A start has no such flag and the API rejects the
// field outright rather than ignoring it.
export function stackBody(action, cleanUp) {
  return action === "start" ? {} : { clean_up: cleanUp };
}

export function createActions({
  texts,
  toaster,
  report,
  fetchImpl = (...args) => globalThis.fetch(...args),
  htmx = () => globalThis.htmx,
  doc = globalThis.document,
  clock = globalThis,
  newId = newCorrelationId,
}) {
  // True when the response ends the session: the person has been told, the caller stops.
  function authGuard(response) {
    if (!response || response.status !== SESSION_STATUS) return false;
    const answered = response.headers && response.headers.get(CORRELATION_HEADER);
    report({ status: SESSION_STATUS, body: null, network: false, correlationId: answered || newId() });
    return true;
  }

  // {ok: true, response} or {ok: false, failure}; a failure is not reported yet.
  async function send(method, url, body) {
    const id = newId();
    const headers = { "X-CSRF-Token": csrfToken(doc), [CORRELATION_HEADER]: id };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    let response;
    try {
      response = await fetchImpl(url, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch {
      return { ok: false, failure: { status: 0, body: null, network: true, correlationId: id } };
    }
    if (response.ok) return { ok: true, response };
    return { ok: false, failure: await readFailure(response, id) };
  }

  // The parsed answer of a write, or null after telling the person why there is none.
  async function post(method, url, body) {
    const result = await send(method, url, body);
    if (!result.ok) {
      report(result.failure);
      return null;
    }
    return result.response.status === 204 ? {} : result.response.json();
  }

  const poll = (jobId, onDone) => pollJob(jobId, onDone, { fetchImpl, clock, guard: authGuard });
  const swap = (url, target) => {
    if (doc.querySelector(target)) htmx().ajax("GET", url, { target, swap: "innerHTML" });
  };
  const addonPanel = (name) => swap("/partials/addons/" + name, "#addon-detail");
  const refreshServices = () => swap("/partials/services", "#service-list");
  const refreshStackRunner = () => swap("/partials/stack-runner", "#stack-runner");
  const refreshCatalogs = () => swap("/partials/catalogs", "#catalog-list");
  const catalogUrl = (name) => "/api/v1/catalogs/" + encodeURIComponent(name);

  const addonPost = (name, verb, body) => post("POST", "/api/v1/addons/" + name + "/" + verb, body || {});

  // Reloads the detail panel after the job finishes.
  function addonAction(name, verb, body) {
    return addonPost(name, verb, body).then((data) => {
      if (!data) return false;
      poll(data.job_id, () => addonPanel(name));
      return true;
    });
  }

  // Runs an action the add-on declares, refreshes the panel when the job ends and says how it
  // went, with a link to the log: the output is often the point.
  function addonMaintenanceAction(name, action) {
    return addonPost(name, "actions/" + action, {}).then((data) => {
      if (!data) return false;
      poll(data.job_id, (job) => {
        addonPanel(name);
        const values = { action, href: "/jobs/" + job.id };
        if (job.status === "failed") toaster.show("error", fmtHtml(texts.maintenanceActionFailed, values));
        else toaster.show("success", fmtHtml(texts.maintenanceActionFinished, values));
      });
      return true;
    });
  }

  // Reloads the dashboard gallery after the job finishes.
  function galleryAction(name, verb, body) {
    return addonPost(name, verb, body).then((data) => {
      if (!data) return false;
      poll(data.job_id, () => swap("/partials/addons", "#addon-gallery"));
      return true;
    });
  }

  // An add-on verb driven from the services page.
  function serviceAddonAction(name, verb, body) {
    return addonPost(name, verb, body).then((data) => {
      if (!data) return false;
      poll(data.job_id, () => refreshServices());
      return true;
    });
  }

  // POST /api/v1/stack/groups/{action} for a set of Compose profiles, poll the job, then
  // refresh the services list. Resolves false when the request was refused, so the caller can
  // release its busy flag; a resolved-true call leaves it set, because the swap that follows
  // replaces the button anyway.
  function stackGroupAction(action, groups, body) {
    return post("POST", "/api/v1/stack/groups/" + action, { groups, ...(body || {}) }).then((data) => {
      if (!data) return false;
      poll(data.job_id, (job) => {
        refreshServices();
        if (job.status === "failed") {
          toaster.show("error", fmtHtml(texts.stackActionFailed, { href: "/jobs/" + job.id }));
        } else {
          toaster.show("success", escapeHtml(texts.stackActionFinished));
        }
      });
      return true;
    });
  }

  // The whole stack, including this container's own profile. The runner outlives the manager,
  // so there is no job to poll: /partials/stack-runner picks the outcome back up, here and
  // after a reload.
  function stackAction(action, body) {
    return post("POST", "/api/v1/stack/" + action, body || {}).then((data) => {
      if (!data) return false;
      refreshStackRunner();
      toaster.show("info", escapeHtml(texts.stackActionStarted));
      return true;
    });
  }

  // POST /api/v1/catalogs/{name}/{verb}, poll the job to completion, then refresh the list and
  // tell the outcome. onJobId (optional) gets the job id as soon as it is known, so a caller
  // can show a "view log" link while the job is still running.
  function catalogAction(name, verb, onJobId) {
    return post("POST", catalogUrl(name) + "/" + verb).then((data) => {
      if (!data) return false;
      if (onJobId) onJobId(data.job_id);
      return new Promise((resolve) => {
        poll(data.job_id, (job) => {
          refreshCatalogs();
          if (job.status === "succeeded") {
            toaster.show("success", fmtHtml(texts.catalogRefreshed, { name }));
          } else {
            toaster.show("error", fmtHtml(texts.catalogRefreshFailed, { name, href: "/jobs/" + data.job_id }));
          }
          resolve(true);
        });
      });
    });
  }

  // PUT /api/v1/catalogs/{name}; synchronous, so refresh the list and tell the outcome.
  function catalogUpdate(name, body) {
    return post("PUT", catalogUrl(name), body).then((data) => {
      if (!data) return false;
      refreshCatalogs();
      toaster.show("success", fmtHtml(texts.catalogUpdated, { name }));
      return true;
    });
  }

  // DELETE /api/v1/catalogs/{name}: answers 204, which htmx never swaps, so the list is
  // refreshed here.
  function catalogDelete(name) {
    return post("DELETE", catalogUrl(name)).then((data) => {
      if (!data) return false;
      refreshCatalogs();
      toaster.show("success", fmtHtml(texts.catalogDeleted, { name }));
      return true;
    });
  }

  async function ragError(response) {
    try {
      return ragDetail(await response.json(), response.status);
    } catch {
      return String(response.status);
    }
  }

  // {ok, data} on success, {ok: false, error} on a refusal the page shows itself,
  // {ok: false, handled: true} when the person has been told already (session end, no answer).
  async function ragRequest(method, url, body) {
    const result = await send(method, url, body);
    if (result.ok) {
      return { ok: true, data: result.response.status === 204 ? null : await result.response.json() };
    }
    const { failure } = result;
    if (failure.network || failure.status === SESSION_STATUS) {
      report(failure);
      return { ok: false, handled: true };
    }
    return { ok: false, error: ragDetail(failure.body, failure.status) };
  }

  return {
    authGuard,
    send,
    addonPost,
    pollJob: poll,
    addonAction,
    addonMaintenanceAction,
    galleryAction,
    serviceAddonAction,
    stackGroupAction,
    stackAction,
    refreshServices,
    refreshStackRunner,
    catalogAction,
    catalogUpdate,
    catalogDelete,
    ragError,
    ragRequest,
  };
}
