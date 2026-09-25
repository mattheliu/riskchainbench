# Consolidation and disclosure boundary — 2026-09-25

## One public entry point, not a wholesale private-repository release

`mattheliu/riskchainbench-task1` is renamed to `mattheliu/riskchainbench`.
Its history, existing `paper/`, `evaluation/` and release tags remain.
The Task 2 reviewed `paper/` snapshot is imported byte-for-byte into
`task2/paper/`. The separate Task 2 repository keeps its historical code/releases
and points to this maintained home. No old-name repository should be recreated.

The private eval repository remains a development archive. Reviewed source is
exported by file allowlist, never by merging private Git history.

## Newly released

- A parameterized web-only recomputation entry point and aggregate validation summary.
- A new strict-entry helper, explicitly marked protocol reconstruction, with synthetic tests.
- Four self-contained historical research files: deterministic evidence validator,
  two schemas and a semantic-audit prompt. These are not represented as the final
  paper Judge.
- Source hashes, provenance, integration checks and public tests/CI.

## Deliberately not released

- `evaluator_3600.jsonl`, source sessions, resolver aliases and website bindings.
- Raw scoring archives containing case-level Gold and predictions.
- Private trajectories, original websites/screenshots and the private recovery ZIP.
- Provider credentials/endpoints, internal run directories and private Git history.
- Broken Judge runner: missing dependency modules and original case fixtures.
- The full browser runtime: existing historical code is linked, but dependency,
  asset and final-version closure are not established.

## Reproducibility statement

Task 1 model-visible inputs, Gold and bindings were locally recovered and hash
verified. Gold and bindings are still private. Local web-only recomputation
matched 40 point estimates and 80 CI endpoints within 1e-12; the public report
omits private case rows, mappings, local paths and runtime identifiers.

The exact paired input hash
`da6bbdc314758baaa80dc5279a1adf65b4e8ab3d0b50e55a1e419a323ba2b7e0`
is recorded but its file remains missing. Historical model predictions and the
original gate materializer were not recovered. The frozen scorer normalizes
some URL variants whereas the supplement specifies strict equality; the new
helper follows the supplement without changing old scores or claiming equivalence.
Task 1 bootstrap seed provenance remains unresolved (archive 20260726 versus
supplement 20260727 plus offsets).

Public smoke tests do not prove full historical reproduction. New provider runs
would produce new experimental results, not restore missing history.

## Data distribution

A clean public HF mirror may contain only already-approved model-visible inputs
and a Dataset Card with source hashes and license scope. Do not flip an existing
private archive to public: its files and history need a separate review.
Gold publication needs an explicit split/access policy and field-level review;
Gold is not categorically unpublishable, but this release does not include it.
