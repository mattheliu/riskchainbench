# RiskChainBench Task 1

Independent code and contracts for obfuscated-message reconstruction.

- Frozen source: `riskchainbench-balanced-600-v0.3`
- Contract:
  `28a2fe6bac429d902153f3d9f2b575b89439115a91e9cd882843d5e54e056240`
- Scale: 600 source sessions, six deterministic variants per source
- Main track: `v000`; only this variant may enter Task 2
- Robustness track: `v001` through `v005`
- Transport: `libinfer/libinfer-neo` only

The repository contains model-visible inputs, prompts, schemas, and runners.
Scoring and Task 2 handoff additionally require the authorized evaluator files
from one of the private dataset mirrors:

- ModelScope:
  <https://modelscope.cn/datasets/leonliuzx/riskchainbench-task1>
- Hugging Face:
  <https://huggingface.co/datasets/leonliuzx/riskchainbench-task1>

See the complete Chinese runbook:
[docs/evaluate_new_models_zh.md](docs/evaluate_new_models_zh.md).

The runner rejects a release unless the embedded and pinned contract hashes,
file hashes, counts, source benchmark ID, and libinfer-neo route probe all pass.
