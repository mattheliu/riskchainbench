# Historical evidence-protocol research subset

**Research prototype, not the final paper Judge and not human Gold.**

Four project-owned source files were selectively exported from the private
`riskchainbench-eval` commit `794ca62f9b22be87dd0f550f79276225c11c4f25`:
a deterministic validator, two schemas, and a semantic-audit prompt. Their bytes
are preserved; hashes and paths are in `../../docs/INTEGRATION_PROVENANCE.json`.
No private repository history, cases, aliases, original domains, labels,
provider settings, trajectories or credentials are included.

The source documentation describes v0.2 as a July 15 shadow/research protocol.
Its pass/fail indicates structural/methodological admissibility, not correctness
of a website risk label. This export does not establish that the final paper
used this exact validator or prompt.

## Run the deterministic validator

Install `jsonschema==4.23.0`, then supply your own authorized evidence case:

```bash
python research/evidence_protocol_v02/scripts/evidence_protocol_v02.py \
  --case /path/to/authorized-case.json --artifact-root /path/to/artifacts --strict
```

The command prints its report without overwriting a file. Artifact verification
requires `--artifact-root`; omitting it checks structure only. This tool does not
make network/model calls. Treat reports derived from private cases as private.

The prompt and output schema are reference material, not an executable LLM
pipeline. The private runner references missing `validate_llm_judge_v02.py`
and `model_request_boundary.py`; its tests also depend on unavailable fixtures.
That incomplete runner and those tests were not exported.

New public tests verify schema validity, rejection of empty cases, cycle
detection and artifact path/hash handling with invented data. They do not replace
the unavailable original positive fixture or prove paper-scale validation.

## Rights and attribution

These four project-owned code/schema/prompt files and this new documentation are
released under the repository's Apache-2.0 license in this reviewed export.
Copyright RiskChainBench contributors. Historical CovertEntryWalker names and
schema identifiers are retained for provenance; renaming them would change the
data contract. No license grant extends to user-supplied evidence or private data.
