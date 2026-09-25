# Data distribution and archive policy

Status checked/updated: 2026-09-25. This page does not change published paper scores.

## Maintained public inputs

[HF Task 1 reviewed paper inputs](https://huggingface.co/datasets/leonliuzx/riskchainbench-task1-paper-inputs)
is a separate input-only export. Pin revision
`3f3744388625c9b62d1f5070753d0c2969d2665c` for this release.
It contains 3,600 inputs from 600 synthetic source sessions, six variants each,
the prompt, schema, provenance, licenses, citation, acknowledgments and verifier.
No input text or ordering was changed. This is not a new benchmark split.

Input SHA-256:
`a3c7248d06caa6f2d978284d87fecf6c080f963b6a30473da338ba459e65b644`.
Prompt SHA-256:
`2ece83426010b6c5d6abc4ef005011f6780a60a42b467009a2cc4f753269373c`.
Download the complete file set and run `python3 verify.py`.
The upload and public visibility were verified in the HF interface. Independent
anonymous byte-for-byte download verification was attempted but blocked by a
local network connection reset; it is not claimed complete.
HF currently renders a row preview but reports `UnexpectedError` / incoherent
dataset size for the full viewer. Full viewer readiness is not claimed verified;
the frozen input bytes were not rewritten to work around this service error.

The 12-file export was locally verified (11 manifest entries plus the manifest).
Six additional packaging tests cover the valid export and rejection of extra,
missing, changed, duplicate-manifest and traversal-path cases.
These checks do not establish full historical model/browser reproduction.

## Historical archives

| Location | Status | Purpose |
| --- | --- | --- |
| HF `leonliuzx/riskchainbench-task1` | Private | Mixed-version historical archive; not the public input mirror |
| HF `leonliuzx/riskchainbench-task2-controlled-web-replay` | Private | Restricted replay/runtime/evaluator archive |
| ModelScope `leonliuzx/riskchainbench-task1` | Application-gated | Historical archive; clean mirror and card alignment pending |
| ModelScope `leonliuzx/riskchainbench-task2-controlled-web-replay` | Application-gated | Replay archive; card and file-level audit pending |

Matching names or displayed sizes do not prove cross-platform file equivalence.
The two existing HF archive cards now link to this maintained repository and
credit the four annotation contributors. Archive visibility remains unchanged.
Original archive manifests must be checked at their original revisions, not
against a later documentation-only update.

The old [Task 2 code repository](https://github.com/mattheliu/riskchainbench-task2)
is archived, not deleted. Its historical code and Release remain available.
The private eval repository is retained; its private history is not merged here.

## Further data processing

- Preserve the frozen sources; create explicit allowlisted exports instead of
  flipping whole private archives to public.
- Preserve task IDs, Unicode, row ordering and source groups. Do not deduplicate
  legitimate variants or rewrite obfuscations in the paper snapshot.
- Gold release requires an explicit development/held-out-test policy and a
  field-level review. Group all six variants from a source together in any future
  split; label a new split as a new version, not the original experiment.
- Inspect Task 2 files, container layers, build history, environment variables,
  logs, screenshots, mounts and network settings before a wider export. Review
  privacy, credentials and third-party redistribution rights; do not execute
  unreviewed containers during this audit.
- Keep evaluator labels, private resolver/site bindings and source evidence out
  of model-visible material. Application-gated access is not a redistribution grant.
- Do not infer the missing 6,000-row paired input or historical predictions from
  aggregate scores. Full runtime/final Judge closure remains incomplete.

See [integration limitations](INTEGRATION_20260925.md) and [licensing](../LICENSING.md).
