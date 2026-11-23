// Three-part error messages (core/errors.js, PLT-62-A5) for the four cases "connection
// stopped", "right missing", "input invalid" and "session expired": what happened, what to do,
// the reference with the correlation id the server logged, never a status code alone, a stack
// trace or a key name. The texts are the kit's English ones (fixtures/error-texts.en.json,
// kept equal to app/client_texts.py by tests/test_core_texts.py).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  CORRELATION_HEADER,
  describeFailure,
  failureClass,
  isPlainSentence,
  readFailure,
} from "../../src/app/static/js/core/errors.js";

const TEXTS = JSON.parse(readFileSync(new URL("./fixtures/error-texts.en.json", import.meta.url), "utf8"));
const ID = "0190f3a2-7c4e-7a51-9b1d-2f6c8e4a1b3d";

function response(status, body, correlationId = ID) {
  return {
    status,
    ok: status < 400,
    headers: { get: (name) => (name === CORRELATION_HEADER ? correlationId : null) },
    json: async () => {
      if (body === undefined) throw new SyntaxError("Unexpected end of JSON input");
      return body;
    },
  };
}

// The patterns the person must never read (the kit's forbidden-patterns.yaml, W5).
const FORBIDDEN = {
  status_code: /(?<![\w.,])[45]\d\d(?![\w,]|[.,]\d)/,
  stack_trace: /traceback|\bat\s+\S+\s+\(\S+:\d+(?::\d+)?\)|file "[^"]*", line \d+/i,
  key_name: /\b[A-Za-z][A-Za-z0-9]*(?:[_.][A-Za-z0-9]+)+\b|\b_[A-Za-z0-9]/,
};

function assertThreeParts(parts, expected) {
  assert.equal(parts.errorClass, expected.errorClass);
  assert.equal(parts.what, expected.what);
  assert.equal(parts.todo, expected.todo);
  assert.equal(parts.referenceLabel, "Reference");
  assert.equal(parts.correlationId, expected.id ?? ID);
  for (const [name, pattern] of Object.entries(FORBIDDEN)) {
    for (const shown of [parts.what, parts.consequence, parts.todo]) {
      assert.equal(pattern.test(shown), false, `${name} in "${shown}"`);
    }
  }
}

test("connection stopped: the service behind the request does not answer", async () => {
  for (const status of [502, 503, 504]) {
    const parts = describeFailure(await readFailure(response(status, { detail: "upstream_timeout" })), TEXTS);
    assertThreeParts(parts, {
      errorClass: "upstream_unavailable",
      what: "A service involved is not reachable right now.",
      todo: "Try again in a few minutes.",
    });
    assert.equal(parts.consequence, "Nothing was carried out.");
  }
});

test("connection stopped: no answer at all keeps the id the request was sent with", () => {
  const sent = "0190f3a2-0000-7000-8000-000000000001";
  const parts = describeFailure({ status: 0, body: null, network: true, correlationId: sent }, TEXTS);
  assertThreeParts(parts, {
    errorClass: "upstream_unavailable",
    what: "A service involved is not reachable right now.",
    todo: "Try again in a few minutes.",
    id: sent,
  });
});

test("right missing", async () => {
  const body = { detail: "role papaia_admin required" };
  const parts = describeFailure(await readFailure(response(403, body)), TEXTS);
  assertThreeParts(parts, {
    errorClass: "forbidden_or_not_found",
    what: "The content does not exist or is not shared with you.",
    todo: "Ask your contact person if you need access.",
  });
});

test("input invalid: the field list of a validation error is not shown", async () => {
  const body = { detail: [{ loc: ["body", "clean_up"], msg: "Input should be a valid boolean", type: "bool_parsing" }] };
  const parts = describeFailure(await readFailure(response(422, body)), TEXTS);
  assertThreeParts(parts, {
    errorClass: "invalid_input",
    what: "The details are incomplete or do not fit.",
    todo: "Check your details and send them again.",
  });
  assert.equal(parts.consequence, "Nothing was saved.");
});

test("input invalid: a plain sentence of the server says what did not fit", async () => {
  const sentence = "The add-on is still required by another add-on and cannot be removed.";
  const parts = describeFailure(await readFailure(response(400, { detail: sentence })), TEXTS);
  assertThreeParts(parts, {
    errorClass: "invalid_input",
    what: sentence,
    todo: "Check your details and send them again.",
  });
});

test("input invalid: a sentence with a key name, a code or markup falls back to the kit", async () => {
  for (const detail of [
    "field clean_up must be a boolean",
    "upstream answered 500",
    'Traceback (most recent call last): File "x.py", line 3',
    "<b>bad</b>",
    "PK-INPUT-001",
  ]) {
    const parts = describeFailure(await readFailure(response(422, { detail })), TEXTS);
    assert.equal(parts.what, "The details are incomplete or do not fit.", detail);
  }
});

test("session expired", async () => {
  const parts = describeFailure(await readFailure(response(401, { detail: "session expired" })), TEXTS);
  assertThreeParts(parts, {
    errorClass: "unauthenticated",
    what: "You are no longer signed in.",
    todo: "Sign in again.",
  });
  assert.equal(parts.consequence, "Nothing was carried out.");
});

test("the class of a kit envelope wins over the status code", async () => {
  const envelope = { class: "conflict", code: "PK-CONFLICT-001", correlation_id: ID };
  const failure = await readFailure(response(400, envelope));
  assert.equal(failureClass(failure), "conflict");
  assert.equal(failureClass({ status: 400, body: { class: "made_up" } }), "invalid_input");
});

test("an unknown status is an unexpected problem, never the code itself", async () => {
  for (const status of [418, 500, 501]) {
    const parts = describeFailure(await readFailure(response(status, undefined)), TEXTS);
    assert.equal(parts.errorClass, "internal_error");
    assert.equal(parts.what, "Something went wrong unexpectedly.");
    assert.ok(!parts.what.includes(String(status)));
  }
});

test("the reference is the id the server answered with, else the one that was sent", async () => {
  const answered = await readFailure(response(403, {}, "0190f3a2-0000-7000-8000-00000000000a"), "sent-id");
  assert.equal(answered.correlationId, "0190f3a2-0000-7000-8000-00000000000a");
  const silent = await readFailure(response(403, {}, null), "0190f3a2-0000-7000-8000-00000000000b");
  assert.equal(silent.correlationId, "0190f3a2-0000-7000-8000-00000000000b");
});

test("plain sentences", () => {
  assert.equal(isPlainSentence("The catalog name is already taken."), true);
  for (const text of [undefined, "", " ", 42, "x".repeat(301), "line\nbreak", "see /api/v1/x"]) {
    assert.equal(isPlainSentence(text), false, String(text));
  }
});
