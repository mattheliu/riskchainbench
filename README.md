# RiskChainBench

[Paper](https://arxiv.org/abs/2609.16900) · [Task 1](task1/README.md) · [Task 2](task2/README.md) · [Evaluation](evaluation/README.md)

**Unified public home, updated 2026-09-25.** This repository consolidates the
reviewed Task 1, Task 2 and evaluation resources. Existing `paper/` and
`evaluation/` commands and historical release tags are preserved.

- [Task 1](task1/README.md): 3,600 model-visible inputs, prompt, runners and scoring.
- [Task 2](task2/README.md): frozen website analysis, aggregates and historical runtime references.
- [Offline evaluation](evaluation/README.md): one scoring entry point for both tasks.
- [Recovery analysis](reproduction/README.md): web-only recomputation plus a clearly labeled reconstructed strict-entry helper.
- [Historical evidence research](research/evidence_protocol_v02/README.md): reviewed validator/schema/prompt subset, **not the final paper Judge**.
- [Release boundaries and remaining gaps](docs/INTEGRATION_20260925.md).
- [Public Task 1 input dataset](https://huggingface.co/datasets/leonliuzx/riskchainbench-task1-paper-inputs): a clean, input-only HF mirror; [data distribution and archive policy](docs/DATA_DISTRIBUTION.md).

**Partial reproduction only.** Local web-only verification matched 40 point
estimates and 80 CI endpoints within 1e-12 using private inputs. The exact paired
input and historical model predictions remain missing. Gold, mappings, raw
trajectories and the private recovery archive are not distributed.
The original private eval history was not imported.

## Preserved Task 1 instructions

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
- `task1/`, `task2/`: unified task navigation and the imported Task 2 paper snapshot.
- `reproduction/`, `research/`: separately labeled recovery tools and historical research code.
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

For all consolidated tests, also install `pytest==8.3.4` and run:

```bash
python -m pytest paper/tests evaluation/tests task2/paper/tests tests -q
```

These are offline artifact/schema and synthetic scoring checks, not new model
evaluations or proof of full historical recomputation.

## Licensing, citation and acknowledgment

We thank [@Poor-Jack](https://github.com/Poor-Jack),
[@lele104](https://github.com/lele104),
[@Zzzzzz-y-s](https://github.com/Zzzzzz-y-s), and
[@zhengwj07-lgtm](https://github.com/zhengwj07-lgtm) for their contributions to
human annotation and related dataset preparation. See the
[dataset contributor acknowledgments](ACKNOWLEDGMENTS.md#dataset-annotation-contributors).

See [LICENSING.md](LICENSING.md) for exact scope. Project-owned reviewed code is
Apache-2.0; project-owned synthetic records and aggregate results are CC BY 4.0.
**These grants do not relicense legacy or third-party material.**
See [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md) and [CITATION.cff](CITATION.cff).

The separate evaluation repository and original HF archives remain private; the
new input-only HF mirror is public. The old Task 2 repository is archived, not deleted.
Access to restricted archives is not
automatic; ask via repository issues without posting private data or credentials.
