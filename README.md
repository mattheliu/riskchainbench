# RiskChainBench — Task 1

[Paper](https://arxiv.org/abs/2609.16900) · [Task 1](https://github.com/mattheliu/riskchainbench-task1) · [Task 2](https://github.com/mattheliu/riskchainbench-task2)

**Start with [paper/README.md](paper/README.md).** This repository provides reviewed,
partial paper resources; it does not yet provide a complete public reproduction
of the benchmark. The 2026-09-23 update packages recovered artifacts, without
rerunning models or changing published scores.

**Standalone evaluation:** [evaluation/README.md](evaluation/README.md) combines
the public Task 1 scorer and Task 2 paired-analysis core, with an offline CLI,
invented demo fixtures and tests. It requires no private data for smoke tests;
paper scoring still requires separately authorized, hash-matched private inputs.
See [release boundaries](docs/RELEASE_BOUNDARIES.md) for what is public or withheld.

The reviewed package contains 3,600 model-visible token-text inputs (600 source
sessions × six variants), the matching prompt, HTTP runners, scoring code,
schemas, synthetic tests and ten-model archived aggregate results.

The matching evaluator/Gold is withheld. Archived Task 1 confidence intervals
record seed 20260726 while the supplement describes 20260727 plus model offsets.
See [known gaps](paper/KNOWN_GAPS.md); we preserve this discrepancy explicitly.

## Layout

- `paper/`: reviewed release entry point, source provenance, checksums and tests.
- `evaluation/`: independently runnable scoring/analysis subset for both tasks.
- `legacy/`: previous public root tree, preserved byte-for-byte for compatibility
  and audit. Run legacy commands from that directory. Its input versions and
  licensing notices must not be confused with `paper/`.
- `third_party/`: upstream notices accompanying legacy derived material.

The pre-cleanup tree is preserved in Git history at
`65d58feffab29bd1c78b7b2dfa1bab8b97bd0d6f`.
Old repository-root paths have moved to `legacy/`; users relying on them should
pin the old commit or update their paths. No legacy file contents were edited.

## Tests

Install `paper/requirements.txt`, then install `pytest==8.3.4`.

```bash
python -m pytest paper/tests -q
python paper/verify_release.py
```

These are offline artifact/schema and synthetic scoring checks, not new model
evaluations or proof of full historical recomputation.

## Licensing, citation and acknowledgment

See [LICENSING.md](LICENSING.md) for exact scope. Project-owned reviewed code is
Apache-2.0; project-owned synthetic records and aggregate results are CC BY 4.0.
**These grants do not relicense legacy or third-party material.**
See [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md) and [CITATION.cff](CITATION.cff).

The separate evaluation repository and HF archives remain private. Access is not
automatic; ask via repository issues without posting private data or credentials.
