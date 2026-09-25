#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator, FormatChecker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_PATH = (
    PROJECT_ROOT / "schemas/fraud_evidence_case_v0.2.schema.json"
)
SUPPORTED_OUTCOMES = {"SUPPORTED_HIGH_RISK", "BENIGN_SUPPORTED"}
REVIEW_OUTCOMES = {
    "INSUFFICIENT_EVIDENCE",
    "CONFLICTING_EVIDENCE",
    "INVALID_EVIDENCE",
}
REQUIRED_GATE_FIELDS = {
    "collection_authorized",
    "capture_complete",
    "integrity_verified",
    "provenance_complete",
    "fidelity_verified",
    "interaction_trace_complete",
    "sanitization_recorded",
}


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _json_path(parts: Iterable[Any]) -> str:
    result = "$"
    for part in parts:
        if isinstance(part, int):
            result += f"[{part}]"
        else:
            result += f".{part}"
    return result


def schema_errors(
    case: dict[str, Any], schema: dict[str, Any]
) -> list[str]:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [
        f"schema:{_json_path(error.absolute_path)}:{error.message}"
        for error in sorted(
            validator.iter_errors(case),
            key=lambda item: (_json_path(item.absolute_path), item.message),
        )
    ]


def _index_by_id(
    rows: Any, label: str, errors: list[str]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(rows, list):
        return result
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        row_id = str(row.get("id") or "")
        if not row_id:
            continue
        if row_id in result:
            errors.append(f"{label}[{index}]:duplicate_id:{row_id}")
        else:
            result[row_id] = row
    return result


def _portable_path(raw_path: Any) -> bool:
    value = str(raw_path or "").strip()
    if not value:
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_artifacts(
    entities: dict[str, dict[str, Any]],
    artifact_root: Path,
    errors: list[str],
) -> tuple[int, int]:
    root = artifact_root.resolve()
    checked = 0
    verified = 0
    for entity_id, entity in entities.items():
        raw_path = str(entity.get("path") or "")
        if not _portable_path(raw_path):
            continue
        resolved = (root / raw_path).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            errors.append(f"entity:{entity_id}:artifact_path_escapes_root")
            continue
        checked += 1
        if not resolved.is_file():
            errors.append(f"entity:{entity_id}:artifact_missing")
            continue
        expected = str(entity.get("sha256") or "").lower()
        if _sha256_file(resolved) != expected:
            errors.append(f"entity:{entity_id}:artifact_sha256_mismatch")
            continue
        verified += 1
    return checked, verified


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _reference_errors(
    values: Any,
    known: set[str],
    prefix: str,
    errors: list[str],
) -> None:
    if not isinstance(values, list):
        return
    for value in values:
        if str(value) not in known:
            errors.append(f"{prefix}:unknown_reference:{value}")


def _cycle_nodes(graph: dict[str, set[str]]) -> set[str]:
    visiting: set[str] = set()
    visited: set[str] = set()
    cycles: set[str] = set()

    def visit(node: str, stack: list[str]) -> None:
        if node in visiting:
            try:
                start = stack.index(node)
            except ValueError:
                start = 0
            cycles.update(stack[start:])
            return
        if node in visited:
            return
        visiting.add(node)
        stack.append(node)
        for parent in graph.get(node, set()):
            if parent in graph:
                visit(parent, stack)
        stack.pop()
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node, [])
    return cycles


def _is_derived_from(
    entity_id: str,
    root_id: str,
    parents: dict[str, set[str]],
    seen: set[str] | None = None,
) -> bool:
    if entity_id == root_id:
        return True
    visited = set() if seen is None else seen
    if entity_id in visited:
        return False
    visited.add(entity_id)
    return any(
        _is_derived_from(parent, root_id, parents, visited)
        for parent in parents.get(entity_id, set())
    )


def _root_observation_ids(
    subject_id: str,
    observations: set[str],
    inferences: dict[str, dict[str, Any]],
    memo: dict[str, set[str]],
    visiting: set[str] | None = None,
) -> set[str]:
    if subject_id in observations:
        return {subject_id}
    if subject_id in memo:
        return memo[subject_id]
    if subject_id not in inferences:
        return set()
    active = set() if visiting is None else visiting
    if subject_id in active:
        return set()
    active.add(subject_id)
    roots: set[str] = set()
    for parent in inferences[subject_id].get("based_on_ids") or []:
        roots.update(
            _root_observation_ids(
                str(parent), observations, inferences, memo, active
            )
        )
    active.remove(subject_id)
    memo[subject_id] = roots
    return roots


def audit_case(
    case: dict[str, Any],
    schema: dict[str, Any] | None = None,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    loaded_schema = schema or read_json(DEFAULT_SCHEMA_PATH)
    errors = schema_errors(case, loaded_schema)
    blockers: list[str] = []
    warnings: list[str] = []

    provenance = case.get("provenance") if isinstance(case.get("provenance"), dict) else {}
    agents = _index_by_id(provenance.get("agents"), "agent", errors)
    entities = _index_by_id(provenance.get("entities"), "entity", errors)
    activities = _index_by_id(provenance.get("activities"), "activity", errors)
    derivations = _index_by_id(provenance.get("derivations"), "derivation", errors)
    search_scopes = _index_by_id(case.get("search_scopes"), "search_scope", errors)
    dependency_groups = _index_by_id(
        case.get("dependency_groups"), "dependency_group", errors
    )
    observations = _index_by_id(case.get("observations"), "observation", errors)
    inferences = _index_by_id(case.get("inferences"), "inference", errors)
    proposition_set = (
        case.get("proposition_set")
        if isinstance(case.get("proposition_set"), dict)
        else {}
    )
    propositions = _index_by_id(
        proposition_set.get("propositions"), "proposition", errors
    )
    predictions = _index_by_id(case.get("predictions"), "prediction", errors)
    alternatives = _index_by_id(
        case.get("alternative_explanations"), "alternative", errors
    )
    assessments = _index_by_id(case.get("assessments"), "assessment", errors)

    gate = case.get("evidence_gate") if isinstance(case.get("evidence_gate"), dict) else {}
    for field in sorted(REQUIRED_GATE_FIELDS):
        if gate.get(field) is not True:
            errors.append(f"evidence_gate_failed:{field}")

    for entity_id, entity in entities.items():
        if not _portable_path(entity.get("path")):
            errors.append(f"entity:{entity_id}:path_not_portable")
        if entity.get("primary_source") is True and entity.get("source_stage") != "source_capture":
            errors.append(f"entity:{entity_id}:primary_source_stage_invalid")

    artifact_files_checked = 0
    artifact_files_verified = 0
    if artifact_root is not None:
        artifact_files_checked, artifact_files_verified = _verify_artifacts(
            entities, artifact_root, errors
        )

    entity_ids = set(entities)
    agent_ids = set(agents)
    activity_ids = set(activities)
    generated_by: dict[str, set[str]] = defaultdict(set)
    for activity_id, activity in activities.items():
        _reference_errors(
            activity.get("agent_ids"), agent_ids, f"activity:{activity_id}:agent", errors
        )
        _reference_errors(
            activity.get("used_entity_ids"),
            entity_ids,
            f"activity:{activity_id}:used_entity",
            errors,
        )
        _reference_errors(
            activity.get("generated_entity_ids"),
            entity_ids,
            f"activity:{activity_id}:generated_entity",
            errors,
        )
        used = {str(value) for value in activity.get("used_entity_ids") or []}
        generated = {
            str(value) for value in activity.get("generated_entity_ids") or []
        }
        for entity_id in generated:
            generated_by[entity_id].add(activity_id)
        if used.intersection(generated):
            errors.append(f"activity:{activity_id}:entity_used_and_generated_in_place")
        started_at = _parse_timestamp(activity.get("started_at"))
        ended_at = _parse_timestamp(activity.get("ended_at"))
        if started_at and ended_at and ended_at < started_at:
            errors.append(f"activity:{activity_id}:ended_before_started")

    for entity_id in entities:
        producers = generated_by.get(entity_id, set())
        if not producers:
            errors.append(f"entity:{entity_id}:no_generating_activity")
        elif len(producers) > 1:
            errors.append(
                f"entity:{entity_id}:multiple_generating_activities:{','.join(sorted(producers))}"
            )

    derivation_parents: dict[str, set[str]] = defaultdict(set)
    for derivation_id, derivation in derivations.items():
        generated_id = str(derivation.get("generated_entity_id") or "")
        used_ids = {str(value) for value in derivation.get("used_entity_ids") or []}
        activity_id = str(derivation.get("activity_id") or "")
        if generated_id not in entity_ids:
            errors.append(
                f"derivation:{derivation_id}:unknown_generated_entity:{generated_id}"
            )
        _reference_errors(
            list(used_ids),
            entity_ids,
            f"derivation:{derivation_id}:used_entity",
            errors,
        )
        if activity_id not in activity_ids:
            errors.append(f"derivation:{derivation_id}:unknown_activity:{activity_id}")
        else:
            activity = activities[activity_id]
            generated_by_activity = {
                str(value) for value in activity.get("generated_entity_ids") or []
            }
            used_by_activity = {
                str(value) for value in activity.get("used_entity_ids") or []
            }
            if generated_id not in generated_by_activity:
                errors.append(
                    f"derivation:{derivation_id}:generated_entity_not_activity_output"
                )
            if not used_ids.issubset(used_by_activity):
                errors.append(
                    f"derivation:{derivation_id}:used_entity_not_activity_input"
                )
        derivation_parents[generated_id].update(used_ids)

    for entity_id in _cycle_nodes(derivation_parents):
        errors.append(f"provenance_derivation_cycle:{entity_id}")

    search_scope_ids = set(search_scopes)
    observation_ids = set(observations)
    for scope_id, scope in search_scopes.items():
        _reference_errors(
            scope.get("evidence_ids"),
            observation_ids,
            f"search_scope:{scope_id}:evidence",
            errors,
        )
        planned = scope.get("planned_actions")
        completed = scope.get("completed_actions")
        if (
            scope.get("coverage_status") == "COMPLETE"
            and isinstance(planned, int)
            and isinstance(completed, int)
            and completed < planned
        ):
            errors.append(f"search_scope:{scope_id}:complete_but_actions_missing")
        if scope.get("coverage_status") == "COMPLETE" and not str(
            scope.get("completion_basis") or ""
        ).strip():
            errors.append(f"search_scope:{scope_id}:completion_basis_missing")

    dependency_membership: dict[str, list[str]] = defaultdict(list)
    for group_id, group in dependency_groups.items():
        _reference_errors(
            group.get("root_entity_ids"),
            entity_ids,
            f"dependency_group:{group_id}:root_entity",
            errors,
        )
        _reference_errors(
            group.get("member_observation_ids"),
            observation_ids,
            f"dependency_group:{group_id}:member_observation",
            errors,
        )
        for evidence_id in group.get("member_observation_ids") or []:
            dependency_membership[str(evidence_id)].append(group_id)

    incomplete_negative_observations: set[str] = set()
    for evidence_id, observation in observations.items():
        entity_id = str(observation.get("entity_id") or "")
        activity_id = str(observation.get("activity_id") or "")
        group_id = str(observation.get("dependency_group_id") or "")
        if entity_id not in entity_ids:
            errors.append(f"observation:{evidence_id}:unknown_entity:{entity_id}")
        if activity_id not in activity_ids:
            errors.append(f"observation:{evidence_id}:unknown_activity:{activity_id}")
        elif entity_id in entity_ids:
            activity = activities[activity_id]
            touched = {
                str(value)
                for field in ("used_entity_ids", "generated_entity_ids")
                for value in activity.get(field) or []
            }
            if entity_id not in touched:
                errors.append(
                    f"observation:{evidence_id}:entity_not_touched_by_activity"
                )
        if group_id not in dependency_groups:
            errors.append(f"observation:{evidence_id}:unknown_dependency_group:{group_id}")
        memberships = dependency_membership.get(evidence_id, [])
        if memberships != [group_id]:
            errors.append(
                f"observation:{evidence_id}:dependency_membership_mismatch:{','.join(memberships)}"
            )
        elif entity_id in entity_ids:
            roots = {
                str(value)
                for value in dependency_groups[group_id].get("root_entity_ids") or []
            }
            if roots and not any(
                _is_derived_from(entity_id, root, derivation_parents) for root in roots
            ):
                errors.append(
                    f"observation:{evidence_id}:entity_not_derived_from_group_root"
                )
        scope_refs = {
            str(value) for value in observation.get("search_scope_ids") or []
        }
        _reference_errors(
            list(scope_refs),
            search_scope_ids,
            f"observation:{evidence_id}:search_scope",
            errors,
        )
        if observation.get("polarity") == "ABSENCE":
            incomplete = [
                scope_id
                for scope_id in scope_refs
                if search_scopes.get(scope_id, {}).get("coverage_status") != "COMPLETE"
            ]
            if not scope_refs or incomplete:
                incomplete_negative_observations.add(evidence_id)
                warnings.append(
                    f"negative_observation:{evidence_id}:incomplete_search_scope"
                )

    inference_ids = set(inferences)
    subject_ids = observation_ids | inference_ids
    inference_graph: dict[str, set[str]] = {}
    for inference_id, inference in inferences.items():
        parents = {str(value) for value in inference.get("based_on_ids") or []}
        _reference_errors(
            list(parents),
            subject_ids,
            f"inference:{inference_id}:based_on",
            errors,
        )
        agent_id = str(inference.get("agent_id") or "")
        if agent_id not in agent_ids:
            errors.append(f"inference:{inference_id}:unknown_agent:{agent_id}")
        inference_graph[inference_id] = {
            parent for parent in parents if parent in inference_ids
        }
    for inference_id in _cycle_nodes(inference_graph):
        errors.append(f"inference_cycle:{inference_id}")

    proposition_ids = set(propositions)
    target_ids = {
        proposition_id
        for proposition_id, proposition in propositions.items()
        if proposition.get("role") == "TARGET"
    }
    alternative_ids = {
        proposition_id
        for proposition_id, proposition in propositions.items()
        if proposition.get("role") == "ALTERNATIVE"
    }
    if len(target_ids) != 1:
        errors.append(f"propositions:target_count:{len(target_ids)}")
    if not alternative_ids:
        errors.append("propositions:alternative_missing")
    decision_level = (
        case.get("scope", {}).get("decision_level")
        if isinstance(case.get("scope"), dict)
        else None
    )
    alignments = {str(row.get("risk_alignment") or "") for row in propositions.values()}
    if "HIGH_RISK" not in alignments or "BENIGN" not in alignments:
        errors.append("propositions:high_risk_and_benign_pair_required")
    for proposition_id, proposition in propositions.items():
        if proposition.get("level") != decision_level:
            errors.append(
                f"proposition:{proposition_id}:level_mismatch:{proposition.get('level')}!={decision_level}"
            )
        exclusive = {
            str(value) for value in proposition.get("mutually_exclusive_with") or []
        }
        _reference_errors(
            list(exclusive),
            proposition_ids,
            f"proposition:{proposition_id}:exclusive",
            errors,
        )
        if proposition_id in exclusive:
            errors.append(f"proposition:{proposition_id}:self_exclusive")
        for other_id in exclusive:
            other_exclusive = {
                str(value)
                for value in propositions.get(other_id, {}).get(
                    "mutually_exclusive_with"
                )
                or []
            }
            if proposition_id not in other_exclusive:
                errors.append(
                    f"proposition:{proposition_id}:asymmetric_exclusivity:{other_id}"
                )
    for target_id in target_ids:
        target_exclusive = {
            str(value)
            for value in propositions[target_id].get("mutually_exclusive_with") or []
        }
        for alternative_id in alternative_ids:
            if alternative_id not in target_exclusive:
                errors.append(
                    f"proposition_pair:not_mutually_exclusive:{target_id}:{alternative_id}"
                )

    prediction_ids = set(predictions)
    prediction_count_by_proposition: dict[str, int] = defaultdict(int)
    for prediction_id, prediction in predictions.items():
        proposition_id = str(prediction.get("proposition_id") or "")
        if proposition_id not in proposition_ids:
            errors.append(
                f"prediction:{prediction_id}:unknown_proposition:{proposition_id}"
            )
        else:
            prediction_count_by_proposition[proposition_id] += 1
        _reference_errors(
            prediction.get("search_scope_ids"),
            search_scope_ids,
            f"prediction:{prediction_id}:search_scope",
            errors,
        )
    for proposition_id in proposition_ids:
        if prediction_count_by_proposition.get(proposition_id, 0) == 0:
            errors.append(f"proposition:{proposition_id}:prediction_missing")

    alternative_explanation_ids = set(alternatives)
    for alternative_id, alternative in alternatives.items():
        _reference_errors(
            alternative.get("related_proposition_ids"),
            proposition_ids,
            f"alternative:{alternative_id}:proposition",
            errors,
        )
        _reference_errors(
            alternative.get("evidence_ids"),
            observation_ids,
            f"alternative:{alternative_id}:evidence",
            errors,
        )
        _reference_errors(
            alternative.get("search_scope_ids"),
            search_scope_ids,
            f"alternative:{alternative_id}:search_scope",
            errors,
        )
        if alternative.get("status") == "REFUTED" and not (
            alternative.get("evidence_ids") or []
        ):
            blockers.append(f"alternative:{alternative_id}:refuted_without_evidence")

    assessment_count_by_proposition: dict[str, int] = defaultdict(int)
    for assessment_id, assessment in assessments.items():
        subject_id = str(assessment.get("subject_id") or "")
        proposition_id = str(assessment.get("proposition_id") or "")
        prediction_id = str(assessment.get("prediction_id") or "")
        if subject_id not in subject_ids:
            errors.append(f"assessment:{assessment_id}:unknown_subject:{subject_id}")
        if proposition_id not in proposition_ids:
            errors.append(
                f"assessment:{assessment_id}:unknown_proposition:{proposition_id}"
            )
        else:
            assessment_count_by_proposition[proposition_id] += 1
        if prediction_id:
            if prediction_id not in prediction_ids:
                errors.append(
                    f"assessment:{assessment_id}:unknown_prediction:{prediction_id}"
                )
            elif predictions[prediction_id].get("proposition_id") != proposition_id:
                errors.append(
                    f"assessment:{assessment_id}:prediction_proposition_mismatch"
                )
        _reference_errors(
            assessment.get("alternative_explanation_ids"),
            alternative_explanation_ids,
            f"assessment:{assessment_id}:alternative",
            errors,
        )
        if assessment.get("direction") == "NEUTRAL" and assessment.get("strength") != "LOW":
            warnings.append(f"assessment:{assessment_id}:neutral_strength_not_low")

    covered_propositions = {
        proposition_id
        for proposition_id, count in assessment_count_by_proposition.items()
        if count > 0
    }
    proposition_coverage = (
        len(covered_propositions) / len(proposition_ids) if proposition_ids else 0.0
    )

    conclusion = case.get("conclusion") if isinstance(case.get("conclusion"), dict) else {}
    submitted_outcome = str(conclusion.get("outcome") or "INVALID_EVIDENCE")
    selected_id = conclusion.get("selected_proposition_id")
    selected_id = str(selected_id) if selected_id is not None else None
    support_ids = {
        str(value) for value in conclusion.get("supporting_assessment_ids") or []
    }
    counter_ids = {
        str(value) for value in conclusion.get("counter_assessment_ids") or []
    }
    _reference_errors(
        list(support_ids), set(assessments), "conclusion:supporting_assessment", errors
    )
    _reference_errors(
        list(counter_ids), set(assessments), "conclusion:counter_assessment", errors
    )

    supported_outcome = submitted_outcome in SUPPORTED_OUTCOMES
    if supported_outcome:
        if selected_id not in proposition_ids:
            blockers.append("conclusion:selected_proposition_missing")
        else:
            expected_alignment = (
                "HIGH_RISK"
                if submitted_outcome == "SUPPORTED_HIGH_RISK"
                else "BENIGN"
            )
            if propositions[selected_id].get("risk_alignment") != expected_alignment:
                blockers.append(
                    f"conclusion:selected_proposition_alignment_mismatch:{expected_alignment}"
                )
        if not support_ids:
            blockers.append("conclusion:supporting_assessments_missing")
    elif selected_id is not None:
        blockers.append("conclusion:selected_proposition_for_non_supported_outcome")

    for assessment_id in support_ids:
        assessment = assessments.get(assessment_id, {})
        if assessment.get("proposition_id") != selected_id:
            blockers.append(
                f"conclusion:support_assessment_wrong_proposition:{assessment_id}"
            )
        if assessment.get("direction") != "SUPPORTS":
            blockers.append(
                f"conclusion:support_assessment_wrong_direction:{assessment_id}"
            )
    for assessment_id in counter_ids:
        assessment = assessments.get(assessment_id, {})
        if assessment.get("proposition_id") != selected_id:
            blockers.append(
                f"conclusion:counter_assessment_wrong_proposition:{assessment_id}"
            )
        if assessment.get("direction") != "REFUTES":
            blockers.append(
                f"conclusion:counter_assessment_wrong_direction:{assessment_id}"
            )

    all_refuting_selected = {
        assessment_id
        for assessment_id, assessment in assessments.items()
        if selected_id
        and assessment.get("proposition_id") == selected_id
        and assessment.get("direction") == "REFUTES"
    }
    omitted_counter = all_refuting_selected - counter_ids
    if supported_outcome and omitted_counter:
        blockers.append(
            "conclusion:counterevidence_omitted:" + ",".join(sorted(omitted_counter))
        )

    root_memo: dict[str, set[str]] = {}
    support_root_observations: set[str] = set()
    for assessment_id in support_ids:
        subject_id = str(assessments.get(assessment_id, {}).get("subject_id") or "")
        support_root_observations.update(
            _root_observation_ids(
                subject_id, observation_ids, inferences, root_memo
            )
        )
    support_groups = {
        str(observations[evidence_id].get("dependency_group_id") or "")
        for evidence_id in support_root_observations
        if evidence_id in observations
    }
    support_groups.discard("")
    min_groups = int(
        (case.get("decision_policy") or {}).get(
            "min_independent_support_groups", 2
        )
    )
    if supported_outcome and len(support_groups) < min_groups:
        blockers.append(
            f"conclusion:independent_support_groups={len(support_groups)}<{min_groups}"
        )
    invalid_negative_support = support_root_observations.intersection(
        incomplete_negative_observations
    )
    if supported_outcome and invalid_negative_support:
        blockers.append(
            "conclusion:negative_evidence_without_complete_scope:"
            + ",".join(sorted(invalid_negative_support))
        )

    unresolved_material = {
        alternative_id
        for alternative_id, alternative in alternatives.items()
        if alternative.get("materiality") == "MATERIAL"
        and alternative.get("status") in {"NOT_TESTED", "SUPPORTED", "UNRESOLVED"}
    }
    listed_unresolved = {
        str(value) for value in conclusion.get("unresolved_alternative_ids") or []
    }
    if listed_unresolved != unresolved_material:
        blockers.append(
            "conclusion:unresolved_alternative_set_mismatch:expected="
            + ",".join(sorted(unresolved_material))
        )
    if supported_outcome and unresolved_material:
        blockers.append(
            "conclusion:material_alternatives_unresolved:"
            + ",".join(sorted(unresolved_material))
        )

    if supported_outcome and proposition_coverage < 1.0:
        missing = sorted(proposition_ids - covered_propositions)
        blockers.append(
            "conclusion:proposition_coverage_incomplete:" + ",".join(missing)
        )

    strong_support_by_proposition: dict[str, set[str]] = defaultdict(set)
    for assessment in assessments.values():
        if assessment.get("direction") != "SUPPORTS" or assessment.get(
            "strength"
        ) not in {"MODERATE", "HIGH"}:
            continue
        subject_id = str(assessment.get("subject_id") or "")
        roots = _root_observation_ids(
            subject_id, observation_ids, inferences, root_memo
        )
        strong_support_by_proposition[
            str(assessment.get("proposition_id") or "")
        ].update(
            str(observations[root].get("dependency_group_id") or "")
            for root in roots
            if root in observations
        )
    competing_supported = {
        proposition_id
        for proposition_id, groups in strong_support_by_proposition.items()
        if proposition_id != selected_id and len(groups - {""}) >= min_groups
    }
    if supported_outcome and competing_supported:
        blockers.append(
            "conclusion:competing_proposition_strongly_supported:"
            + ",".join(sorted(competing_supported))
        )

    stopping = case.get("stopping") if isinstance(case.get("stopping"), dict) else {}
    stopping_decision = str(stopping.get("decision") or "")
    _reference_errors(
        stopping.get("unmet_prediction_ids"),
        prediction_ids,
        "stopping:unmet_prediction",
        errors,
    )
    _reference_errors(
        stopping.get("unresolved_alternative_ids"),
        alternative_explanation_ids,
        "stopping:unresolved_alternative",
        errors,
    )
    stopping_unresolved = {
        str(value) for value in stopping.get("unresolved_alternative_ids") or []
    }
    if stopping_unresolved != unresolved_material:
        blockers.append(
            "stopping:unresolved_alternative_set_mismatch:expected="
            + ",".join(sorted(unresolved_material))
        )
    if supported_outcome and stopping_decision != "STOP_SUFFICIENT":
        blockers.append("stopping:supported_outcome_requires_stop_sufficient")
    if not supported_outcome and stopping_decision == "STOP_SUFFICIENT":
        blockers.append("stopping:stop_sufficient_without_supported_outcome")

    if submitted_outcome in REVIEW_OUTCOMES and conclusion.get(
        "requires_human_review"
    ) is not True:
        blockers.append("conclusion:review_outcome_requires_human_review")

    errors = sorted(set(errors))
    blockers = sorted(set(blockers))
    warnings = sorted(set(warnings))
    evidence_valid = not errors
    conclusion_admissible = evidence_valid and not blockers
    conflict_blocked = any(
        value.startswith("conclusion:competing_proposition_strongly_supported")
        for value in blockers
    )
    if not evidence_valid:
        recommended_outcome = "INVALID_EVIDENCE"
    elif conflict_blocked:
        recommended_outcome = "CONFLICTING_EVIDENCE"
    elif blockers:
        recommended_outcome = "INSUFFICIENT_EVIDENCE"
    else:
        recommended_outcome = submitted_outcome

    return {
        "protocol": {
            "name": "covert_entry_evidence_protocol",
            "version": "0.2.0",
            "adjudication_scope": "RESEARCH_RISK_TRIAGE",
            "legal_status": "UNDETERMINED",
            "is_gold_label": False,
        },
        "case_id": case.get("case_id"),
        "site_id": case.get("site_id"),
        "status": "PASS" if conclusion_admissible else "FAIL",
        "evidence_valid": evidence_valid,
        "conclusion_admissible": conclusion_admissible,
        "submitted_outcome": submitted_outcome,
        "recommended_outcome": recommended_outcome,
        "errors": errors,
        "blockers": blockers,
        "warnings": warnings,
        "metrics": {
            "agents": len(agents),
            "entities": len(entities),
            "artifact_verification_enabled": artifact_root is not None,
            "artifact_files_checked": artifact_files_checked,
            "artifact_files_verified": artifact_files_verified,
            "activities": len(activities),
            "derivations": len(derivations),
            "observations": len(observations),
            "inferences": len(inferences),
            "dependency_groups": len(dependency_groups),
            "search_scopes": len(search_scopes),
            "propositions": len(propositions),
            "predictions": len(predictions),
            "assessments": len(assessments),
            "proposition_coverage": proposition_coverage,
            "support_root_observations": sorted(support_root_observations),
            "independent_support_groups": sorted(support_groups),
            "independent_support_group_count": len(support_groups),
            "negative_observations": sum(
                1
                for observation in observations.values()
                if observation.get("polarity") == "ABSENCE"
            ),
            "invalid_negative_observations": sorted(
                incomplete_negative_observations
            ),
            "unresolved_material_alternatives": sorted(unresolved_material),
            "omitted_counter_assessments": sorted(omitted_counter),
        },
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a proposition-driven evidence case and audit whether its "
            "conclusion is methodologically supportable."
        )
    )
    parser.add_argument("--case", type=Path, required=True, help="Evidence case JSON")
    parser.add_argument(
        "--schema", type=Path, default=DEFAULT_SCHEMA_PATH, help="JSON Schema"
    )
    parser.add_argument("--output", type=Path, help="Write audit JSON to this path")
    parser.add_argument(
        "--artifact-root",
        type=Path,
        help="Resolve entity paths under this root and verify every registered SHA-256.",
    )
    parser.add_argument(
        "--strict", action="store_true", help="Return non-zero when the audit fails"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = audit_case(
        read_json(args.case),
        read_json(args.schema),
        artifact_root=args.artifact_root,
    )
    if args.output:
        atomic_json(args.output, result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if args.strict and result["status"] != "PASS":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
