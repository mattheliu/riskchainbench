# Known release and reproducibility gaps

1. **Bootstrap seed discrepancy:** all ten recovered Task 1 score summaries record
   `seed=20260726`. The arXiv v2 supplement states a base seed of `20260727` with
   model-specific offsets for Task 1 as well as Task 2. The archived intervals are
   preserved, not relabeled. Resolving this requires original per-case Task 1
   results or a later authenticated recomputation artifact; neither is in the
   recovered result archive. No paper edit or new confidence interval is implied.
2. **Historical scorer identity:** the bundled scorer matches the recovered code
   snapshot and the already-visible legacy scorer, but per-model audit summaries
   do not record the executed scorer's file hash. This does not prove bit-identical
   historical execution solely from a matching source copy.
3. **Restricted evaluation:** the matching evaluator and private offline resolver
   remain withheld. Public inputs/code alone do not enable complete local scoring
   or full end-to-end reproduction. Dataset mirrors are not public access grants.
4. **Licensing scope:** the owner approved Apache-2.0 for project-owned reviewed
   code and CC BY 4.0 for project-owned synthetic data and aggregates. Legacy and
   third-party material do not inherit these grants. See `LICENSING.md`.
5. **Scope:** the separate 600-input epsilon extension is not part of the 3,600-row
   main set and is not bundled. No raw trajectories, human audit rows, or website
   snapshots are part of this reviewed package.

The scientific inputs, historical scores, and publication text have not been
altered to hide or resolve these gaps.
