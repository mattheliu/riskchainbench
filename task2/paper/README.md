# RiskChainBench: recovered Task 2 / entry-gated analysis

[Paper](https://arxiv.org/abs/2609.16900) · [Task 1](https://github.com/mattheliu/riskchainbench-task1)

This **reviewed partial release** restores the paper engineering package's paired
website-bootstrap script and sanitized ten-model aggregates. It is not a new
evaluation and does not contain the complete resettable sandbox, final evidence
judge package, Gold, or private resolver. See `LICENSING.md` for the approved,
limited Apache-2.0 / CC BY 4.0 scope and third-party exclusions.

## Correct evaluation interpretation

- Each underlying model investigates each correctly associated website once.
- The browser runner does not consume Task 1 message semantics or predictions.
- Web-only task correctness uses human website labels, with all 600 sites per model.
- Entry-gated results reuse the same frozen website result and apply the primary
  Task 1 entry prediction offline. There is no second browser run and no Gold repair.
- Missing/invalid decisions remain failures; they are not removed to inflate accuracy.
- Decision Macro-F1 uses three fixed labels. Type Macro-F1 uses the six supported
  types within Gold-violation cases. These are not evidence-judge scores.
- Confidence intervals use 2,000 paired website-level bootstrap draws, base seed
  20260727, and the exact per-model seeds preserved in the aggregate JSON.

## Available files

- `results/web_and_gated_aggregates.json`: all recovered point estimates and CIs,
  with paper-facing model names and per-model seeds; no case-level labels.
- `bootstrap_task2_and_gated.py`: byte-identical to the recovered paper analysis script.
- `PROVENANCE.json`: source-summary hash, code hash, transformations, and limitations.
- `tests/test_bootstrap.py`: synthetic tests of denominator, gating, class averaging,
  paired resampling, and the recovered aggregate structure. No model calls.

The full repository's `legacy/scripts/compose_task1_task2_end_to_end.py` remains
available. This release does not silently replace its input contract or claim
that legacy browser settings exactly reproduce all final paper runs.

## Recompute the archived analysis (authorized input required)

```bash
python3 paper/bootstrap_task2_and_gated.py \
  --case-level /path/to/authorized/paired_case_level.jsonl \
  --out-dir ../private-analysis-output --replicates 2000 --seed 20260727
```

This archival command requires exactly 6,000 rows (ten models × 600 websites)
with input SHA-256
`da6bbdc314758baaa80dc5279a1adf65b4e8ab3d0b50e55a1e419a323ba2b7e0`.
The script deliberately refuses a different input. The exact input has **not**
been recovered in this packaging check, so full recomputation is unverified.
Do not reconstruct its private labels or entry gates from aggregate statistics.

The JSONL contract is shown by the script's `validate_and_group`, `score_rows`,
and `summarize_model` functions. New experiment data must receive a separately
identified analysis/version, not be substituted under the historical hash.

## Access and release boundaries

The [HF website replay archive](https://huggingface.co/datasets/leonliuzx/riskchainbench-task2-controlled-web-replay)
is private; it is not currently a public or self-service gated release.
[Repository issues](https://github.com/mattheliu/riskchainbench-task2/issues) can
be used to ask about availability, but access is neither automatic nor promised.
Do not post private labels, raw trajectories, original-site mappings, or credentials.

The whole HF archive must not be made public without a content/history and
redistribution review. Synthetic identities and blocked networking do not by
themselves establish rights to redistribute third-party website content.

## Known gaps

- Exact paired case-level input and original predictions are not bundled/recovered.
- Final frozen judge prompts/configuration, approved redacted cases and a portable
  600-site sandbox still require a separate verified export.
- Historical Task 2 ZIPs contain differing denominator policies; they are not
  interchangeable. Use the all-600-site paper aggregates here, not an answered-only
  table or a table excluding pending infrastructure cases.
- Task 1 has a separate bootstrap-seed provenance discrepancy; see its release's
  `KNOWN_GAPS.md`. Task 2's recovered summary records 20260727 plus offsets.
- This release is not a claim that the complete benchmark is openly reproducible.
