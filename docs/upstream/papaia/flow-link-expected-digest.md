# Proposal for the core: approve a Flow link only as it was shown

Status: proposed, not accepted. Base: the core's branch `feat/flow-approval-at-install`
(`tools/lib/flow_link.py`, `tools/lib/cli_addon.py`, `tools/lib/cli.py`,
`tools/lib/sh/addon.sh`). Patch: [`flow-link-expected-digest.patch`](flow-link-expected-digest.patch).

## Problem

The manager's install dialog shows the `flow:` section of the add-on's catalog manifest and
offers to approve it. With the box ticked, `papaia-ctl addon install --approve-flow-link` asks
Flow to approve `declaration_digest` of whatever Flow read at that moment. Nothing ties that to
what the administrator saw: a catalog changed between the dialog and the job, a second request
with `approve_flow_link: true` and no dialog at all, or a dialog that cut a long list all end in
an approval of a declaration nobody looked at.

## Change

1. `flow_link.section_digest(manifest)`: `sha256:<hex>` of the manifest's `flow:` section as
   canonical JSON (`sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=False`, UTF-8).
   papaia-manager computes it the same way (`app.core.flow_link.section_digest`).
2. `addon install` takes `--expect-flow-digest=sha256:HEX` and `--expect-addon-version=VER`.
3. `flow_link.link_at_install(..., expect_digest=, expect_version=)`: with an approval and
   either value given, the approval is refused unless both equal the digest and the version of
   the manifest being installed. The refusal prints
   `WARNING: Flow link of <name> not approved: the declaration differs from the one that was
   shown for approval.` (read by the manager as `error`), and the install itself goes on.
   Without the two flags the behaviour is unchanged (a person at the terminal sees Flow's own
   summary before answering).

## What the manager does meanwhile

- The dialog sends the digest and version it showed; the manager compares them with the
  catalog manifest it installs and refuses (409) an approval without them, with other values, or
  from a dialog that did not show every entry.
- It passes both flags with `--approve-flow-link`, and it offers and accepts an approval only
  from a core whose installer names both flags (`flow_link.core_binds_approval`). Until this
  proposal is accepted no core does, so the manager approves no Flow link at installation;
  the link stays a proposal to be approved in Flow.

## Checks

The patch was applied to a copy of the four files, not to the core. Against that copy:
`PAPAIA_SRC=<copy> uv run pytest -q -k test_flow_link_contract` (from `src/`) passed
9 tests, including `test_the_manager_binds_an_approval_only_to_a_core_that_checks_the_digest`,
which compares the two digests; `link_at_install` approved with matching values and returned
`error` for another digest or version; `bash -n` and `shellcheck --severity=style` passed on
the changed `addon.sh`. The core's own tests were not run with the patch.
