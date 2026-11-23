// Error messages in three parts (pattern 8 of the kit; PLT-62-A5): what happened, what to do,
// and the reference, in this order and in the language of the page.
//
// A refused request is sorted into the error class of the kit by its error envelope: the
// `class` of a kit envelope when the body is one, otherwise the status code. The texts of each
// class are the kit's (app/client_texts.py writes them into the data island of base.html), so
// "connection stopped" (the service behind the request does not answer), "right missing",
// "input invalid" and "session expired" read the same in every module. The reference is the
// correlation id the server logged for the request (`X-Papaia-Correlation-Id`); the request
// sends one of its own, which the server takes over, so even a request that never got an
// answer has one. A status code alone, a stack trace or a key name never reaches the person.

export const CORRELATION_HEADER = "X-Papaia-Correlation-Id";

export const ERROR_CLASSES = Object.freeze([
  "unauthenticated",
  "forbidden_or_not_found",
  "invalid_input",
  "conflict",
  "rate_limited",
  "upstream_unavailable",
  "identity_revoked",
  "class_exceeded",
  "contract_mismatch",
  "budget_exceeded",
  "internal_error",
]);

const BY_STATUS = new Map([
  [400, "invalid_input"],
  [401, "unauthenticated"],
  [403, "forbidden_or_not_found"],
  [404, "forbidden_or_not_found"],
  [409, "conflict"],
  [412, "conflict"],
  [413, "invalid_input"],
  [415, "invalid_input"],
  [422, "invalid_input"],
  [429, "rate_limited"],
  [502, "upstream_unavailable"],
  [503, "upstream_unavailable"],
  [504, "upstream_unavailable"],
]);

// The classes whose part 1 may be the server's own sentence, when it is one: a refusal of
// the input or of a change says best itself what did not fit ("The add-on is still required
// by ..."). Every other class keeps the kit's sentence.
const OWN_SENTENCE = new Set(["invalid_input", "conflict"]);
const MAX_SENTENCE = 300;

// The patterns of the kit's forbidden-patterns.yaml that a server sentence can contain.
const FORBIDDEN = [
  /[A-Z]{3,}-\d|\b(?:PK|FLOW|DATA|STUDIO|ASSIST|ME|CONNECTIONS|MCP)-[A-Z0-9]/,
  /(?<![\w.,])[45]\d\d(?![\w,]|[.,]\d)/,
  /\b[A-Za-z][A-Za-z0-9]*(?:[_.][A-Za-z0-9]+)+\b|\b_[A-Za-z0-9]/,
  /\b(?:null|undefined|nil|nan|\w*exception|\w*errors?|\w*tokens?|scopes?|bearer)\b/i,
  /traceback|\bat\s+\S+\s+\(\S+:\d+(?::\d+)?\)|file "[^"]*", line \d+/i,
  /[{}[\]<>]|\w=\S/,
  /(?=[A-Za-z0-9+/_=-]*\d)[A-Za-z0-9+/_=-]{20,}|\beyJ[A-Za-z0-9_-]{4,}/,
  /(?:^|\s)\/[A-Za-z0-9_.-]+|\bhttps?:\/\//i,
];

export function failureClass({ status, body, network }) {
  if (network) return "upstream_unavailable";
  if (body && typeof body === "object" && ERROR_CLASSES.includes(body.class)) return body.class;
  if (BY_STATUS.has(status)) return BY_STATUS.get(status);
  return "internal_error";
}

export function isPlainSentence(text) {
  if (typeof text !== "string") return false;
  const sentence = text.trim();
  if (!sentence || sentence.length > MAX_SENTENCE || /[\u0000-\u001f]/.test(sentence)) return false;
  return !FORBIDDEN.some((pattern) => pattern.test(sentence));
}

// The parts of the message: {errorClass, what, consequence, todo, referenceLabel,
// correlationId}.
// `texts` is the `errors` entry of the data island: {reference_label, classes: {class: {what,
// consequence, todo}}}.
export function describeFailure(failure, texts) {
  const errorClass = failureClass(failure);
  const entry = texts.classes[errorClass] || texts.classes.internal_error;
  const detail = failure.body && typeof failure.body === "object" ? failure.body.detail : undefined;
  const own = OWN_SENTENCE.has(errorClass) && isPlainSentence(detail);
  return {
    errorClass,
    what: own ? detail.trim() : entry.what,
    consequence: entry.consequence || "",
    todo: entry.todo,
    referenceLabel: texts.reference_label,
    correlationId: failure.correlationId,
  };
}

// A correlation id for a request this page sends (the kit's rule: letters, digits, hyphens).
export function newCorrelationId(cryptoApi = globalThis.crypto) {
  return cryptoApi.randomUUID();
}

// The failure of an answered request: the status, the parsed body when there is one, and the
// correlation id the server answered with, or the one the request was sent with.
export async function readFailure(response, sentId) {
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  const answered = response.headers && response.headers.get(CORRELATION_HEADER);
  return { status: response.status, body, network: false, correlationId: answered || sentId };
}
