#!/usr/bin/env python3
"""Recompute Task 2 web-only metrics from original scored rows; never invent gates."""
import collections
import argparse
import hashlib
import importlib.util
import json
import random
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCHIVED_SCORES = 'task2_final_results/task2_case_level_scored.jsonl'
SCRIPT = ROOT / 'evaluation/task2/bootstrap_task2_and_gated.py'
REFERENCE = ROOT / 'task2/paper/results/web_and_gated_aggregates.json'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    if sys.flags.optimize:
        raise SystemExit('Run without -O: assertions are part of validation')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scored-zip', type=Path, required=True, help='Separately authorized original Final_Scores ZIP')
    parser.add_argument('--ordinal-map', type=Path, required=True, help='Separately authorized private ordinal mapping')
    parser.add_argument('--output', type=Path, required=True, help='New aggregate-only output path')
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing to overwrite output')
    ARCHIVE, MAPPING = args.scored_zip, args.ordinal_map
    started = time.monotonic()
    assert sha(SCRIPT.read_bytes()) == 'aeb87f842d5f5d162c45b0f379643fa2e85504c2b979400dff77042c60797312'
    assert sha(REFERENCE.read_bytes()) == '8b1140f4423a3ff0be2e676f8dd35708d806d5804886cf8a0373a7004e4b1c8c'
    spec = importlib.util.spec_from_file_location('original_analysis', SCRIPT)
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    reference = json.loads(REFERENCE.read_text())
    with zipfile.ZipFile(ARCHIVE) as z:
        manifest_count = 0
        for line in z.read('PACKAGE_MANIFEST.sha256').decode().splitlines():
            expected, name = line.split(maxsplit=1)
            assert sha(z.read(name.removeprefix('./'))) == expected, name
            manifest_count += 1
        data = z.read(ARCHIVED_SCORES)
        assert sha(data) == 'd07e7f79427088796fbe6c0d337ebc9f3ac3d482f5c034b6b2e0421447ded5ae'
        assert sha(z.read('task2_final_results/task2_final_metrics.json')) == '843e19653fb05697e94acbd34c95f55fa1bc23ad081b71c049881da2f4096001'
        rows = [json.loads(line) for line in data.splitlines() if line.strip()]
    mapping_rows = [json.loads(l) for l in MAPPING.read_text().splitlines() if l.strip()]
    mapping = {r['case_ref']: r['ordinal'] for r in mapping_rows}
    assert len(mapping_rows) == len(mapping) == len(set(mapping.values())) == 600
    assert sha(MAPPING.read_bytes()) == 'ac1fd62e0948d4006035a7ea88521245dc252533591ed3febb7fc75ddb6d7e7c'
    assert len(rows) == 6600
    groups = collections.defaultdict(list)
    for row in rows:
        groups[row['model']].append(row)
    assert len(groups) == 11
    # The already-public reference uses display names instead of provider IDs.
    published = {row['model']: row for row in reference['models']}
    selected = {}
    for model, group in groups.items():
        names = {row['display_model'] for row in group}
        assert len(names) == 1
        name = next(iter(names))
        if name in published:
            selected[model] = {**published[name], 'display_model': name}
    assert len(selected) == len(published) == 10
    reference = {**reference, 'models': selected, 'model_order': sorted(selected)}
    labels = tuple(sorted({r['gold_type'] for r in rows if r['gold_decision'] == 'VIOLATION'}))
    assert len(labels) == 6
    assert list(labels) == reference['bootstrap']['fixed_violation_type_labels']
    assert reference['model_order'] == sorted(reference['model_order'])
    # Demonstrate that the actual archived table is not the exact paired input.
    with tempfile.TemporaryDirectory(prefix='riskchainbench-input-check-') as tmp:
        candidate = Path(tmp) / 'archived_scored_rows.jsonl'
        candidate.write_bytes(data)
        attempt = subprocess.run([sys.executable, str(SCRIPT), '--case-level', str(candidate),
                                  '--out-dir', str(Path(tmp) / 'should-not-be-created')],
                                 capture_output=True, text=True)
        assert attempt.returncode != 0 and 'case-level input hash mismatch' in attempt.stderr
        assert not (Path(tmp) / 'should-not-be-created').exists()
    report = {
        'scope': 'independent web-only recomputation from frozen scored rows, not full paired reproduction',
        'sources': {'archive_sha256': sha(ARCHIVE.read_bytes()),
                    'scored_member': ARCHIVED_SCORES, 'scored_sha256': sha(data),
                    'reference_sha256': sha(REFERENCE.read_bytes()), 'script_sha256': sha(SCRIPT.read_bytes()),
                    'ordinal_mapping_sha256': sha(MAPPING.read_bytes())},
        'archive_manifest_files_verified': manifest_count,
        'exact_paired_input_sha256': core.EXPECTED_CASE_LEVEL_SHA256,
        'exact_paired_input_recovered': False, 'original_cli_rejects_unmatched_table': True,
        'original_archive_rows': 6600, 'selected_models': 10, 'selected_rows': 6000,
        'excluded_model_reason': 'Gemini 3.6 Flash is absent from the frozen ten-model paper summary',
        'bootstrap': {'replicates': 2000, 'base_seed': 20260727, 'unit': 'website',
                      'ordering': 'ascending ordinal from independently preserved private handoff map'},
        'models': [], 'network_or_model_calls': 0,
        'runtime': {'python': sys.version},
        'comparison_absolute_tolerance': 1e-12,
        'not_reproduced': ['entry_gate_pass', 'gated_decision_accuracy', 'gated_decision_macro_f1',
                           'gated_violation_type_macro_f1', 'gated_hierarchical_exact_match', 'gate_loss'],
        'privacy': 'aggregate output only; no case references, Gold rows, mappings or predictions exported',
    }
    gold_reference = None
    point_matches = ci_endpoint_matches = 0
    for index, model in enumerate(reference['model_order']):
        group = groups[model]
        assert len(group) == len({r['case_ref'] for r in group}) == 600
        assert {r['case_ref'] for r in group} == set(mapping)
        group.sort(key=lambda r: mapping[r['case_ref']])
        gold = [(r['gold_decision'], r['gold_type']) for r in group]
        if gold_reference is None:
            gold_reference = gold
        assert gold == gold_reference
        assert sum(r['gold_decision'] == 'VIOLATION' for r in group) == 394
        normalized = [{**r, 'web_pred_decision': r['pred_decision'], 'web_pred_type': r['pred_type']} for r in group]
        assert all(r['strict_report_eligible'] or r['web_pred_decision'] == core.BOTTOM for r in normalized)
        point = core.score_rows(normalized, 'web', labels)
        seed = 20260727 + index
        assert reference['models'][model]['seed'] == seed
        rng = random.Random(seed)
        draws = {k: [] for k in point}
        for _ in range(2000):
            sample = [normalized[rng.randrange(len(normalized))] for _ in normalized]
            scored = core.score_rows(sample, 'web', labels)
            for key, val in scored.items():
                draws[key].append(val)
        ci95 = {k: [core.percentile(v, .025), core.percentile(v, .975)] for k,v in draws.items()}
        checks = {}
        for key, value in point.items():
            target_point = reference['models'][model]['point']['web_' + key]
            target_ci = reference['models'][model]['ci95']['web_' + key]
            pm = abs(value - target_point) <= 1e-12
            cm = [abs(a-b) <= 1e-12 for a,b in zip(ci95[key], target_ci)]
            point_matches += int(pm)
            ci_endpoint_matches += sum(cm)
            checks[key] = {'point_match': pm, 'ci_endpoint_matches': cm, 'point_delta': value-target_point,
                           'ci_deltas': [a-b for a,b in zip(ci95[key],target_ci)]}
        report['models'].append({'display_model': reference['models'][model]['display_model'], 'websites': 600,
                                 'seed': seed, 'point': point, 'ci95': ci95, 'comparison': checks})
        print(json.dumps({'model': reference['models'][model]['display_model'], 'point_matches_so_far': point_matches,
                          'ci_endpoint_matches_so_far': ci_endpoint_matches}), flush=True)
    report.update(point_matches=point_matches, point_comparisons=40, ci_endpoint_matches=ci_endpoint_matches,
                  ci_endpoint_comparisons=80, elapsed_seconds=round(time.monotonic()-started,2))
    deltas = [abs(value) for model in report['models'] for check in model['comparison'].values()
              for value in [check['point_delta'], *check['ci_deltas']]]
    report['max_absolute_difference'] = max(deltas)
    report['exact_float_equal_comparisons'] = sum(value == 0 for value in deltas)
    report['all_equal_at_12_decimal_places'] = all(
        f"{value:.12f}" == f"{reference['models'][model]['point']['web_' + key]:.12f}"
        and all(f'{a:.12f}' == f'{b:.12f}' for a, b in zip(result['ci95'][key], reference['models'][model]['ci95']['web_' + key]))
        for model, result in zip(reference['model_order'], report['models']) for key, value in result['point'].items())
    report['status'] = 'WEB_ONLY_NUMERIC_MATCH_WITHIN_TOLERANCE' if point_matches == 40 and ci_endpoint_matches == 80 else 'PARTIAL_MATCH_SEE_COMPARISON'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ['status','point_matches','ci_endpoint_matches','elapsed_seconds']},indent=2))
    if point_matches != 40 or ci_endpoint_matches != 80:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
