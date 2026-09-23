# RiskChainBench standalone offline evaluation package

This code-only evaluation distribution reuses the already-public Task 1 scorer
and Task 2 paired-bootstrap core **byte-for-byte**. It adds a portable entry point,
synthetic smoke examples, tests and a complete file/hash manifest. It does not
make the private `riskchainbench-eval` repository or its history public.

## Install and smoke-test without private data

From this directory, using Python 3.10 or newer:

```bash
python -m pip install -r requirements.txt
python verify_package.py
python evaluate.py task1 --mode demo --seed 7 --replicates 50 --output ../demo-task1.json
python evaluate.py task2 --mode demo --seed 7 --replicates 50 --output ../demo-task2.json
python -m pip install pytest==8.3.4
python -m pytest tests -q
```

The examples are invented fixtures, not benchmark Gold, leaked cases or paper
results. No model API, credentials, private repository checkout, live URL lookup
or website sandbox is needed. Outputs contain aggregates, not per-case answers.
Existing outputs are not overwritten; choose a fresh output path for a rerun.

## Evaluate against separately authorized paper inputs

```bash
python evaluate.py task1 --mode paper --dataset /path/to/evaluator_3600.jsonl \
  --predictions /path/to/model_predictions.jsonl --seed 20260726 \
  --output ../private-results/task1.json
python evaluate.py task2 --mode paper --dataset /path/to/paired_case_level.jsonl \
  --seed 20260727 --output ../private-results/task2.json
```

The wrapper rejects incompatible dataset hashes. Task 1 uses all 3,600 inputs;
missing predictions fail validation rather than reducing the denominator.
Task 2 requires the original paired 6,000 rows (10 models × 600 websites), keeps
three fixed decision labels and six supported violation types, and pairs web and
entry-gated outcomes in each website bootstrap draw. It is historical paired
analysis, not a general-purpose browser runner or evidence judge.

Seeds are mandatory. Default bootstrap replicates: 2,000. Task 2 assigns offsets
in sorted model order, exactly as the original analysis does. For alternative
experiments use separately versioned inputs and report the changed protocol;
do not bypass a frozen hash and call the result an original paper reproduction.

## What remains unavailable

- Matching Task 1 evaluator/Gold is private. Scoring code alone cannot replace it.
- Task 1 archived CIs use seed 20260726; the supplement states 20260727 plus model
  offsets. That discrepancy remains unresolved. Original model predictions and
  the executed historical scorer hash are not fully evidenced by the archive.
- Exact Task 2 paired input SHA-256 is recorded but its file was not recovered.
- Full resettable 600-site environment, final evidence-judge assets, private
  resolver, original site assets, raw trajectories and credentials are excluded.
- This package does not newly export all private eval protocols. It is the
  independently runnable, reviewed scoring/analysis subset.

## Provenance, citation and rights

See `PROVENANCE.json` for copied file hashes and source commits and
`MANIFEST.sha256` for the full package allowlist. Pin this package's version when
reporting results. Local synthetic tests do not prove full benchmark reproduction.

[Paper](https://arxiv.org/abs/2609.16900) and `CITATION.cff` provide citation metadata.
Code, schema and documentation: Apache-2.0 (`LICENSE`). Designated invented JSONL
fixtures in `examples/`: CC BY 4.0 (`LICENSE-DATA`), RiskChainBench contributors.
These grants do not cover third-party dependencies or any private input supplied
by a user. `NOTICE` records acknowledgments and exclusions.
