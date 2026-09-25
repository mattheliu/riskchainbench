"""Protocol reconstruction, 2026-09-25; NOT the recovered historical gate code.

Strict submitted-string equality follows the paper supplement. It intentionally
does not reuse the historical Task 1 scorer's URL normalization. No aliases,
Gold or case mappings are bundled. Callers select the frozen v000 prediction.
"""
import re

BOTTOM = "__BOTTOM__"
RESERVED = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.test", re.ASCII)


def strict_top1_gate(candidates, expected_entry):
    """Evaluate only rank 1. Invalid/missing candidates fail; invalid Gold raises."""
    if not isinstance(expected_entry, str) or not RESERVED.fullmatch(expected_entry):
        raise ValueError("expected_entry must be a canonical lowercase three-label .test name")
    if not isinstance(candidates, list) or not candidates:
        return False
    ranks = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            return False
        rank = candidate.get("rank")
        if type(rank) is not int or rank < 1 or not isinstance(candidate.get("value"), str):
            return False
        ranks.append(rank)
    if len(ranks) != len(set(ranks)) or 1 not in ranks:
        return False
    top = next(candidate for candidate in candidates if candidate["rank"] == 1)
    return top["value"] == expected_entry


def apply_gate(web_decision, web_type, gate_pass):
    """Reuse the frozen web prediction on pass; otherwise return bottom twice."""
    if type(gate_pass) is not bool:
        raise ValueError("gate_pass must be an explicit boolean, not a missing value")
    if not isinstance(web_decision, str) or not isinstance(web_type, str):
        raise ValueError("web prediction fields must be strings")
    return (web_decision, web_type) if gate_pass else (BOTTOM, BOTTOM)
