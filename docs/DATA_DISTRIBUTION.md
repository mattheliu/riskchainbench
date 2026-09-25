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
The initially observed HF `UnexpectedError` / incoherent-size message cleared
on a later refresh on 2026-09-25. The full viewer now displays `test · 3.6k rows`
and pagination. No frozen input bytes were changed to resolve that platform error.
This browser check is distinct from anonymous byte-for-byte download validation.

The 12-file export was locally verified (11 manifest entries plus the manifest).
Six additional packaging tests cover the valid export and rejection of extra,
missing, changed, duplicate-manifest and traversal-path cases.
These checks do not establish full historical model/browser reproduction.

## Historical archives

| Location | Status | Purpose |
| --- | --- | --- |
| HF `leonliuzx/riskchainbench-task1` | Private | Mixed-version historical archive; not the public input mirror |
| HF `leonliuzx/riskchainbench-task2-controlled-web-replay` | Private | Restricted replay/runtime/evaluator archive |
| ModelScope `leonliuzx/riskchainbench-task1` | Application-gated | Card updated; platform moderation reverted historical files; not a complete mirror |
| ModelScope `leonliuzx/riskchainbench-task2-controlled-web-replay` | Application-gated | Missing card added; file-level/container audit still pending |

Matching names or displayed sizes do not prove cross-platform file equivalence.
The two existing HF archive cards and both ModelScope cards now link to this
maintained repository and credit the four annotation contributors. Archive
visibility remains unchanged. ModelScope Task 1 metadata now says `other` to
clarify its mixed rights, not to revoke any prior valid license grant.
Original archive manifests must be checked at their original revisions, not
against a later documentation-only update.

The old [Task 2 code repository](https://github.com/mattheliu/riskchainbench-task2)
is archived, not deleted. Its historical code and Release remain available.
The private eval repository is retained; its private history is not merged here.

## ModelScope moderation and mirror limitation

The Task 1 commit history records three Administrator reversions dated
2026-07-28, described by the platform as sensitive file content or message:

- `model_visible/task1_inputs.jsonl` at `e7d85843e7dd118d7be0359e90a9d44a1947ead3`.
- `releases/task1-six-variant-token-text-v0.3/evaluator_only/source_sessions_600.jsonl`
  at `fe3f276f3fc6d390b1d425fdb759470dac754de3`.
- `releases/task1-six-variant-token-text-v0.3/spec/generation_base_v0.2.json`
  at `daa7eb4c2dfbff88f853916e8266b03f3b5e3635`.

[Platform history](https://modelscope.cn/datasets/leonliuzx/riskchainbench-task1/commitList)
is the source for these actions; the exact triggering records have not been
established. This does not prove that every Task 1 input version was affected.
Do not describe the current ModelScope archive as an intact paper mirror.
No reverted file was restored, renamed or re-uploaded in this documentation pass.
The proposed clean ModelScope mirror is deferred pending clarification through
the platform's normal content-review process, not recreated under another name.

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
