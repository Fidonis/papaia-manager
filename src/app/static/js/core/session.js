// Session expiry and refused htmx requests.
//
// The server renews an expiring Keycloak token on its own; a 401 only arrives once the SSO
// session itself is over. An XHR cannot follow the cross-origin sign-in redirect, so the page
// says what happened in the three parts and offers "Sign in again" as a link to the page
// itself: loading it in the top-level context runs the sign-in, and the server sends the
// person back here. One such message at a time, however many polls run into the same end.
//
// Every htmx request carries a correlation id of its own (htmx:configRequest), so a refusal
// of a request the person started (anything but GET) is told with the reference the server
// logged. A poll (GET) that fails stays quiet: the next one tries again.
import { CORRELATION_HEADER, newCorrelationId } from "./errors.js";

export const SESSION_KEY = "session-expired";
export const SESSION_STATUS = 401;

function headerOf(detail) {
  const config = detail && detail.requestConfig;
  const headers = config && config.headers;
  return headers ? headers[CORRELATION_HEADER] || "" : "";
}

function verbOf(detail) {
  const config = detail && detail.requestConfig;
  return String((config && config.verb) || "get").toLowerCase();
}

function parse(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

// The failure of an htmx request from the detail of htmx:responseError or htmx:sendError.
export function htmxFailure(detail, network) {
  const xhr = detail && detail.xhr;
  const sent = headerOf(detail);
  if (network || !xhr) return { status: 0, body: null, network: true, correlationId: sent };
  return {
    status: xhr.status,
    body: parse(xhr.responseText),
    network: false,
    correlationId: xhr.getResponseHeader(CORRELATION_HEADER) || sent,
  };
}

// `report(failure)` shows a failure; it is the one place that tells a session end apart.
export function installSessionGuard(doc, report, newId = newCorrelationId) {
  doc.addEventListener("htmx:configRequest", (event) => {
    if (event.detail && event.detail.headers) event.detail.headers[CORRELATION_HEADER] = newId();
  });
  doc.addEventListener("htmx:responseError", (event) => {
    const failure = htmxFailure(event.detail, false);
    if (failure.status === SESSION_STATUS || verbOf(event.detail) !== "get") report(failure);
  });
  doc.addEventListener("htmx:sendError", (event) => {
    if (verbOf(event.detail) !== "get") report(htmxFailure(event.detail, true));
  });
}
