# Release and access boundaries — 2026-09-23

| Layer | Available now | Not included / conditions |
| --- | --- | --- |
| Public Task 1 | 3,600 model-visible inputs, prompt, schemas, runners, scoring code, aggregate results | Matching evaluator and resolver withheld; historical CI seed discrepancy unresolved |
| Public Task 2 | Paired-bootstrap code and sanitized ten-model aggregates | Exact 6,000-row paired input not recovered; full portable sandbox/final judge export incomplete |
| Standalone evaluation | Public scoring cores, offline wrapper, invented fixtures, tests, source hashes | No real Gold, browser execution or evidence-judge service; not the entire private eval repository |
| HF Task 1 archive | Private; card updated with version and access boundaries | Mixed historical snapshots; verify hashes, not just row counts |
| HF Task 2 replay archive | Private; card added; listing approximately 7.04 GB | Containers/site content/evaluator materials require content, history and rights review before any wider release |
| Private eval repository | Remains private | No wholesale source-history export or visibility change |

## Documentation updates

- [Task 1 Dataset Card](https://huggingface.co/datasets/leonliuzx/riskchainbench-task1),
  documentation commit `4069d0603840fec665c28aee4b8e9650d245c421`.
- [Task 2 Dataset Card](https://huggingface.co/datasets/leonliuzx/riskchainbench-task2-controlled-web-replay),
  documentation commit `b59a7a9681f7fb3197fec2a9ba4b62aef2917202`.

These links require existing authorization. Cards do not grant access or relicense
archive content. HF metadata says `restricted-archive-mixed-rights`, not a blanket
CC BY license. This is a descriptive archive-rights label, not a new standardized
open license. Historical manifests are verified at pinned revisions, not against
a later edited README.

## Before releasing additional data

1. Select the exact paper-compatible version and match frozen file hashes.
2. Separate model-visible tasks, evaluation-only labels, resolver mappings,
   website content, container layers and raw trajectories.
3. Review both working-tree content and history for secrets, personal data,
   unlicensed third-party material and model-visible answer leakage.
4. For containers, inspect contents and startup/network behavior in isolation;
   a large archive or a successful download is not a validated portable release.
5. Export only an approved file allowlist, retain notices, publish checksums and
   tests, and define the exact access process for withheld evaluation data.

The present review is a bounded source/hash and synthetic-test audit of the
public subset, plus directory-level inventory and card updates for HF archives.
It is **not** a complete audit of 7.04 GB of replay data, every container layer,
all private history, or third-party redistribution rights.
