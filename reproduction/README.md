# Recovered web-only analysis and a reconstructed gate helper

These are separate from the immutable historical analysis core in `evaluation/`.
No private inputs are distributed here.

## Web-only numerical recomputation

With separately authorized, hash-matched inputs:

```bash
python reproduction/web_only.py --scored-zip /path/to/Final_Scores_20260727.zip \
  --ordinal-map /path/to/offline_handoff_map_600.jsonl --output /path/to/new-web-report.json
```

The script independently scores the frozen per-case table (all 600 websites per
model), orders rows by the original ordinal map, and performs 2,000 website
resamples with seed 20260727 plus sorted model index. It selects the ten models
in the published aggregate snapshot, not the archive's extra unranked model.
The public reference contains aggregates, not private predictions or gates.

Local recovery verified 40 point estimates and 80 CI endpoints within 1e-12.
This is a local validation claim using private inputs, not a public end-to-end
reproduction or a fresh browser/model run. See `../results/recovery_web_only_20260925.json`.
Outputs are aggregate-only; local input paths are not exported.

## Reconstructed entry gate (not historical code)

`entry_gate.py` implements a small, newly written strict-comparison helper.
Its synthetic tests cover lowercase canonical entries, no URL normalization,
rank-1-only behavior, malformed candidates and bottom propagation.

The caller must select the predeclared v000 sample and supply its authorized
canonical alias. The helper does not discover aliases, infer missing predictions,
reconstruct original 6,000-row inputs or modify the frozen bootstrap hash guard.
It is not wired into historical paper scoring. The original Task 1 scorer's
normalization differs from the supplement; this reconstruction explicitly uses
the supplement's strict policy and does not claim to resolve historical execution.

Any future combination of new restoration predictions and historical web results
must be labeled as a new, mixed-vintage offline composition experiment.
