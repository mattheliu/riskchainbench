#!/usr/bin/env python3
"""Small sequential HTTP harness for Task 1 reconstruction probes.

This runner intentionally avoids agent frameworks. It sends one task per
OpenAI-compatible chat-completions request, retries only transient or
response-format failures, and writes a schema-compatible fallback prediction
after retry exhaustion so failures remain in the evaluation denominator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
from typing import Any, Callable
import urllib.error
import urllib.request


PLATFORMS = {
    "bilibili",
    "douyin",
    "wechat",
    "wechat_channels",
    "qq",
    "weibo",
    "baidu_tieba",
    "tiktok",
    "unknown",
}
RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}
SAMPLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LENGTH_RETRY_INSTRUCTION = (
    "The previous response reached its output-token limit before producing the "
    "required JSON. Respond more concisely. Return only one complete JSON object "
    "that follows the requested Task 1 output contract; do not include analysis."
)
EMPTY_RETRY_INSTRUCTION = (
    "The previous response contained no model-visible content. Return only one "
    "complete JSON object that follows the requested Task 1 output contract; do "
    "not include analysis."
)


class RetryableAttempt(RuntimeError):
    """A transient request or response failure eligible for another attempt."""

    def __init__(
        self,
        kind: str,
        detail: str,
        *,
        finish_reason: str = "",
        usage: dict[str, Any] | None = None,
        response_model: str = "",
        response_content: str = "",
        reasoning_content_characters: int = 0,
        http_status: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.finish_reason = finish_reason
        self.usage = usage or {}
        self.response_model = response_model
        self.response_content = response_content
        self.reasoning_content_characters = reasoning_content_characters
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds


class FatalRunError(RuntimeError):
    """A configuration or authorization failure that must stop the batch."""


class ModelOutputFailure(RuntimeError):
    """A completed but invalid model response that must remain countable."""

    def __init__(
        self,
        kind: str,
        detail: str,
        *,
        response_content: str = "",
    ) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.response_content = response_content


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        raise ValueError(f"empty JSONL: {path}")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"JSONL must contain objects: {path}")
    return rows


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    atomic_text(path, "".join(canonical_json(row) + "\n" for row in rows))


def write_json(path: Path, value: Any) -> None:
    atomic_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def sample_variant_index(sample_id: str) -> int:
    match = re.search(r"--v([0-9]{3})$", sample_id)
    if match is None:
        raise ValueError(f"sample_id lacks variant suffix: {sample_id}")
    return int(match.group(1))


def source_id(sample_id: str) -> str:
    return sample_id.rsplit("--v", 1)[0]


def select_tasks(
    tasks: list[dict[str, Any]],
    *,
    source_limit: int,
    variant_indices: set[int] | None,
) -> list[dict[str, Any]]:
    source_order: list[str] = []
    for task in tasks:
        current = source_id(str(task["sample_id"]))
        if current not in source_order:
            source_order.append(current)
    if source_limit:
        source_order = source_order[:source_limit]
    allowed_sources = set(source_order)
    selected = [
        task
        for task in tasks
        if source_id(str(task["sample_id"])) in allowed_sources
        and (
            variant_indices is None
            or sample_variant_index(str(task["sample_id"])) in variant_indices
        )
    ]
    if not selected:
        raise ValueError("task selection is empty")
    return selected


def request_options(model: str, max_tokens: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "temperature": 0,
        "reasoning_effort": "minimal",
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
    }
    if model.startswith("gpt-5."):
        payload.pop("temperature")
        payload.pop("reasoning_effort")
    elif model.startswith("qwen"):
        # Qwen's vLLM-compatible hard switch is a chat-template kwarg. A
        # top-level enable_thinking field is accepted by some gateways but may
        # be ignored before the chat template is rendered.
        payload["reasoning_effort"] = "none"
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    elif model.startswith("kimi-"):
        payload["thinking"] = {"type": "disabled"}
    return payload


def build_user_payload(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase": "TASK1_RECONSTRUCTION",
        "evaluation_setting": "BLIND",
        "input_view": task.get("input_view", "TOKEN_TEXT"),
        "platform": task.get("platform", "unknown"),
        "surface": task.get("surface", "PRIVATE_MESSAGE"),
        "messages": task.get("messages") or [],
    }


def parse_retry_after_seconds(
    headers: Any,
    body: str,
) -> float | None:
    values = []
    if headers is not None:
        values.append(headers.get("Retry-After"))
    try:
        parsed = json.loads(body)
        upstream_headers = (
            parsed.get("error", {}).get("upstream_headers", {})
            if isinstance(parsed, dict)
            else {}
        )
        if isinstance(upstream_headers, dict):
            values.append(upstream_headers.get("retry-after"))
    except (json.JSONDecodeError, AttributeError):
        pass
    for value in values:
        if value is None:
            continue
        try:
            return min(max(float(value), 0.0), 120.0)
        except (TypeError, ValueError):
            continue
    return None


def safe_http_error_detail(status: int, body: str) -> str:
    """Keep actionable gateway errors without persisting internal route details."""

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return f"HTTP {status}: {body[:1000]}"
    if not isinstance(parsed, dict):
        return f"HTTP {status}: malformed JSON error body"
    error = parsed.get("error")
    if not isinstance(error, dict):
        return f"HTTP {status}: unstructured JSON error body"
    upstream_body = error.get("upstream_body")
    upstream_error = (
        upstream_body.get("error")
        if isinstance(upstream_body, dict)
        and isinstance(upstream_body.get("error"), dict)
        else {}
    )
    upstream_headers = error.get("upstream_headers")
    retry_after = (
        upstream_headers.get("retry-after")
        if isinstance(upstream_headers, dict)
        else None
    )
    detail = {
        "type": error.get("type"),
        "message": error.get("message"),
        "upstream_status": error.get("upstream_status"),
        "upstream_error_type": upstream_error.get("type"),
        "upstream_error_message": upstream_error.get("message"),
        "retry_after": retry_after,
        "attempts": error.get("attempts"),
    }
    return f"HTTP {status}: {canonical_json(detail)}"


def perform_request(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_payload: dict[str, Any],
    max_tokens: int,
    timeout_seconds: float,
    recovery_instruction: str = "",
) -> dict[str, Any]:
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False, indent=1),
        },
    ]
    if recovery_instruction:
        messages.append({"role": "user", "content": recovery_instruction})
    payload = {
        "model": model,
        "messages": messages,
        **request_options(model, max_tokens),
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1000]
        if exc.code in RETRYABLE_HTTP_STATUS:
            raise RetryableAttempt(
                "http_retryable",
                safe_http_error_detail(exc.code, body),
                http_status=exc.code,
                retry_after_seconds=parse_retry_after_seconds(
                    exc.headers,
                    body,
                ),
            ) from exc
        raise FatalRunError(
            "non-retryable "
            + safe_http_error_detail(exc.code, body)
        ) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise RetryableAttempt(
            "transport",
            f"{type(exc).__name__}: {exc}",
        ) from exc
    except json.JSONDecodeError as exc:
        raise RetryableAttempt(
            "gateway_json",
            f"gateway response is not JSON: {exc}",
        ) from exc


def response_content(response: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    try:
        choice = response["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RetryableAttempt(
            "gateway_shape",
            "gateway response lacks choices[0].message",
        ) from exc
    content = message.get("content") or ""
    metadata = {
        "finish_reason": str(choice.get("finish_reason") or ""),
        "usage": response.get("usage") or {},
        "response_model": str(response.get("model") or ""),
        "response_id": str(response.get("id") or ""),
        # Do not persist hidden reasoning text. Its length is enough to detect
        # whether the endpoint honored the requested non-thinking policy.
        "reasoning_content_characters": len(
            message.get("reasoning_content") or ""
        ),
    }
    if not isinstance(content, str) or not content.strip():
        raise RetryableAttempt(
            "empty_response",
            "model response content is empty",
            finish_reason=metadata["finish_reason"],
            usage=metadata["usage"],
            response_model=metadata["response_model"],
            reasoning_content_characters=metadata[
                "reasoning_content_characters"
            ],
        )
    return content, metadata


def extract_json_object(content: str) -> dict[str, Any]:
    candidates = [content.strip()]
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.S)
    if fenced:
        candidates.append(fenced.group(1))
    start = content.find("{")
    end = content.rfind("}")
    if start >= 0 and end > start:
        candidates.append(content[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ModelOutputFailure(
        "response_json",
        "model response contains no valid JSON object",
        response_content=content[:4000],
    )


def normalize_model_output(
    value: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any]:
    required_any = {
        "reconstructed_messages",
        "reconstructed_text",
    }
    if not required_any.intersection(value):
        raise ModelOutputFailure(
            "output_contract",
            "response lacks reconstructed_messages or reconstructed_text",
        )

    expected_messages = task.get("messages") or []
    expected_ids = [str(row["message_id"]) for row in expected_messages]
    message_rows = value.get("reconstructed_messages")
    if not message_rows and isinstance(value.get("reconstructed_text"), str):
        if len(expected_ids) != 1:
            raise ModelOutputFailure(
                "output_contract",
                "single reconstructed_text cannot represent multiple messages",
            )
        message_rows = [
            {
                "message_id": expected_ids[0],
                "text": value["reconstructed_text"],
            }
        ]
    if not isinstance(message_rows, list):
        raise ModelOutputFailure(
            "output_contract",
            "reconstructed_messages is not a list",
        )
    reconstructed = []
    for row in message_rows:
        if not isinstance(row, dict) or not isinstance(row.get("text"), str):
            raise ModelOutputFailure(
                "output_contract",
                "invalid reconstructed message",
            )
        reconstructed.append(
            {
                "message_id": str(row.get("message_id") or ""),
                "text": row["text"],
            }
        )
    if [row["message_id"] for row in reconstructed] != expected_ids:
        raise ModelOutputFailure(
            "output_contract",
            "reconstructed message IDs differ from the input",
        )

    raw_candidates = value.get("entry_candidates") or []
    if not isinstance(raw_candidates, list):
        raise ModelOutputFailure(
            "output_contract",
            "entry_candidates is not a list",
        )
    candidates = []
    for index, row in enumerate(raw_candidates[:5], 1):
        if not isinstance(row, dict):
            raise ModelOutputFailure(
                "output_contract",
                "entry candidate is not an object",
            )
        candidate = row.get("value", row.get("entry"))
        if not isinstance(candidate, str):
            raise ModelOutputFailure(
                "output_contract",
                "entry candidate has no string value",
            )
        confidence = row.get("confidence", 0.5)
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= float(confidence) <= 1
        ):
            raise ModelOutputFailure(
                "output_contract",
                "entry candidate confidence is invalid",
            )
        candidates.append(
            {
                "value": candidate,
                "rank": index,
                "confidence": float(confidence),
            }
        )

    intent = value.get("intent", value.get("intent_label"))
    if not isinstance(intent, str):
        raise ModelOutputFailure("output_contract", "intent is not a string")
    platform = value.get("platform")
    if platform not in PLATFORMS:
        raise ModelOutputFailure("output_contract", "platform is invalid")
    abstain = value.get("abstain")
    if not isinstance(abstain, bool):
        raise ModelOutputFailure("output_contract", "abstain is not a boolean")
    uncertainties = value.get("uncertainties")
    if not isinstance(uncertainties, list) or any(
        not isinstance(row, str) for row in uncertainties
    ):
        raise ModelOutputFailure(
            "output_contract",
            "uncertainties is not a string array",
        )

    return {
        "reconstructed_messages": reconstructed,
        "intent": intent,
        "platform": platform,
        "entry_candidates": candidates,
        "abstain": abstain,
        "uncertainties": uncertainties,
    }


def prediction_from_output(
    task: dict[str, Any],
    output: dict[str, Any],
    *,
    model: str,
    run_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": "obfuscated-reconstruction-prediction/v0.1",
        "sample_id": task["sample_id"],
        "model_id": model,
        "run_id": run_id,
        "input_view": task.get("input_view", "TOKEN_TEXT"),
        **output,
    }


def failure_prediction(
    task: dict[str, Any],
    *,
    model: str,
    run_id: str,
    final_reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": "obfuscated-reconstruction-prediction/v0.1",
        "sample_id": task["sample_id"],
        "model_id": model,
        "run_id": run_id,
        "input_view": task.get("input_view", "TOKEN_TEXT"),
        "reconstructed_messages": [],
        "intent": "",
        "platform": task.get("platform", "unknown"),
        "entry_candidates": [],
        "abstain": True,
        "uncertainties": [f"HTTP harness exhausted retries: {final_reason}"],
    }


def run_task(
    task: dict[str, Any],
    *,
    base_url: str,
    api_key: str,
    model: str,
    run_id: str,
    system_prompt: str,
    max_tokens: int,
    timeout_seconds: float,
    max_attempts: int,
    retry_delays: list[float],
    request_fn: Callable[..., dict[str, Any]] = perform_request,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], dict[str, Any]]:
    attempt_rows = []
    user_payload = build_user_payload(task)
    recovery_instruction = ""
    request_fingerprint = sha256_text(
        canonical_json(
            {
                "model": model,
                "system_prompt_sha256": sha256_text(system_prompt),
                "user_payload": user_payload,
                "request_options": request_options(model, max_tokens),
            }
        )
    )

    for attempt in range(1, max_attempts + 1):
        started = time.monotonic()
        content = ""
        metadata: dict[str, Any] = {}
        try:
            response = request_fn(
                base_url=base_url,
                api_key=api_key,
                model=model,
                system_prompt=system_prompt,
                user_payload=user_payload,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
                recovery_instruction=recovery_instruction,
            )
            content, metadata = response_content(response)
            parsed = extract_json_object(content)
            normalized = normalize_model_output(parsed, task)
            attempt_rows.append(
                {
                    "attempt": attempt,
                    "status": "PASS",
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "finish_reason": metadata["finish_reason"],
                    "usage": metadata["usage"],
                    "response_model": metadata["response_model"],
                    "response_id": metadata["response_id"],
                    "reasoning_content_characters": metadata[
                        "reasoning_content_characters"
                    ],
                    "recovery_instruction_used": bool(recovery_instruction),
                    "response_content": content[:8000],
                }
            )
            return (
                prediction_from_output(
                    task,
                    normalized,
                    model=model,
                    run_id=run_id,
                ),
                {
                    "sample_id": task["sample_id"],
                    "status": "PASS",
                    "request_sha256": request_fingerprint,
                    "attempts": attempt_rows,
                },
            )
        except RetryableAttempt as exc:
            attempt_rows.append(
                {
                    "attempt": attempt,
                    "status": "FAIL_RETRYABLE",
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "error_kind": exc.kind,
                    "error": exc.detail[:1000],
                    "http_status": exc.http_status,
                    "finish_reason": exc.finish_reason
                    or metadata.get("finish_reason", ""),
                    "usage": exc.usage or metadata.get("usage", {}),
                    "response_model": exc.response_model
                    or metadata.get("response_model", ""),
                    "reasoning_content_characters": (
                        exc.reasoning_content_characters
                        or metadata.get("reasoning_content_characters", 0)
                    ),
                    "recovery_instruction_used": bool(recovery_instruction),
                    "response_content": (
                        exc.response_content or content
                    )[:8000],
                }
            )
            if attempt < max_attempts:
                if exc.kind == "empty_response":
                    recovery_instruction = (
                        LENGTH_RETRY_INSTRUCTION
                        if (exc.finish_reason or metadata.get("finish_reason"))
                        == "length"
                        else EMPTY_RETRY_INSTRUCTION
                    )
                delay = retry_delays[min(attempt - 1, len(retry_delays) - 1)]
                if exc.retry_after_seconds is not None:
                    delay = max(delay, exc.retry_after_seconds)
                attempt_rows[-1]["retry_delay_seconds"] = delay
                sleep_fn(delay)
                continue
            final_reason = f"{exc.kind}: {exc.detail}"
            return (
                failure_prediction(
                    task,
                    model=model,
                    run_id=run_id,
                    final_reason=final_reason,
                ),
                {
                    "sample_id": task["sample_id"],
                    "status": "FAIL_RETRIES_EXHAUSTED",
                    "request_sha256": request_fingerprint,
                    "attempts": attempt_rows,
                },
            )
        except ModelOutputFailure as exc:
            attempt_rows.append(
                {
                    "attempt": attempt,
                    "status": "FAIL_MODEL_OUTPUT",
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "error_kind": exc.kind,
                    "error": exc.detail[:1000],
                    "finish_reason": metadata.get("finish_reason", ""),
                    "usage": metadata.get("usage", {}),
                    "response_model": metadata.get("response_model", ""),
                    "reasoning_content_characters": metadata.get(
                        "reasoning_content_characters", 0
                    ),
                    "recovery_instruction_used": bool(recovery_instruction),
                    "response_content": (
                        exc.response_content or content
                    )[:8000],
                }
            )
            final_reason = f"{exc.kind}: {exc.detail}"
            return (
                failure_prediction(
                    task,
                    model=model,
                    run_id=run_id,
                    final_reason=final_reason,
                ),
                {
                    "sample_id": task["sample_id"],
                    "status": "FAIL_MODEL_OUTPUT",
                    "request_sha256": request_fingerprint,
                    "attempts": attempt_rows,
                },
            )
    raise AssertionError("retry loop ended without a result")


def run_preflight(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float,
    max_attempts: int,
    retry_delays: list[float],
    max_tokens: int = 256,
    request_fn: Callable[..., dict[str, Any]] = perform_request,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Verify that a route can return a tiny model-visible JSON object."""

    system_prompt = "Return only the requested valid JSON object."
    user_payload = {
        "instruction": 'Return exactly this JSON object: {"ok":true}',
    }
    request_fingerprint = sha256_text(
        canonical_json(
            {
                "model": model,
                "system_prompt_sha256": sha256_text(system_prompt),
                "user_payload": user_payload,
                "request_options": request_options(model, max_tokens),
            }
        )
    )
    attempts = []
    recovery_instruction = ""
    for attempt in range(1, max_attempts + 1):
        started = time.monotonic()
        content = ""
        metadata: dict[str, Any] = {}
        try:
            response = request_fn(
                base_url=base_url,
                api_key=api_key,
                model=model,
                system_prompt=system_prompt,
                user_payload=user_payload,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
                recovery_instruction=recovery_instruction,
            )
            content, metadata = response_content(response)
            parsed = extract_json_object(content)
            if parsed != {"ok": True}:
                raise ModelOutputFailure(
                    "preflight_contract",
                    'preflight response is not exactly {"ok":true}',
                    response_content=content[:1000],
                )
            attempts.append(
                {
                    "attempt": attempt,
                    "status": "PASS",
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "finish_reason": metadata["finish_reason"],
                    "usage": metadata["usage"],
                    "response_model": metadata["response_model"],
                    "response_id": metadata["response_id"],
                    "reasoning_content_characters": metadata[
                        "reasoning_content_characters"
                    ],
                    "recovery_instruction_used": bool(recovery_instruction),
                    "response_content": content[:1000],
                }
            )
            return {
                "status": "PASS",
                "request_sha256": request_fingerprint,
                "attempts": attempts,
            }
        except (RetryableAttempt, ModelOutputFailure) as exc:
            finish_reason = (
                exc.finish_reason
                if isinstance(exc, RetryableAttempt)
                else metadata.get("finish_reason", "")
            )
            reasoning_characters = (
                exc.reasoning_content_characters
                if isinstance(exc, RetryableAttempt)
                else metadata.get("reasoning_content_characters", 0)
            )
            attempts.append(
                {
                    "attempt": attempt,
                    "status": "FAIL_RETRYABLE",
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "error_kind": exc.kind,
                    "error": exc.detail[:1000],
                    "http_status": (
                        exc.http_status
                        if isinstance(exc, RetryableAttempt)
                        else None
                    ),
                    "finish_reason": finish_reason,
                    "usage": (
                        exc.usage
                        if isinstance(exc, RetryableAttempt) and exc.usage
                        else metadata.get("usage", {})
                    ),
                    "response_model": (
                        exc.response_model
                        if isinstance(exc, RetryableAttempt)
                        and exc.response_model
                        else metadata.get("response_model", "")
                    ),
                    "reasoning_content_characters": reasoning_characters,
                    "recovery_instruction_used": bool(recovery_instruction),
                    "response_content": (
                        exc.response_content or content
                    )[:1000],
                }
            )
            if attempt < max_attempts:
                if exc.kind == "empty_response":
                    recovery_instruction = (
                        LENGTH_RETRY_INSTRUCTION
                        if finish_reason == "length"
                        else EMPTY_RETRY_INSTRUCTION
                    )
                delay = retry_delays[min(attempt - 1, len(retry_delays) - 1)]
                if (
                    isinstance(exc, RetryableAttempt)
                    and exc.retry_after_seconds is not None
                ):
                    delay = max(delay, exc.retry_after_seconds)
                attempts[-1]["retry_delay_seconds"] = delay
                sleep_fn(delay)
                continue
            return {
                "status": "FAIL_RETRIES_EXHAUSTED",
                "request_sha256": request_fingerprint,
                "attempts": attempts,
            }
    raise AssertionError("preflight retry loop ended without a result")


def parse_retry_delays(value: str) -> list[float]:
    try:
        delays = [float(row.strip()) for row in value.split(",") if row.strip()]
    except ValueError as exc:
        raise ValueError("retry delays must be comma-separated numbers") from exc
    if not delays or any(delay < 0 for delay in delays):
        raise ValueError("retry delays must contain non-negative numbers")
    return delays


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--raw-output", type=Path, required=True)
    parser.add_argument("--source-limit", type=int, default=0)
    parser.add_argument("--variant-indices")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--retry-delays", default="2,5")
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--preflight-max-tokens", type=int, default=256)
    parser.add_argument("--skip-preflight", action="store_true")
    args = parser.parse_args()

    try:
        if not 1 <= args.max_attempts <= 5:
            raise ValueError("max-attempts must be between 1 and 5")
        if args.source_limit < 0:
            raise ValueError("source-limit cannot be negative")
        if args.max_tokens < 256:
            raise ValueError("max-tokens must be at least 256")
        if args.preflight_max_tokens < 128:
            raise ValueError("preflight-max-tokens must be at least 128")
        if args.timeout_seconds <= 0:
            raise ValueError("timeout-seconds must be positive")
        retry_delays = parse_retry_delays(args.retry_delays)
        base_url = os.environ.get("LIBINFER_NEO_URL")
        api_key = os.environ.get("LIBINFER_SK")
        if not base_url or not api_key:
            raise FatalRunError("missing LIBINFER_NEO_URL or LIBINFER_SK")
        if args.output.exists() or args.raw_output.exists():
            raise FatalRunError("refusing to overwrite an existing output")

        tasks = read_jsonl(args.tasks)
        variants = None
        if args.variant_indices:
            variants = {
                int(row.strip())
                for row in args.variant_indices.split(",")
                if row.strip()
            }
            if not variants or min(variants) < 0:
                raise ValueError("variant-indices must be non-negative integers")
        selected = select_tasks(
            tasks,
            source_limit=args.source_limit,
            variant_indices=variants,
        )
        system_prompt = args.prompt.read_text(encoding="utf-8")
        run_id = args.run_id or (
            "task1-http-"
            + sha256_text(
                f"{args.model}|{sha256_text(system_prompt)}|{args.max_attempts}"
            )[:12]
        )
        if not SAMPLE_ID_RE.fullmatch(run_id):
            raise ValueError("run-id is not schema compatible")

        preflight = (
            {
                "status": "SKIPPED",
                "request_sha256": "",
                "attempts": [],
            }
            if args.skip_preflight
            else run_preflight(
                base_url=base_url,
                api_key=api_key,
                model=args.model,
                timeout_seconds=min(args.timeout_seconds, 120),
                max_attempts=args.max_attempts,
                retry_delays=retry_delays,
                max_tokens=args.preflight_max_tokens,
            )
        )
        predictions = []
        raw_rows = []
        audit_document = {
            "schema_version": "task1-http-harness-audit/v0.1",
            "run_id": run_id,
            "model": args.model,
            "system_prompt_sha256": sha256_text(system_prompt),
            "request_options": request_options(args.model, args.max_tokens),
            "preflight": preflight,
            "tasks": raw_rows,
        }
        write_json(args.raw_output, audit_document)
        print(
            canonical_json(
                {
                    "event": "preflight",
                    "model": args.model,
                    "status": preflight["status"],
                    "attempt_count": len(preflight["attempts"]),
                }
            ),
            flush=True,
        )
        if preflight["status"] not in {"PASS", "SKIPPED"}:
            raise FatalRunError(
                "preflight did not produce the required JSON after all attempts"
            )

        for index, task in enumerate(selected, 1):
            sample_id = str(task.get("sample_id") or "")
            if not SAMPLE_ID_RE.fullmatch(sample_id):
                raise ValueError(f"unsafe sample_id: {sample_id}")
            prediction, audit = run_task(
                task,
                base_url=base_url,
                api_key=api_key,
                model=args.model,
                run_id=run_id,
                system_prompt=system_prompt,
                max_tokens=args.max_tokens,
                timeout_seconds=args.timeout_seconds,
                max_attempts=args.max_attempts,
                retry_delays=retry_delays,
            )
            predictions.append(prediction)
            raw_rows.append(audit)
            write_jsonl(args.output, predictions)
            write_json(args.raw_output, audit_document)
            print(
                canonical_json(
                    {
                        "event": "progress",
                        "completed": index,
                        "total": len(selected),
                        "sample_id": sample_id,
                        "status": audit["status"],
                        "attempt_count": len(audit["attempts"]),
                    }
                ),
                flush=True,
            )

        retry_exhausted = sum(
            row["status"] == "FAIL_RETRIES_EXHAUSTED" for row in raw_rows
        )
        model_output_failures = sum(
            row["status"] == "FAIL_MODEL_OUTPUT" for row in raw_rows
        )
        failures = retry_exhausted + model_output_failures
        print(
            canonical_json(
                {
                    "status": (
                        "COMPLETE" if failures == 0 else "COMPLETE_WITH_FAILURES"
                    ),
                    "model": args.model,
                    "task_count": len(selected),
                    "failed_prediction_count": failures,
                    "retry_exhausted_count": retry_exhausted,
                    "model_output_failure_count": model_output_failures,
                    "max_attempts": args.max_attempts,
                    "retry_delays": retry_delays,
                }
            )
        )
        return 0
    except (FatalRunError, ValueError) as exc:
        print(
            canonical_json(
                {
                    "status": "FAIL",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1000],
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
