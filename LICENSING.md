# Licensing scope

Approved release scope: 2026-09-23. This is a mixed-license repository.

The standalone `evaluation/` distribution has its own scoped LICENSE, LICENSE-DATA,
NOTICE and README: project-owned code/schema/docs are Apache-2.0, and its invented
`examples/*.jsonl` smoke fixtures are CC BY 4.0. No private evaluator is licensed
or distributed by that package. The frozen `paper/` notices remain unchanged.

- **Apache-2.0:** project-owned Python code, schemas, prompts, and documentation in `paper/`, and newly authored repository-level documentation. See `LICENSE`.
- **CC BY 4.0:** project-owned synthetic Task 1 records in `paper/model_visible/tasks_3600.jsonl` (Task 1 repository only), aggregate result JSON in `paper/results/`, and project-owned provenance metadata. See `LICENSE-DATA`. Attribute RiskChainBench contributors and cite the paper; identify your modifications.
- **Excluded from these grants:** `legacy/`, third-party content, trademarks, original website assets, external model weights, and any rights the contributors do not own. Upstream dependencies remain under their own licenses. A paper citation is not a substitute for retaining license notices.
- License files and third-party notices retain their respective terms.

The project licenses permit commercial use within their scope. They do not grant
access to private evaluation data, promise third-party clearance, or override API
provider terms. Gold, evaluator-only labels, resolver maps and raw trajectories
are not included in the reviewed release.

## Legacy preservation

`legacy/` preserves the previous public tree without changing its file contents.
Some legacy configuration contains INTERNAL_RESEARCH_ONLY, RESEARCH_FIXTURE_ONLY
or RIGHTS_UNKNOWN notices. It is retained for provenance, **not newly relicensed
or certified for redistribution/reuse**. Follow the original notices; do not
assume the root LICENSE overrides them. The reviewed downloadable package
contains only `paper/`, not `legacy/`.

Unicode-derived legacy data is accompanied by the upstream notice in
`third_party/UNICODE-LICENSE.txt` in the full repository. Imported dependencies
are not vendored in the reviewed package. Unicode identifiers or platform names
appearing in synthetic examples do not convey ownership of the underlying marks.

## Attribution example

“Uses RiskChainBench reviewed paper resources (2026-09-23), by RiskChainBench
contributors, https://arxiv.org/abs/2609.16900. Project-owned synthetic data and
aggregates: CC BY 4.0, https://creativecommons.org/licenses/by/4.0/.
Changes: [describe your changes, or state none].”
