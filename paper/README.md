# RiskChainBench: recovered Task 1 paper resources

[Paper](https://arxiv.org/abs/2609.16900) · [Task 2](https://github.com/mattheliu/riskchainbench-task2)

This directory restores the six-variant input snapshot used by all ten completed
Task 1 runs in the archived July 27 result package. It is a packaging update, not
a new benchmark or a new model evaluation. **This is a reviewed partial release;
the confidence-interval provenance gap remains open.** See `LICENSING.md` for
the approved, limited Apache-2.0 / CC BY 4.0 scope.

## What is verified

- 3,600 unique token-text inputs, 600 synthetic source sessions, six variants per source.
- All ten archived model audits identify these exact inputs and this exact prompt.
- Archived settings: 8,000 output tokens, 300-second timeout, at most three attempts.
- The sequential HTTP harness and scorer are byte-identical to the recovered source.
- The concurrent wrapper only changes module loading: its adjacent harness is mandatory.
- Prediction schema placement fixes the scorer's previously broken default path.

The six variants are Primary (v000), Phonetic (v001), Entry encoding (v002),
Lexical (v003), Few-line (v004), and Vertical (v005). All 3,600 rows enter Task 1
scores; v000 alone provides the preselected entry gate for offline Task 2 analysis.
The older `legacy/model_visible/task1_inputs.jsonl` in the full repository is a different snapshot.
Never mix its predictions, gold, or hashes with this directory.

## Install and verify

```bash
python3 -m venv .venv
.venv/bin/pip install -r paper/requirements.txt
.venv/bin/python paper/verify_release.py
```

## Run a model

Use an authorized OpenAI-compatible chat-completions provider. Set
`LIBINFER_NEO_URL` to the provider's base URL and `LIBINFER_SK` to your own key
outside the repository. These legacy environment-variable names are retained;
no account, service endpoint, or credential is bundled. Use the exact model ID
available from your provider; historical model availability is not guaranteed.

```bash
.venv/bin/python paper/run.py --model YOUR_MODEL_ID --run-id my-evaluation \
  --output-dir ../private-runs/my-evaluation --dry-run
# Remove --dry-run only when you intend to make paid provider requests.
```

The concurrent runner validates resume metadata and rejects mismatched datasets,
prompts, models, or request options. Do not upload raw audit files or predictions
without a separate disclosure review. Put run directories outside `paper/`.

## Score with authorized evaluation-only data

The private evaluator is not bundled. Its required SHA-256 appears in
`PROVENANCE.json`; an old Task 1 evaluator is not interchangeable.

```bash
.venv/bin/python paper/score.py --evaluator /path/to/authorized/evaluator_3600.jsonl \
  --predictions ../private-runs/my-evaluation/predictions.jsonl \
  --output ../private-runs/my-evaluation/scores.json --bootstrap-seed 20260726
```

This seed reproduces the **archived Task 1 convention**, not the different seed
statement in the published supplement. See [KNOWN_GAPS.md](KNOWN_GAPS.md).
Model-output failures stay in the denominator. Test fixtures are synthetic and
do not substitute for benchmark Gold or reproduce historical model outputs.

## Resources and access

- `results/task1_archived_aggregates.json`: ten-model aggregate scores and archived CIs.
- `PROVENANCE.json`: per-model input/prompt/evaluator hashes and bounded verification claims.
- [Private HF archive](https://huggingface.co/datasets/leonliuzx/riskchainbench-task1):
  currently private, not a public or self-service gated dataset. Its root snapshot
  must not be assumed to match this package.
- [GitHub issues](https://github.com/mattheliu/riskchainbench-task1/issues): ask about
  evaluator availability without attaching private material. Access is not automatic
  or guaranteed; no public scoring service is currently provided.

Gold, resolver mappings, source sessions, raw model responses, hidden reasoning,
review queues and credentials are excluded. No original website assets or model
weights are redistributed here. See `ACKNOWLEDGMENTS.md`, `CITATION.cff`, and
`LICENSING.md` for attribution, citation, and rights exclusions.
