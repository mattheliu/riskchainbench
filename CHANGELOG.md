# Changelog

## 2026.09.25-consolidated — public home and reviewed eval export

- Consolidate reviewed Task 2 paper resources in the existing Task 1 repository,
  renamed RiskChainBench; preserve old paper/evaluation paths and release tags.
- Export four self-contained historical evidence-research files from the private
  eval repository without importing its history or private data.
- Add web-only recomputation verified with private inputs (40 points, 80 CI
  endpoints within 1e-12); publish aggregate-only validation metadata.
- Add a strict-entry helper explicitly labeled as a protocol reconstruction,
  not the missing historical materializer.
- Add 21 tests (55 consolidated tests total) and public offline CI.
- Document the incomplete Judge runner, normalization difference, missing paired
  inputs and unresolved bootstrap seed provenance. No model calls or paper edits.

## 2026.09.23-evaluation-v1 — standalone offline subset

- Add `evaluation/`: byte-identical public scoring cores, portable CLI, invented
  smoke fixtures, explicit seeds, frozen-input hash guards and aggregate-only output.
- Add 12 offline tests; existing 22 paper-resource tests remain unchanged.
- Document HF Dataset Card updates and tiered disclosure boundaries.
- Private eval history, Gold and replay data remain excluded.

## 2026.09.23-paper-resources — reviewed partial release

- Promote reviewed `paper/` resources as the default entry point.
- Preserve the previous public root tree byte-for-byte under `legacy/`.
- Add scoped Apache-2.0 / CC BY 4.0 licensing, citations and acknowledgments.
- Preserve historical scores and explicitly document reproducibility gaps.
- Exclude private eval/Gold, raw trajectories, resolver maps and site assets.
- No new model evaluation or scientific paper revision.
