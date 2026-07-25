#!/usr/bin/env python3
"""Generate deterministic, reversible obfuscated private-message sessions.

This is stage 1 of the CovertEntryWalker pipeline. It does not browse websites,
build Docker images, call an LLM, or judge risk. Its only job is to transform a
sanitized source session while preserving a machine-verifiable provenance trail.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import copy
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Iterable
import unicodedata2 as unicodedata
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator
import idna
import regex as unicode_regex

try:
    import pypinyin
    from pypinyin import Style, pinyin
except ImportError:  # pragma: no cover - validated before generation
    pypinyin = None
    Style = None
    pinyin = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "configs/obfuscated_session_generation_v0.1.json"
DEFAULT_SOURCE_SCHEMA = PROJECT_ROOT / "schemas/obfuscated_session_source_v0.1.schema.json"
DEFAULT_RECORD_SCHEMA = PROJECT_ROOT / "schemas/obfuscated_session_generation_v0.1.schema.json"
DEFAULT_RECIPE_RECORD_SCHEMA = (
    PROJECT_ROOT / "schemas/obfuscated_session_generation_v0.2.schema.json"
)
DEFAULT_MANIFEST_SCHEMA = PROJECT_ROOT / "schemas/obfuscated_session_manifest_v0.1.schema.json"
DEFAULT_VARIANT_RECIPES_SCHEMA = (
    PROJECT_ROOT / "schemas/task1_variant_recipes_v0.1.schema.json"
)
GENERATOR_VERSION = "0.3.0"
ENTRY_PLACEHOLDER = "{ENTRY}"
RECIPE_RECORD_SCHEMA_VERSIONS = (
    "obfuscated-session-generation/v0.2",
    "obfuscated-session-generation/v0.3",
)
DEFAULT_ENTRY_REPLACEMENT_SCOPE = "FULL_ENTRY"
ENTRY_REPLACEMENT_SCOPES = ("FULL_ENTRY", "HOST_ONLY", "HOST_AND_PATH")
ENTRY_SURFACE_TYPE_LABELS = {
    "SCHEME_DEFANG": "entry_scheme_defang",
    "DROP_SCHEME": "entry_scheme_omitted",
    "DOT_DEFANG": "entry_dot_defang",
    "PUNCTUATION_DOT": "entry_punctuation_separator",
    "CHARACTER_SPACING": "entry_character_spacing",
}
ENTRY_SURFACE_RULE_GROUPS = {
    "SCHEME_DEFANG": "SCHEME",
    "DROP_SCHEME": "SCHEME",
    "DOT_DEFANG": "HOST",
    "PUNCTUATION_DOT": "HOST",
    "CHARACTER_SPACING": "HOST",
}
READING_ORDER_LAYOUT_MODES = ("VERTICAL_COLUMNS", "DIAGONAL_ACROSTIC")
PLATFORM_TOKEN_UNIT_RE = re.compile(r"\[[^\]\n]{1,32}\]")
MASKED_ENTRY_RE = re.compile(r"^\[MASKED_(?:SITE|URL|ACCOUNT|CODE)(?:_[A-Z0-9_-]+)?\]$")
MASKED_ENTRY_IN_TEXT_RE = re.compile(
    r"\[MASKED_(?:SITE|URL|ACCOUNT|CODE)(?:_[A-Z0-9_-]+)?\]"
)
ZERO_WIDTH_OR_BIDI_RE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]"
)
DEFANGED_DOT_RE = re.compile(
    r"(?i)(?:\[\s*(?:\.|dot|点)\s*\]"
    r"|\(\s*(?:\.|dot|点)\s*\)"
    r"|\{\s*(?:\.|dot|点)\s*\})"
)
SPACED_DOT_WORD_RE = re.compile(
    r"(?i)(?<=[a-z0-9-])\s+(?:dot|点)\s+(?=[a-z0-9-])"
)
URLISH_RE = re.compile(r"(?i)(?:https?|hxxps?)://[^\s<>\"']+")
DOMAIN_RE = re.compile(
    r"(?iu)(?<![@\w-])(?P<host>"
    r"(?:[^\W_](?:[^\W_]|-){0,62}\.)+"
    r"(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})"
    r")(?::\d{1,5})?"
)
EMAIL_RE = re.compile(
    r"(?iu)(?<![\w.+-])(?P<local>[a-z0-9._%+-]+)@(?P<host>"
    r"(?:[^\W_](?:[^\W_]|-){0,62}\.)+"
    r"(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})"
    r")"
)
IPV4_RE = re.compile(r"(?<![\w])(?:\d{1,3}\.){3}\d{1,3}(?![\w])")
BRACKETED_IPV6_RE = re.compile(r"\[[0-9a-f:.]{2,}\]", re.IGNORECASE)
UNBRACKETED_IPV6_RE = re.compile(
    r"(?<![\w:])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?![\w:])",
    re.IGNORECASE,
)
MOBILE_PHONE_RE = re.compile(
    r"(?<![\dA-Za-z])(?:(?:\+?86|[（(]\s*\+?86\s*[）)])[\s-]?)?"
    r"(?:[（(]\s*)?1[3-9](?:[\s-]?\d){9}(?:\s*[）)])?"
    r"(?![\dA-Za-z])"
)
BRACKETED_PHONE_RE = re.compile(
    r"(?<![\dA-Za-z])(?:(?:\+?86|[（(]\s*\+?86\s*[）)])[\s-]?)?[（(]\s*"
    r"(?:1[3-9]\d|0\d{2,3})\s*[）)]\s*[- ]*"
    r"(?:\d[\s-]?){7,8}(?![\dA-Za-z])"
)
BANK_OR_ID_NUMBER_RE = re.compile(
    r"(?<![\dA-Za-z])(?:\d[\s-]?){15,18}\d(?![\dA-Za-z])"
)
# A contextual identifier is sensitive even when it is only one or two ASCII
# characters long.  Do not use a fixed ``{3,63}`` lower bound here: short
# invite codes and group handles are common, and a bounded prefix match would
# leave the suffix of an overlong value in the model-visible projection.
#
# The cue/prefix deliberately accepts the punctuation seen in JSON exports and
# chat UIs (quotes, full-width brackets, ``为/是`` and internal ASCII spaces).
# The value is bounded only to keep pathological input inexpensive; the
# scanner below treats a value that reaches the bound as unsafe as well.
CONTEXTUAL_ACCOUNT_RE = re.compile(
    r"(?i)(?:账号|帐号|微信(?:号)?|微信群|群号|QQ(?:号|群)?|邀请码|邀请代码|"
    r"口令|暗号|卡号|银行卡(?:号)?|收款账号|account(?:_?id)?|acct|"
    r"user(?:name)?|uid|invite(?:[ _-]?code)?|"
    r"bank(?:[ _-]?card)?(?:[ _-]?number)?)"
    r"(?:\s*(?:[:：=]|为|是)\s*|\s+|\s*)"
    r"(?:[\(\[（【「『\"'“‘”’]\s*){0,2}"
    r"(?:[:：=]\s*)?"
    r"(?:[\(\[（【「『\"'“‘”’]\s*)?"
    r"(?P<value>@?[A-Za-z0-9](?:[A-Za-z0-9._+/\- ]{0,99999}[A-Za-z0-9._+/\-])?)"
    r"(?=$|[^\x00-\x7f]|[,，。；：！？、;!?()\[\]{}<>「」『』（）\"'“”‘’])"
)
ACCOUNT_HANDLE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9._-])(?P<value>"
    r"(?:qq|wx|uid)\d{5,20}|"
    r"(?:wechat|weixin|account|acct|user|group)[._-]"
    r"(?=[A-Za-z0-9._-]{4,63}(?![A-Za-z0-9._-]))"
    r"(?=[A-Za-z0-9._-]*\d)[A-Za-z0-9][A-Za-z0-9._-]{3,63}"
    r")(?![A-Za-z0-9._-])"
)
# Backwards-compatible name used by older callers.  New code should use the
# typed patterns above so ordinary dates, versions, counters, and timestamps do
# not become generic long-number false positives.
LONG_CONTACT_NUMBER_RE = re.compile(
    "(?:" + MOBILE_PHONE_RE.pattern + ")|(?:" + BRACKETED_PHONE_RE.pattern
    + ")|(?:" + BANK_OR_ID_NUMBER_RE.pattern + ")"
)
SPACED_DOMAIN_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?P<host>"
    r"(?:[a-z0-9-]\s+){2,}[a-z0-9-]\s*\.\s*"
    r"(?:[a-z0-9-]\s*){2,}"
    r")(?![a-z0-9])"
)
BASE64_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_+/=-])(?P<token>[A-Za-z0-9_+/-]{16,}={0,2})"
    r"(?![A-Za-z0-9_+/=-])"
)
UNICODE_DOMAIN_RE = unicode_regex.compile(
    r"(?iu)(?<![\p{L}\p{N}@_-])(?P<host>"
    r"(?:[\p{L}\p{N}](?:[\p{L}\p{N}-]{0,62})[.。｡﹒])+"
    r"[\p{L}\p{N}](?:[\p{L}\p{N}-]{1,62})"
    r")(?::\d{1,5})?"
)
CJK_ENTRY_CUE_RE = re.compile(
    r"(?:入口|网址|网站|域名|链接|访问|打开|前往|跳转)\s*$"
)
try:
    _TYPE_REGISTRY_FOR_PUBLIC_BOUNDARY = json.loads(
        (PROJECT_ROOT / "configs/obfuscation_type_registry_v0.2.json").read_text(
            encoding="utf-8"
        )
    )
    SAFE_DOTTED_REFERENCE_IDS = frozenset(
        str(row["type_id"])
        for row in _TYPE_REGISTRY_FOR_PUBLIC_BOUNDARY.get("types", [])
        if isinstance(row, dict) and isinstance(row.get("type_id"), str)
    )
except (OSError, UnicodeError, ValueError, TypeError):
    # Missing or malformed registry means no dotted value is exempted.
    SAFE_DOTTED_REFERENCE_IDS = frozenset()
TRAILING_URL_PUNCTUATION = ".,;:!?)]}>，。；：！？）】》"
MAX_SAFETY_DECODE_DEPTH = 2


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        value = json.loads(raw_line)
        if not isinstance(value, dict):
            raise ValueError(f"expected object at {path}:{line_number}")
        records.append(value)
    return records


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(canonical_json(value) + "\n")


class HashRandom:
    """Counter-mode SHA-256 PRNG with stable behavior across Python versions."""

    def __init__(self, material: str):
        self.seed = hashlib.sha256(material.encode("utf-8")).digest()
        self.seed_sha256 = self.seed.hex()
        self.counter = 0

    def u64(self) -> int:
        block = hashlib.sha256(
            self.seed + self.counter.to_bytes(8, byteorder="big", signed=False)
        ).digest()
        self.counter += 1
        return int.from_bytes(block[:8], byteorder="big", signed=False)

    def randbelow(self, upper: int) -> int:
        if upper <= 0:
            raise ValueError("upper must be positive")
        return self.u64() % upper

    def probability_draw(self, probability: float) -> tuple[bool, int]:
        if not 0.0 <= probability <= 1.0:
            raise ValueError(f"probability outside [0, 1]: {probability}")
        draw = self.u64()
        threshold = int(probability * (1 << 64))
        return draw < threshold, draw

    def shuffle(self, values: list[Any]) -> None:
        for index in range(len(values) - 1, 0, -1):
            other = self.randbelow(index + 1)
            values[index], values[other] = values[other], values[index]


def pinyin_tone3(value: str) -> list[str]:
    if pinyin is None or Style is None:
        raise ValueError("pypinyin is required for phonetic rule classification")
    return [
        row[0]
        for row in pinyin(
            value,
            style=Style.TONE3,
            heteronym=False,
            neutral_tone_with_five=True,
            errors=lambda text: list(text),
        )
    ]


def classify_phonetic_relation(source: str, replacement: str) -> dict[str, Any]:
    source_readings = pinyin_tone3(source)
    replacement_readings = pinyin_tone3(replacement)
    source_without_tone = [re.sub(r"[1-5]$", "", value) for value in source_readings]
    replacement_without_tone = [
        re.sub(r"[1-5]$", "", value) for value in replacement_readings
    ]
    if source_readings == replacement_readings:
        type_id = "PHON.SAME_TONE_HOMOPHONE"
    elif source_without_tone == replacement_without_tone:
        type_id = "PHON.TONE_VARIANT_HOMOPHONE"
    else:
        type_id = "PHON.NEAR_HOMOPHONE"
    return {
        "taxonomy_type_id": type_id,
        "source_pinyin_tone3": source_readings,
        "replacement_pinyin_tone3": replacement_readings,
        "source_pinyin_without_tone": source_without_tone,
        "replacement_pinyin_without_tone": replacement_without_tone,
        "pypinyin_version": pypinyin.__version__ if pypinyin is not None else "",
    }


def is_reserved_host(host: str) -> bool:
    normalized = host.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    if normalized.isdecimal():
        try:
            numeric_ipv4 = ipaddress.IPv4Address(int(normalized, 10))
        except (ipaddress.AddressValueError, ValueError):
            pass
        else:
            return numeric_ipv4.is_loopback
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        pass
    if normalized in {"example.com", "example.net", "example.org"}:
        return True
    if normalized.endswith((".example.com", ".example.net", ".example.org")):
        return True
    return normalized.endswith((".test", ".invalid", ".example"))


def normalize_safety_scan_text(
    text: str, translate_domain_separators: bool = True
) -> str:
    """Normalize common leak-evasion forms before safety scanning."""

    value = unicodedata.normalize("NFKC", text)
    for _ in range(3):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    value = ZERO_WIDTH_OR_BIDI_RE.sub("", value)
    if translate_domain_separators:
        value = value.translate(str.maketrans({"。": ".", "｡": ".", "﹒": "."}))
    value = DEFANGED_DOT_RE.sub(".", value)
    value = SPACED_DOT_WORD_RE.sub(".", value)
    value = re.sub(r"(?i)\bhxxps(?=://)", "https", value)
    value = re.sub(r"(?i)\bhxxp(?=://)", "http", value)
    return MASKED_ENTRY_IN_TEXT_RE.sub(" ", value)


def _scan_safety_findings(text: str, decode_depth: int) -> list[dict[str, Any]]:
    """Return potentially live entry/contact material after normalization."""

    normalized = normalize_safety_scan_text(text)
    separator_preserving = normalize_safety_scan_text(
        text, translate_domain_separators=False
    )
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str, start: int, end: int) -> None:
        key = (kind, value.casefold())
        if key in seen:
            return
        seen.add(key)
        findings.append(
            {
                "kind": kind,
                "value": value,
                "normalized_span": {"start": start, "end": end},
            }
        )

    for match in URLISH_RE.finditer(normalized):
        candidate = match.group(0).rstrip(TRAILING_URL_PUNCTUATION)
        try:
            parsed = urlsplit(candidate)
            host = parsed.hostname
        except ValueError:
            host = None
        if host and not is_reserved_host(host):
            host_start = match.start() + candidate.casefold().find(host.casefold())
            add("LIVE_DOMAIN", host, host_start, host_start + len(host))

    for match in EMAIL_RE.finditer(normalized):
        host = match.group("host")
        if not is_reserved_host(host):
            add("EMAIL_ADDRESS", match.group(0), match.start(), match.end())

    for pattern in (IPV4_RE, BRACKETED_IPV6_RE, UNBRACKETED_IPV6_RE):
        for match in pattern.finditer(normalized):
            candidate = match.group(0).strip("[]")
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if not address.is_loopback:
                add("IP_ADDRESS", match.group(0), match.start(), match.end())

    for match in DOMAIN_RE.finditer(normalized):
        host = match.group("host")
        if not is_reserved_host(host):
            add("LIVE_DOMAIN", host, match.start("host"), match.end("host"))

    for match in UNICODE_DOMAIN_RE.finditer(separator_preserving):
        raw_host = match.group("host")
        host = raw_host.translate(str.maketrans({"。": ".", "｡": ".", "﹒": "."}))
        if host.isascii():
            continue
        has_ascii_label_character = bool(re.search(r"[A-Za-z0-9-]", raw_host))
        separator_count = sum(character in ".。｡﹒" for character in raw_host)
        has_ascii_dot = "." in raw_host
        context = separator_preserving[max(0, match.start("host") - 12) : match.start("host")]
        has_entry_cue = bool(CJK_ENTRY_CUE_RE.search(context))
        if (
            not has_ascii_label_character
            and not has_ascii_dot
            and separator_count == 1
            and not has_entry_cue
        ):
            continue
        try:
            ascii_host = idna.encode(host, uts46=True).decode("ascii")
        except idna.IDNAError:
            continue
        if not is_reserved_host(ascii_host):
            add("LIVE_DOMAIN", host, match.start("host"), match.end("host"))

    for match in SPACED_DOMAIN_RE.finditer(normalized):
        spaced_host = match.group("host")
        compact_host = re.sub(r"\s+", "", spaced_host)
        if not is_reserved_host(compact_host):
            add("LIVE_DOMAIN", compact_host, match.start("host"), match.end("host"))

    for pattern in (MOBILE_PHONE_RE, BRACKETED_PHONE_RE):
        for match in pattern.finditer(normalized):
            digits = re.sub(r"\D", "", match.group(0))
            add("PHONE_OR_ACCOUNT_NUMBER", digits, match.start(), match.end())

    for match in BANK_OR_ID_NUMBER_RE.finditer(normalized):
        digits = re.sub(r"\D", "", match.group(0))
        add("BANK_OR_ID_NUMBER", digits, match.start(), match.end())

    for match in CONTEXTUAL_ACCOUNT_RE.finditer(normalized):
        add(
            "ACCOUNT_IDENTIFIER",
            match.group("value"),
            match.start("value"),
            match.end("value"),
        )

    for match in ACCOUNT_HANDLE_RE.finditer(normalized):
        add(
            "ACCOUNT_IDENTIFIER",
            match.group("value"),
            match.start("value"),
            match.end("value"),
        )

    if decode_depth < MAX_SAFETY_DECODE_DEPTH:
        for match in BASE64_TOKEN_RE.finditer(normalized):
            token = match.group("token")
            if len(token) > 4096:
                continue
            padded = token + "=" * (-len(token) % 4)
            try:
                decoded_bytes = base64.b64decode(
                    padded.encode("ascii"), altchars=b"-_", validate=True
                )
                decoded = decoded_bytes.decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                continue
            if not decoded or decoded == token or len(decoded) > 8192:
                continue
            nested = _scan_safety_findings(decoded, decode_depth + 1)
            if nested:
                add("ENCODED_LIVE_ENTRY", token, match.start("token"), match.end("token"))

    return sorted(
        findings,
        key=lambda item: (
            item["normalized_span"]["start"],
            item["kind"],
            item["value"],
        ),
    )


def scan_safety_findings(text: str) -> list[dict[str, Any]]:
    return _scan_safety_findings(text, 0)


def scan_public_boundary_findings(text: str) -> list[dict[str, Any]]:
    """Scan a model/public boundary while retaining frozen taxonomy IDs.

    The raw scanner intentionally treats every non-reserved dotted host as a
    potential live domain.  Structured evidence also contains registered IDs
    such as ``PLATFORM.CUSTOM_EMOJI_TOKEN``; only this exact namespace/value
    allowlist is safe to retain, never a shape-only uppercase exemption.
    """

    findings = scan_safety_findings(text)
    if text in SAFE_DOTTED_REFERENCE_IDS and all(
        row.get("kind") == "LIVE_DOMAIN" for row in findings
    ):
        return []
    return findings


def validate_safe_entry(entry: dict[str, Any]) -> None:
    value = str(entry.get("value") or "")
    entry_type = str(entry.get("type") or "")
    if MASKED_ENTRY_RE.fullmatch(value):
        return
    if entry_type != "SITE":
        raise ValueError(
            f"non-site entry must use a [MASKED_*] token, got {value!r}"
        )
    if "\\" in value or any(
        unicodedata.category(character) in {"Cc", "Cf"} for character in value
    ):
        raise ValueError("entry aliases must not contain controls or backslashes")
    normalized = unicodedata.normalize("NFKC", value)
    parse_target = (
        normalized
        if "://" in normalized or normalized.startswith("//")
        else f"//{normalized}"
    )
    try:
        parsed = urlsplit(parse_target)
        host = parsed.hostname
        _ = parsed.port
    except ValueError as error:
        raise ValueError(f"malformed entry alias: {value!r}") from error
    if parsed.scheme and parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("entry aliases only support http/https schemes")
    if parsed.username or parsed.password:
        raise ValueError("entry aliases must not contain credentials")
    if not host or not is_reserved_host(host):
        raise ValueError(
            "live entry rejected; use .test/.invalid/.example, example.com, "
            f"localhost, loopback, or [MASKED_*]: {value!r}"
        )
    findings = scan_safety_findings(normalized)
    if findings:
        finding = findings[0]
        raise ValueError(
            "embedded live URL/domain or contact in entry rejected "
            f"({finding['kind']}): {finding['value']!r}"
        )


def validate_message_urls(text: str) -> None:
    findings = scan_safety_findings(text)
    if findings:
        finding = findings[0]
        raise ValueError(
            "live URL/domain or contact in source message rejected "
            f"({finding['kind']}): {finding['value']!r}"
        )


def validate_with_schema(value: dict[str, Any], schema: dict[str, Any], label: str) -> None:
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        rendered = []
        for error in errors[:12]:
            path = ".".join(str(item) for item in error.absolute_path) or "$"
            rendered.append(f"{path}: {error.message}")
        raise ValueError(f"{label} schema validation failed: " + "; ".join(rendered))


def validate_source_record(record: dict[str, Any], source_schema: dict[str, Any]) -> None:
    validate_with_schema(record, source_schema, "source")
    validate_safe_entry(record["entry"])
    transformed_count = 0
    message_ids: set[str] = set()
    for message in record["messages"]:
        if message["message_id"] in message_ids:
            raise ValueError(
                f"duplicate message_id in {record['session_id']}: {message['message_id']}"
            )
        message_ids.add(message["message_id"])
        text = message["text"]
        validate_message_urls(text)
        if message["transform"]:
            transformed_count += 1
            count = text.count(ENTRY_PLACEHOLDER)
            if count != 1:
                raise ValueError(
                    f"{record['session_id']}/{message['message_id']} must contain "
                    f"exactly one {ENTRY_PLACEHOLDER}; found {count}"
                )
        elif ENTRY_PLACEHOLDER in text:
            raise ValueError(
                f"non-transformed message contains {ENTRY_PLACEHOLDER}: "
                f"{record['session_id']}/{message['message_id']}"
            )
    if transformed_count == 0:
        raise ValueError(f"session has no transformed message: {record['session_id']}")


def resolve_project_data_path(value: str) -> Path:
    path = (PROJECT_ROOT / value).resolve()
    try:
        path.relative_to(PROJECT_ROOT.resolve())
    except ValueError as error:
        raise ValueError(f"reference data path escapes project root: {value!r}") from error
    return path


def classify_inline_confusable(source: str, candidate: str, confusable_class: str) -> str:
    if unicodedata.normalize("NFKC", candidate).casefold() == source.casefold():
        return "UNICODE.NFKC_COMPATIBILITY"
    if "TO_GREEK" in confusable_class or "TO_CYRILLIC" in confusable_class:
        return "UNICODE.UTS39_MIXED_SCRIPT"
    return "UNICODE.UTS39_SINGLE_SCRIPT"


def materialize_confusable_rule_map(
    confusables: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    cached = confusables.get("_materialized_rule_map")
    if isinstance(cached, dict):
        return cached

    rule_map: dict[str, dict[str, Any]] = {}

    def add_candidate(
        source: str,
        value: str,
        rule_id: str,
        source_id: str,
        verification_status: str,
        taxonomy_type_id: str,
        metadata: dict[str, Any],
    ) -> None:
        target = source.casefold()
        row = rule_map.setdefault(target, {"source": target, "candidates": []})
        if any(candidate["value"] == value for candidate in row["candidates"]):
            return
        row["candidates"].append(
            {
                "value": value,
                "rule_id": rule_id,
                "source_id": source_id,
                "verification_status": verification_status,
                "taxonomy_type_id": taxonomy_type_id,
                "metadata": metadata,
            }
        )

    inline_source = confusables["source"]
    for rule in confusables["rules"]:
        for candidate in rule["candidates"]:
            add_candidate(
                rule["source"],
                candidate,
                rule["rule_id"],
                inline_source["source_id"],
                inline_source["verification_status"],
                classify_inline_confusable(
                    rule["source"], candidate, rule["confusable_class"]
                ),
                {
                    "confusable_class": rule["confusable_class"],
                    "candidate_codepoints": [
                        f"U+{ord(character):04X}" for character in candidate
                    ],
                    "reference_kind": "PROJECT_CURATED",
                },
            )

    reference = confusables.get("reference_rules")
    if reference:
        reference_path = resolve_project_data_path(reference["path"])
        actual_sha256 = sha256_file(reference_path)
        if actual_sha256 != reference["sha256"]:
            raise ValueError(
                "entry confusable reference hash mismatch: "
                f"{actual_sha256} != {reference['sha256']}"
            )
        document = read_json(reference_path)
        if document.get("mapping_id") != reference["mapping_id"]:
            raise ValueError("entry confusable reference mapping_id mismatch")
        if document.get("unicode_version") != reference["unicode_version"]:
            raise ValueError("entry confusable reference Unicode version mismatch")
        if unicodedata.unidata_version != reference["unicode_version"]:
            raise ValueError(
                "runtime Unicode database version mismatch: "
                f"{unicodedata.unidata_version} != {reference['unicode_version']}"
            )
        database = document.get("unicode_database", {})
        if database.get("implementation") != "unicodedata2" or database.get(
            "version"
        ) != reference["unicode_version"]:
            raise ValueError("entry confusable Unicode database metadata mismatch")
        source_id = document["source"]["source_id"]
        limit = int(reference.get("candidate_limit_per_target", 0))
        for target, candidates in sorted(document["mappings"].items()):
            selected = candidates[:limit] if limit > 0 else candidates
            for candidate in selected:
                add_candidate(
                    target,
                    candidate["character"],
                    f"{document['mapping_id']}:{target}",
                    source_id,
                    "UNICODE_UTS39_VERSIONED",
                    candidate["taxonomy_type_id"],
                    {
                        "confusable_class": candidate["taxonomy_type_id"],
                        "candidate_codepoints": candidate["codepoints"],
                        "candidate_unicode_name": candidate["name"],
                        "candidate_script_hint": candidate["script_hint"],
                        "candidate_nfkc": candidate["nfkc"],
                        "uts39_mapping_type": candidate["uts39_mapping_type"],
                        "unicode_version": document["unicode_version"],
                        "reference_sha256": actual_sha256,
                        "reference_kind": "UNICODE_UTS39",
                    },
                )
        if reference.get("require_ascii_a_to_z"):
            missing = [
                character
                for character in "abcdefghijklmnopqrstuvwxyz"
                if not rule_map.get(character, {}).get("candidates")
            ]
            if missing:
                raise ValueError(
                    "entry confusable reference does not cover ASCII letters: "
                    + ", ".join(missing)
                )

    confusables["_materialized_rule_map"] = rule_map
    return rule_map


def validate_config(config: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "config_id",
        "entry_policy",
        "homophone_lexicon",
        "entry_confusables",
        "emoji_insertion",
        "platform_profiles",
    }
    missing = sorted(required - set(config))
    if missing:
        raise ValueError(f"config missing keys: {', '.join(missing)}")
    if config["entry_policy"] != "RESERVED_OR_MASKED_ONLY":
        raise ValueError("entry_policy must remain RESERVED_OR_MASKED_ONLY")
    phonetic_engine = config["homophone_lexicon"].get("phonetic_engine", {})
    if phonetic_engine.get("name") != "pypinyin" or pypinyin is None:
        raise ValueError("homophone_lexicon requires the pypinyin engine")
    if pypinyin.__version__ != phonetic_engine.get("version"):
        raise ValueError(
            "pypinyin version mismatch: "
            f"{pypinyin.__version__} != {phonetic_engine.get('version')}"
        )
    materialize_confusable_rule_map(config["entry_confusables"])
    entry_replacement_scope(config["entry_confusables"])
    decomposition = config.get("glyph_decomposition")
    if decomposition is not None:
        if not decomposition.get("rules"):
            raise ValueError("glyph_decomposition has no rules")
        for rule in decomposition["rules"]:
            if len(rule["source"]) != 1:
                raise ValueError(
                    f"glyph_decomposition rule is not single-character: {rule['rule_id']}"
                )
            if any(len(value) < 2 for value in rule["candidates"]):
                raise ValueError(
                    f"glyph_decomposition candidate is not a split: {rule['rule_id']}"
                )
    similar = config.get("similar_glyph")
    if similar is not None:
        if not similar.get("rules"):
            raise ValueError("similar_glyph has no rules")
        for rule in similar["rules"]:
            if len(rule["source"]) != 1:
                raise ValueError(
                    f"similar_glyph rule is not single-character: {rule['rule_id']}"
                )
            for value in rule["candidates"]:
                if len(value) != 1:
                    raise ValueError(
                        f"similar_glyph candidate is not one character: {rule['rule_id']}"
                    )
                if value == rule["source"]:
                    raise ValueError(
                        f"similar_glyph candidate equals its source: {rule['rule_id']}"
                    )
    reading_order = config.get("reading_order_layout")
    if reading_order is not None:
        mode = reading_order.get("mode")
        if mode not in READING_ORDER_LAYOUT_MODES:
            raise ValueError(f"unsupported reading-order layout mode: {mode}")
        for key in ("columns", "diagonal_width"):
            if int(reading_order.get(key, 0)) < 2:
                raise ValueError(f"reading_order_layout.{key} must be at least 2")
        cover_characters = reading_order.get("cover_characters") or []
        if not cover_characters or any(len(value) != 1 for value in cover_characters):
            raise ValueError(
                "reading_order_layout.cover_characters must contain single characters"
            )
        if len(set(cover_characters)) != len(cover_characters):
            raise ValueError("reading_order_layout.cover_characters has duplicates")
    for platform, profile in config["platform_profiles"].items():
        if not profile.get("tokens"):
            raise ValueError(f"platform profile has no tokens: {platform}")
        if len(set(profile["tokens"])) != len(profile["tokens"]):
            raise ValueError(f"platform profile has duplicate tokens: {platform}")


VARIANT_RECIPES_SCHEMA_BY_VERSION = {
    "task1-variant-recipes/v0.1": (
        PROJECT_ROOT / "schemas/task1_variant_recipes_v0.1.schema.json"
    ),
    "task1-variant-recipes/v0.2": (
        PROJECT_ROOT / "schemas/task1_variant_recipes_v0.2.schema.json"
    ),
}


def resolve_variant_recipes_schema(
    recipe_set: dict[str, Any],
    provided: Path | None,
) -> Path:
    """Pick the recipe schema matching the set's own declared version.

    The default path cannot serve both versions, so an explicit flag still wins and
    otherwise the set selects its own schema.
    """
    if provided is not None and provided != DEFAULT_VARIANT_RECIPES_SCHEMA:
        return provided
    version = str(recipe_set.get("schema_version", ""))
    resolved = VARIANT_RECIPES_SCHEMA_BY_VERSION.get(version)
    if resolved is None:
        raise ValueError(f"unsupported variant recipe schema_version: {version}")
    return resolved


def validate_variant_recipes(
    recipe_set: dict[str, Any],
    schema: dict[str, Any],
    variants: int,
) -> dict[int, dict[str, Any]]:
    validate_with_schema(recipe_set, schema, "Task 1 variant recipes")
    rows = recipe_set["variants"]
    indices = [int(row["variant_index"]) for row in rows]
    if indices != list(range(len(rows))):
        raise ValueError("variant recipe indices must be contiguous and ordered from zero")
    if len(rows) != variants:
        raise ValueError(
            f"--variants={variants} differs from recipe count {len(rows)}"
        )
    primary_rows = [row for row in rows if row["primary_for_e2e"]]
    if len(primary_rows) != 1:
        raise ValueError("variant recipes must declare exactly one primary_for_e2e")
    if primary_rows[0]["variant_index"] != recipe_set["primary_variant_index"]:
        raise ValueError("primary_variant_index does not match primary_for_e2e")
    recipe_ids = [str(row["recipe_id"]) for row in rows]
    if len(set(recipe_ids)) != len(recipe_ids):
        raise ValueError("variant recipe IDs must be unique")
    for row in rows:
        overrides = row["overrides"]
        emoji_min = overrides.get("emoji_min_insertions")
        emoji_max = overrides.get("emoji_max_insertions")
        if emoji_min is not None and emoji_max is not None and emoji_min > emoji_max:
            raise ValueError(f"{row['recipe_id']}: emoji min exceeds max")
        chunk_min = overrides.get("emoji_chunk_min_characters")
        chunk_max = overrides.get("emoji_chunk_max_characters")
        if chunk_min is not None and chunk_max is not None and chunk_min > chunk_max:
            raise ValueError(f"{row['recipe_id']}: emoji chunk min exceeds max")
    return {int(row["variant_index"]): row for row in rows}


def apply_variant_recipe(
    base_config: dict[str, Any],
    recipe: dict[str, Any],
) -> dict[str, Any]:
    config = copy.deepcopy(base_config)
    overrides = recipe["overrides"]
    if "homophone_probability_scale" in overrides:
        scale = float(overrides["homophone_probability_scale"])
        for rule in config["homophone_lexicon"]["rules"]:
            rule["probability"] = float(rule.get("probability", 1.0)) * scale
    scalar_paths = {
        "entry_replacement_probability": (
            "entry_confusables",
            "replacement_probability",
        ),
        "entry_minimum_replacements": (
            "entry_confusables",
            "minimum_replacements",
        ),
        "entry_confusable_scope": (
            "entry_confusables",
            "replacement_scope",
        ),
        "glyph_decomposition_probability": (
            "glyph_decomposition",
            "probability",
        ),
        "similar_glyph_probability": (
            "similar_glyph",
            "probability",
        ),
        "redundant_insertion_probability": (
            "redundant_insertion",
            "probability",
        ),
        "entry_surface_probability": (
            "entry_surface",
            "probability",
        ),
        "reading_order_probability": (
            "reading_order_layout",
            "probability",
        ),
        "reading_order_mode": (
            "reading_order_layout",
            "mode",
        ),
        "reading_order_columns": (
            "reading_order_layout",
            "columns",
        ),
        "reading_order_diagonal_width": (
            "reading_order_layout",
            "diagonal_width",
        ),
        "emoji_insertion_probability": (
            "emoji_insertion",
            "insertion_probability",
        ),
        "emoji_min_insertions": ("emoji_insertion", "min_insertions"),
        "emoji_max_insertions": ("emoji_insertion", "max_insertions"),
        "emoji_line_break_probability": (
            "emoji_insertion",
            "line_break_probability",
        ),
        "emoji_chunk_min_characters": (
            "emoji_insertion",
            "chunk_min_characters",
        ),
        "emoji_chunk_max_characters": (
            "emoji_insertion",
            "chunk_max_characters",
        ),
        "emoji_insertions_per_character": (
            "emoji_insertion",
            "insertions_per_character",
        ),
    }
    for override_name, (section, key) in scalar_paths.items():
        if override_name in overrides:
            value = overrides[override_name]
            if section not in config:
                if value in (0, 0.0):
                    # Pinning an absent family to zero is already satisfied, so a
                    # focused recipe can state its exclusions against any config.
                    continue
                raise ValueError(
                    f"{recipe['recipe_id']}: override {override_name} needs a config "
                    f"with a {section} section"
                )
            config[section][key] = value
    if config["emoji_insertion"]["min_insertions"] > config["emoji_insertion"][
        "max_insertions"
    ]:
        raise ValueError(f"{recipe['recipe_id']}: effective emoji min exceeds max")
    if config["emoji_insertion"]["chunk_min_characters"] > config[
        "emoji_insertion"
    ]["chunk_max_characters"]:
        raise ValueError(f"{recipe['recipe_id']}: effective chunk min exceeds max")
    validate_config(config)
    return config


def make_edit(
    start: int,
    end: int,
    before: str,
    after: str,
    operation_type: str,
    rule_id: str,
    rule_source: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "start": start,
        "end": end,
        "before": before,
        "after": after,
        "operation_type": operation_type,
        "rule_id": rule_id,
        "rule_source": rule_source,
        "metadata": metadata or {},
    }


def materialize_edits(
    input_text: str,
    edits: list[dict[str, Any]],
    operation_prefix: str,
) -> tuple[str, list[dict[str, Any]]]:
    ordered = sorted(edits, key=lambda item: (item["start"], item["end"]))
    output_parts: list[str] = []
    operations: list[dict[str, Any]] = []
    cursor = 0
    output_length = 0
    for index, edit in enumerate(ordered, 1):
        start = int(edit["start"])
        end = int(edit["end"])
        if start < cursor or end < start or end > len(input_text):
            raise ValueError(f"overlapping or invalid edit span: {start}:{end}")
        if input_text[start:end] != edit["before"]:
            raise ValueError(
                f"edit source mismatch at {start}:{end}: "
                f"{input_text[start:end]!r} != {edit['before']!r}"
            )
        untouched = input_text[cursor:start]
        output_parts.append(untouched)
        output_length += len(untouched)
        output_start = output_length
        output_parts.append(edit["after"])
        output_length += len(edit["after"])
        output_end = output_length
        operations.append(
            {
                "operation_id": f"{operation_prefix}.op{index:03d}",
                "operation_type": edit["operation_type"],
                "rule_id": edit["rule_id"],
                "rule_source": edit["rule_source"],
                "input_span": {
                    "start": start,
                    "end": end,
                    "text": edit["before"],
                },
                "output_span": {
                    "start": output_start,
                    "end": output_end,
                    "text": edit["after"],
                },
                "before": edit["before"],
                "after": edit["after"],
                "metadata": edit["metadata"],
            }
        )
        cursor = end
    output_parts.append(input_text[cursor:])
    return "".join(output_parts), operations


def reverse_operations(output_text: str, operations: list[dict[str, Any]]) -> str:
    value = output_text
    for operation in sorted(
        operations,
        key=lambda item: (
            item["output_span"]["start"],
            item["output_span"]["end"],
        ),
        reverse=True,
    ):
        start = int(operation["output_span"]["start"])
        end = int(operation["output_span"]["end"])
        if value[start:end] != operation["after"]:
            raise ValueError(
                f"reverse span mismatch for {operation['operation_id']}: "
                f"{value[start:end]!r} != {operation['after']!r}"
            )
        value = value[:start] + operation["before"] + value[end:]
    return value


def build_stage(
    stage_id: str,
    stage_index: int,
    stage_type: str,
    subject: str,
    input_text: str,
    output_text: str,
    parent_stage_ids: list[str],
    operations: list[dict[str, Any]],
    rng_seed_sha256: str | None,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    return {
        "stage_id": stage_id,
        "stage_index": stage_index,
        "stage_type": stage_type,
        "subject": subject,
        "parent_stage_ids": parent_stage_ids,
        "input_text": input_text,
        "output_text": output_text,
        "input_sha256": sha256_text(input_text),
        "output_sha256": sha256_text(output_text),
        "rng_seed_sha256": rng_seed_sha256,
        "parameters": parameters,
        "operations": operations,
    }


def replace_homophones(
    text: str,
    lexicon: dict[str, Any],
    rng: HashRandom,
    operation_prefix: str,
) -> tuple[str, list[dict[str, Any]]]:
    rules = sorted(
        lexicon["rules"],
        key=lambda rule: (-len(rule["source"]), rule["rule_id"]),
    )
    edits: list[dict[str, Any]] = []
    index = 0
    while index < len(text):
        if text.startswith(ENTRY_PLACEHOLDER, index):
            index += len(ENTRY_PLACEHOLDER)
            continue
        matched = next(
            (rule for rule in rules if text.startswith(rule["source"], index)),
            None,
        )
        if matched is None:
            index += 1
            continue
        probability = float(matched.get("probability", 1.0))
        selected, draw = rng.probability_draw(probability)
        if selected:
            candidate_index = rng.randbelow(len(matched["candidates"]))
            replacement = matched["candidates"][candidate_index]
            if replacement != matched["source"]:
                phonetic_relation = classify_phonetic_relation(
                    matched["source"], replacement
                )
                edits.append(
                    make_edit(
                        index,
                        index + len(matched["source"]),
                        matched["source"],
                        replacement,
                        "HOMOPHONE_REPLACE",
                        matched["rule_id"],
                        lexicon["source"]["source_id"],
                        {
                            "pinyin_key": matched["pinyin_key"],
                            "probability": probability,
                            "probability_draw_u64": str(draw),
                            "candidate_index": candidate_index,
                            "lexicon_verification_status": lexicon["source"][
                                "verification_status"
                            ],
                            **phonetic_relation,
                        },
                    )
                )
        index += len(matched["source"])
    return materialize_edits(text, edits, operation_prefix)


def apply_character_lexicon(
    text: str,
    lexicon: dict[str, Any],
    rng: HashRandom,
    operation_prefix: str,
    *,
    operation_type: str,
    taxonomy_type_id: str,
    metadata_fn: Any = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Rewrite characters from a scaled, hand-curated single-character lexicon.

    Shared by radical decomposition and similar-glyph substitution: both walk the
    message longest-match-first, draw per rule, and record an exactly reversible
    edit.  ``lexicon["probability"]`` scales every rule so a variant recipe can turn
    the whole family on or off with one knob.
    """
    rules = sorted(
        lexicon["rules"],
        key=lambda rule: (-len(rule["source"]), rule["rule_id"]),
    )
    probability_scale = float(lexicon.get("probability", 0.0))
    edits: list[dict[str, Any]] = []
    index = 0
    while index < len(text):
        if text.startswith(ENTRY_PLACEHOLDER, index):
            index += len(ENTRY_PLACEHOLDER)
            continue
        matched = next(
            (rule for rule in rules if text.startswith(rule["source"], index)),
            None,
        )
        if matched is None:
            index += 1
            continue
        probability = float(matched.get("probability", 1.0)) * probability_scale
        selected, draw = rng.probability_draw(probability)
        if selected:
            candidate_index = rng.randbelow(len(matched["candidates"]))
            replacement = matched["candidates"][candidate_index]
            if replacement != matched["source"]:
                edits.append(
                    make_edit(
                        index,
                        index + len(matched["source"]),
                        matched["source"],
                        replacement,
                        operation_type,
                        matched["rule_id"],
                        lexicon["source"]["source_id"],
                        {
                            "probability": probability,
                            "probability_draw_u64": str(draw),
                            "candidate_index": candidate_index,
                            "taxonomy_type_id": taxonomy_type_id,
                            "lexicon_verification_status": lexicon["source"][
                                "verification_status"
                            ],
                            **(
                                metadata_fn(lexicon, matched["source"], replacement)
                                if metadata_fn
                                else {}
                            ),
                        },
                    )
                )
        index += len(matched["source"])
    return materialize_edits(text, edits, operation_prefix)


def decompose_glyphs(
    text: str,
    lexicon: dict[str, Any],
    rng: HashRandom,
    operation_prefix: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Split single characters into the component pair typed on Chinese platforms.

    The mapping is hand-curated rather than derived from Unicode IDS, so each edit
    records the rule that produced it and stays exactly reversible.
    """
    return apply_character_lexicon(
        text,
        lexicon,
        rng,
        operation_prefix,
        operation_type="GLYPH_DECOMPOSE",
        taxonomy_type_id="LEX.RADICAL_DECOMPOSITION",
        metadata_fn=lambda _lexicon, source, replacement: {
            "component_sequence": list(replacement),
            "component_count": len(replacement),
        },
    )


def replace_similar_glyphs(
    text: str,
    lexicon: dict[str, Any],
    rng: HashRandom,
    operation_prefix: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Swap a character for a hand-verified visual lookalike.

    Visual similarity is font-dependent, so the shipped table is hand-verified and
    each edit records that basis; a font-derived table can replace it by setting
    ``similarity_basis`` without changing this operator.
    """
    return apply_character_lexicon(
        text,
        lexicon,
        rng,
        operation_prefix,
        operation_type="SIMILAR_GLYPH_REPLACE",
        taxonomy_type_id="LEX.SIMILAR_HAN_GLYPH",
        metadata_fn=lambda lexicon, source, replacement: {
            "similarity_basis": lexicon.get("similarity_basis", "HAND_VERIFIED"),
            "glyph_relation": f"{source}~{replacement}",
        },
    )


def entry_replacement_scope(confusables: dict[str, Any]) -> str:
    scope = str(
        confusables.get("replacement_scope", DEFAULT_ENTRY_REPLACEMENT_SCOPE)
    )
    if scope not in ENTRY_REPLACEMENT_SCOPES:
        raise ValueError(f"unsupported entry replacement_scope: {scope}")
    return scope


def entry_replacement_plan(entry: str, scope: str) -> dict[str, Any]:
    """Resolve ``scope`` against ``entry`` and report what was actually used.

    A narrowed scope silently degrades to the whole string on entries with no URL
    shape (account handles, masked placeholders), so the effective scope is reported
    separately from the requested one.
    """
    start, end = entry_replacement_span(entry, scope)
    effective = scope
    if scope != "FULL_ENTRY" and (start, end) == (0, len(entry)):
        effective = "FULL_ENTRY"
    return {
        "requested_scope": scope,
        "effective_scope": effective,
        "span_start": start,
        "span_end": end,
    }


def entry_replacement_span(entry: str, scope: str) -> tuple[int, int]:
    """Return the ``[start, end)`` span of the entry eligible for replacement.

    Observed evaders disguise the host and leave the scheme readable, so a
    host-scoped span keeps ``http`` out of the substitution set.  Entries without a
    URL shape (account handles, access codes) fall back to the whole string.
    """
    if scope == "FULL_ENTRY":
        return 0, len(entry)
    parts = urlsplit(entry)
    if parts.scheme and "//" in entry:
        host_start = entry.index("//") + 2
        host_end = host_start + len(parts.netloc)
    else:
        host_start = 0
        host_end = entry.index("/") if "/" in entry else len(entry)
    if host_end <= host_start:
        return 0, len(entry)
    if scope == "HOST_AND_PATH":
        return host_start, len(entry)
    return host_start, host_end


def obfuscate_entry(
    entry: str,
    confusables: dict[str, Any],
    rng: HashRandom,
    operation_prefix: str,
) -> tuple[str, list[dict[str, Any]]]:
    rules = materialize_confusable_rule_map(confusables)
    probability = float(confusables["replacement_probability"])
    scope = entry_replacement_scope(confusables)
    span_start, span_end = entry_replacement_span(entry, scope)
    eligible: list[tuple[int, dict[str, Any], int, bool]] = []
    selected_positions: set[int] = set()
    for index, character in enumerate(entry):
        if not span_start <= index < span_end:
            continue
        rule = rules.get(character.lower())
        if rule is None:
            continue
        selected, draw = rng.probability_draw(probability)
        eligible.append((index, rule, draw, selected))
        if selected:
            selected_positions.add(index)

    minimum = min(int(confusables["minimum_replacements"]), len(eligible))
    if len(selected_positions) < minimum:
        remaining = [row[0] for row in eligible if row[0] not in selected_positions]
        rng.shuffle(remaining)
        selected_positions.update(remaining[: minimum - len(selected_positions)])
    if int(confusables["minimum_replacements"]) > 0 and not selected_positions:
        # A narrowed scope can leave no eligible character (numeric host, masked
        # placeholder).  Passing the entry through verbatim would leak the gold entry
        # into the model-visible message, so fail loudly instead.
        raise ValueError(
            f"entry has no replaceable character within scope {scope}: {entry}"
        )

    edits: list[dict[str, Any]] = []
    for index, rule, draw, originally_selected in eligible:
        if index not in selected_positions:
            continue
        candidate_index = rng.randbelow(len(rule["candidates"]))
        candidate = rule["candidates"][candidate_index]
        replacement = candidate["value"]
        source_character = entry[index]
        if source_character.isupper():
            replacement = replacement.upper()
        edits.append(
            make_edit(
                index,
                index + 1,
                source_character,
                replacement,
                "ENTRY_CONFUSABLE_REPLACE",
                candidate["rule_id"],
                candidate["source_id"],
                {
                    "probability": probability,
                    "probability_draw_u64": str(draw),
                    "candidate_index": candidate_index,
                    "forced_to_minimum": not originally_selected,
                    "confusable_class": candidate["metadata"]["confusable_class"],
                    "taxonomy_type_id": candidate["taxonomy_type_id"],
                    "mapping_verification_status": candidate["verification_status"],
                    **candidate["metadata"],
                },
            )
        )
    return materialize_edits(entry, edits, operation_prefix)


def insert_redundant_characters(
    text: str,
    settings: dict[str, Any],
    rng: HashRandom,
    operation_prefix: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Interleave punctuation or whitespace between characters.

    This is the non-emoji half of the redundant-insertion family: the inserted
    character carries no meaning, so removing it restores the canonical text.
    """
    markers = settings["markers"]
    probability = float(settings.get("probability", 0.0))
    maximum = int(settings.get("max_insertions", 0))
    # Insertion at index i splits text[i-1] and text[i], so any index strictly inside
    # the entry placeholder would corrupt it before the compose stage.
    forbidden: set[int] = set()
    search = text.find(ENTRY_PLACEHOLDER)
    while search >= 0:
        forbidden.update(range(search + 1, search + len(ENTRY_PLACEHOLDER)))
        search = text.find(ENTRY_PLACEHOLDER, search + len(ENTRY_PLACEHOLDER))
    edits: list[dict[str, Any]] = []
    for index in range(1, len(text)):
        if len(edits) >= maximum:
            break
        if index in forbidden:
            continue
        window = text[index - 1 : index + 1]
        if window.strip() != window:
            continue
        accepted, draw = rng.probability_draw(probability)
        if not accepted:
            continue
        marker_index = rng.randbelow(len(markers))
        marker = markers[marker_index]
        edits.append(
            make_edit(
                index,
                index,
                "",
                marker["value"],
                "REDUNDANT_CHARACTER_INSERT",
                marker["rule_id"],
                settings["source"]["source_id"],
                {
                    "probability": probability,
                    "probability_draw_u64": str(draw),
                    "marker_index": marker_index,
                    "marker_class": marker["marker_class"],
                    "taxonomy_type_id": marker["taxonomy_type_id"],
                },
            )
        )
    return materialize_edits(text, edits, operation_prefix)


def alter_entry_surface(
    entry: str,
    settings: dict[str, Any],
    rng: HashRandom,
    operation_prefix: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Defang the scheme, defang dots, or space out the host.

    These are the link-surface alterations seen in the wild; each rule declares the
    taxonomy type it instantiates so the record maps back to the registry.
    """
    edits: list[dict[str, Any]] = []
    scale = float(settings.get("probability", 0.0))
    # Rules within a group compete for the same span, so at most one of each group
    # applies; otherwise the later one would always be dropped as an overlap.
    applied_groups: set[str] = set()
    for rule in settings["rules"]:
        kind = rule["rule_kind"]
        group = ENTRY_SURFACE_RULE_GROUPS.get(kind)
        if group is None:
            raise ValueError(f"unsupported entry surface rule_kind: {kind}")
        if group in applied_groups:
            continue
        probability = float(rule.get("probability", 0.0)) * scale
        if probability <= 0:
            continue
        accepted, draw = rng.probability_draw(probability)
        if not accepted:
            continue
        if kind == "SCHEME_DEFANG":
            spans = [(0, len(rule["source"]))] if entry.startswith(rule["source"]) else []
            replacement = rule["replacement"]
        elif kind == "DROP_SCHEME":
            marker = rule["source"]
            spans = [(0, len(marker))] if entry.startswith(marker) else []
            replacement = ""
        elif kind in ("DOT_DEFANG", "PUNCTUATION_DOT"):
            host_start, host_end = entry_replacement_span(entry, "HOST_ONLY")
            position = entry.find(".", host_start, host_end)
            spans = [(position, position + 1)] if position >= 0 else []
            replacement = rule["replacement"]
        elif kind == "CHARACTER_SPACING":
            host_start, host_end = entry_replacement_span(entry, "HOST_ONLY")
            label = entry[host_start:host_end]
            spans = [(host_start, host_end)] if len(label) >= 2 else []
            replacement = rule["replacement"].join(label)
        else:
            raise ValueError(f"unsupported entry surface rule_kind: {kind}")
        if spans:
            applied_groups.add(group)
        for start, end in spans:
            edits.append(
                make_edit(
                    start,
                    end,
                    entry[start:end],
                    replacement,
                    "ENTRY_SURFACE_ALTER",
                    rule["rule_id"],
                    settings["source"]["source_id"],
                    {
                        "probability": probability,
                        "probability_draw_u64": str(draw),
                        "rule_kind": kind,
                        "taxonomy_type_id": rule["taxonomy_type_id"],
                    },
                )
            )
    edits.sort(key=lambda edit: (edit["start"], edit["end"]))
    filtered: list[dict[str, Any]] = []
    for edit in edits:
        if filtered and edit["start"] < filtered[-1]["end"]:
            # Overlapping surface rules would not be independently reversible.
            continue
        filtered.append(edit)
    return materialize_edits(entry, filtered, operation_prefix)


def compose_entry(
    template: str,
    obfuscated_entry: str,
    operation_prefix: str,
) -> tuple[str, list[dict[str, Any]]]:
    if template.count(ENTRY_PLACEHOLDER) != 1:
        raise ValueError(f"compose input must contain exactly one {ENTRY_PLACEHOLDER}")
    start = template.index(ENTRY_PLACEHOLDER)
    edit = make_edit(
        start,
        start + len(ENTRY_PLACEHOLDER),
        ENTRY_PLACEHOLDER,
        obfuscated_entry,
        "COMPOSE_ENTRY",
        "compose-entry-v0.1",
        "PROJECT_PIPELINE",
        {"placeholder": ENTRY_PLACEHOLDER},
    )
    return materialize_edits(template, [edit], operation_prefix)


def emoji_boundaries(text: str, rng: HashRandom, chunk_min: int, chunk_max: int) -> list[int]:
    if not text:
        return []
    boundaries = [0]
    position = 0
    while position < len(text):
        width = chunk_min + rng.randbelow(chunk_max - chunk_min + 1)
        position = min(len(text), position + width)
        if position < len(text):
            boundaries.append(position)
    return sorted(set(boundaries))


def insert_platform_emojis(
    text: str,
    profile: dict[str, Any],
    settings: dict[str, Any],
    rng: HashRandom,
    operation_prefix: str,
) -> tuple[str, list[dict[str, Any]]]:
    boundaries = emoji_boundaries(
        text,
        rng,
        int(settings["chunk_min_characters"]),
        int(settings["chunk_max_characters"]),
    )
    selected: list[tuple[int, int]] = []
    rejected: list[tuple[int, int]] = []
    probability = float(settings["insertion_probability"])
    for position in boundaries:
        accepted, draw = rng.probability_draw(probability)
        (selected if accepted else rejected).append((position, draw))

    # A per-character budget keeps density constant as the message grows; the flat
    # cap alone lets a long message dilute to a fraction of an emoji per character.
    per_character = float(settings.get("insertions_per_character", 0.0))
    budget = int(settings["max_insertions"])
    if per_character > 0:
        budget = max(budget, math.ceil(len(text) * per_character))
    maximum = min(budget, len(boundaries))
    minimum = min(int(settings["min_insertions"]), maximum)
    if len(selected) > maximum:
        rng.shuffle(selected)
        rejected.extend(selected[maximum:])
        selected = selected[:maximum]
    if len(selected) < minimum:
        rng.shuffle(rejected)
        selected.extend(rejected[: minimum - len(selected)])

    edits: list[dict[str, Any]] = []
    for position, draw in sorted(selected):
        token_index = rng.randbelow(len(profile["tokens"]))
        token = profile["tokens"][token_index]
        token_source = profile.get("token_sources", {}).get(token, profile["source"])
        add_line_break, line_break_draw = rng.probability_draw(
            float(settings["line_break_probability"])
        )
        inserted = token + ("\n" if add_line_break else "")
        edits.append(
            make_edit(
                position,
                position,
                "",
                inserted,
                "PLATFORM_EMOJI_INSERT",
                f"{profile['profile_id']}:token:{token_index}",
                token_source["source_id"],
                {
                    "token": token,
                    "token_index": token_index,
                    "token_format": profile["token_format"],
                    "render_representation": profile.get("render_representation", "UNKNOWN"),
                    "probability": probability,
                    "probability_draw_u64": str(draw),
                    "line_break_after": add_line_break,
                    "line_break_probability_draw_u64": str(line_break_draw),
                    "profile_verification_status": profile["source"]["verification_status"],
                    "token_verification_status": token_source["verification_status"],
                    "source_url": token_source.get("source_url", ""),
                },
            )
        )
    return materialize_edits(text, edits, operation_prefix)


def reading_order_units(text: str) -> list[str]:
    """Keep bracket-style platform tokens atomic while laying out rendered text."""
    units: list[str] = []
    index = 0
    while index < len(text):
        match = PLATFORM_TOKEN_UNIT_RE.match(text, index)
        if match is not None:
            units.append(match.group(0))
            index = match.end()
        else:
            units.append(text[index])
            index += 1
    return units


def display_layout_unit(unit: str, newline_marker: str) -> str:
    if unit == "\n":
        return newline_marker
    if unit == "\r":
        return "↩"
    if unit == "\t":
        return "⇥"
    return unit


def apply_reading_order_layout(
    text: str,
    settings: dict[str, Any],
    rng: HashRandom,
    operation_prefix: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Place logical text units into a deterministic 2-D reading order.

    The visible text intentionally omits the target coordinates.  The operation
    trace retains them so benchmark generation remains exactly reversible and
    evaluators can distinguish model difficulty from an underspecified gold.
    """
    probability = float(settings.get("probability", 0.0))
    accepted, draw = rng.probability_draw(probability)
    if not accepted or not text:
        return text, []

    mode = settings["mode"]
    units = reading_order_units(text)
    separator = settings["cell_separator"]
    line_separator = settings["line_separator"]
    newline_marker = settings["newline_marker"]
    visible_units = [
        display_layout_unit(unit, newline_marker)
        for unit in units
    ]
    trace: list[dict[str, int]] = []

    if mode == "VERTICAL_COLUMNS":
        columns = min(int(settings["columns"]), len(units))
        rows = math.ceil(len(units) / columns)
        rendered_rows: list[str] = []
        for row in range(rows):
            cells: list[str] = []
            for column in range(columns):
                source_index = column * rows + row
                if source_index >= len(units):
                    continue
                cells.append(visible_units[source_index])
                trace.append(
                    {
                        "source_index": source_index,
                        "row": row,
                        "column": column,
                    }
                )
            rendered_rows.append(separator.join(cells))
        output = line_separator.join(rendered_rows)
        dimensions = {"rows": rows, "columns": columns}
        reading_order = "COLUMN_MAJOR_TOP_TO_BOTTOM_LEFT_TO_RIGHT"
        ambiguity_set = [
            reading_order,
            "ROW_MAJOR_LEFT_TO_RIGHT_TOP_TO_BOTTOM",
        ]
    elif mode == "DIAGONAL_ACROSTIC":
        width = int(settings["diagonal_width"])
        cover_characters = settings["cover_characters"]
        rendered_rows = []
        for row, visible_unit in enumerate(visible_units):
            target_column = row % width
            cells: list[str] = []
            for column in range(width):
                if column == target_column:
                    cells.append(visible_unit)
                else:
                    cells.append(
                        cover_characters[rng.randbelow(len(cover_characters))]
                    )
            rendered_rows.append(separator.join(cells))
            trace.append(
                {
                    "source_index": row,
                    "row": row,
                    "column": target_column,
                }
            )
        output = line_separator.join(rendered_rows)
        dimensions = {"rows": len(units), "columns": width}
        reading_order = "DIAGONAL_WRAP_TOP_TO_BOTTOM"
        ambiguity_set = [
            reading_order,
            "FIRST_COLUMN_ACROSTIC",
            "ROW_MAJOR_LEFT_TO_RIGHT_TOP_TO_BOTTOM",
        ]
    else:
        raise ValueError(f"unsupported reading-order layout mode: {mode}")

    edit = make_edit(
        0,
        len(text),
        text,
        output,
        "READING_ORDER_LAYOUT",
        f"{settings['settings_id']}:{mode.lower()}",
        settings["source"]["source_id"],
        {
            "probability": probability,
            "probability_draw_u64": str(draw),
            "taxonomy_type_id": "LAYOUT.ACROSTIC_OR_GRID",
            "mode": mode,
            "target_text": text,
            "layout_trace": trace,
            "reading_order": reading_order,
            "ambiguity_set": ambiguity_set,
            "unit_count": len(units),
            **dimensions,
        },
    )
    return materialize_edits(text, [edit], operation_prefix)


def transform_message(
    message: dict[str, Any],
    entry: dict[str, Any],
    config: dict[str, Any],
    profile: dict[str, Any],
    record_seed_material: str,
) -> tuple[str, dict[str, Any], set[str]]:
    message_id = message["message_id"]
    source_text = message["text"]
    stage_prefix = f"{message_id}"

    source_stage_id = f"{stage_prefix}.s0.source"
    source_stage = build_stage(
        source_stage_id,
        0,
        "SOURCE_TEMPLATE",
        "MESSAGE",
        source_text,
        source_text,
        [],
        [],
        None,
        {"entry_placeholder": ENTRY_PLACEHOLDER},
    )

    homophone_material = f"{record_seed_material}|{message_id}|homophone"
    homophone_rng = HashRandom(homophone_material)
    homophone_stage_id = f"{stage_prefix}.s1.homophone"
    homophone_text, homophone_operations = replace_homophones(
        source_text,
        config["homophone_lexicon"],
        homophone_rng,
        homophone_stage_id,
    )
    homophone_stage = build_stage(
        homophone_stage_id,
        1,
        "HOMOPHONE_REPLACEMENT",
        "MESSAGE",
        source_text,
        homophone_text,
        [source_stage_id],
        homophone_operations,
        homophone_rng.seed_sha256,
        {
            "lexicon_id": config["homophone_lexicon"]["lexicon_id"],
            "longest_match_first": True,
        },
    )

    # Optional lexical stages run between the homophone stage and the compose stage.
    # Each is skipped entirely when its probability is zero, so a config without the
    # section produces the same five-stage record as before the family was added.
    optional_lexical_stages = (
        (
            "glyph_decomposition",
            "glyph-decompose",
            f"{stage_prefix}.s5.decompose",
            5,
            "GLYPH_DECOMPOSITION",
            decompose_glyphs,
        ),
        (
            "similar_glyph",
            "similar-glyph",
            f"{stage_prefix}.s6.similar",
            6,
            "SIMILAR_GLYPH_SUBSTITUTION",
            replace_similar_glyphs,
        ),
        (
            "redundant_insertion",
            "redundant-insertion",
            f"{stage_prefix}.s7.redundant",
            7,
            "REDUNDANT_INSERTION",
            insert_redundant_characters,
        ),
    )
    lexical_stages: list[dict[str, Any]] = []
    lexical_operations: dict[str, list[dict[str, Any]]] = {}
    message_text = homophone_text
    message_stage_id = homophone_stage_id
    for (
        section,
        seed_tag,
        stage_id,
        stage_index,
        stage_type,
        operator,
    ) in optional_lexical_stages:
        lexicon = config.get(section)
        lexical_operations[section] = []
        if not lexicon or float(lexicon.get("probability", 0.0)) <= 0:
            continue
        stage_rng = HashRandom(f"{record_seed_material}|{message_id}|{seed_tag}")
        stage_text, stage_operations = operator(
            message_text,
            lexicon,
            stage_rng,
            stage_id,
        )
        lexical_stages.append(
            build_stage(
                stage_id,
                stage_index,
                stage_type,
                "MESSAGE",
                message_text,
                stage_text,
                [message_stage_id],
                stage_operations,
                stage_rng.seed_sha256,
                {
                    "lexicon_id": lexicon.get("lexicon_id")
                    or lexicon.get("settings_id", ""),
                    "longest_match_first": True,
                },
            )
        )
        lexical_operations[section] = stage_operations
        message_text = stage_text
        message_stage_id = stage_id

    entry_material = f"{record_seed_material}|{message_id}|entry-confusable"
    entry_rng = HashRandom(entry_material)
    entry_stage_id = f"{stage_prefix}.s2.entry"
    obfuscated_entry, entry_operations = obfuscate_entry(
        entry["value"],
        config["entry_confusables"],
        entry_rng,
        entry_stage_id,
    )
    entry_stage_parameters = {
        "mapping_id": config["entry_confusables"]["mapping_id"],
        "reference_mapping_id": config["entry_confusables"]
        .get("reference_rules", {})
        .get("mapping_id", ""),
        "reference_sha256": config["entry_confusables"]
        .get("reference_rules", {})
        .get("sha256", ""),
    }
    entry_scope = entry_replacement_scope(config["entry_confusables"])
    if entry_scope != DEFAULT_ENTRY_REPLACEMENT_SCOPE:
        # Recorded only when narrowed, so full-entry runs stay byte-identical to
        # datasets attested before the scope knob existed.  The effective scope is
        # recorded too because a narrowed request degrades on non-URL entries.
        entry_stage_parameters.update(
            entry_replacement_plan(entry["value"], entry_scope)
        )
    entry_stage = build_stage(
        entry_stage_id,
        2,
        "ENTRY_CONFUSABLE",
        "ENTRY",
        entry["value"],
        obfuscated_entry,
        [],
        entry_operations,
        entry_rng.seed_sha256,
        entry_stage_parameters,
    )

    entry_surface_settings = config.get("entry_surface")
    entry_surface_stage = None
    entry_surface_operations: list[dict[str, Any]] = []
    entry_text = obfuscated_entry
    entry_tail_stage_id = entry_stage_id
    if (
        entry_surface_settings
        and float(entry_surface_settings.get("probability", 0.0)) > 0
    ):
        surface_rng = HashRandom(f"{record_seed_material}|{message_id}|entry-surface")
        entry_surface_stage_id = f"{stage_prefix}.s8.entry_surface"
        entry_text, entry_surface_operations = alter_entry_surface(
            obfuscated_entry,
            entry_surface_settings,
            surface_rng,
            entry_surface_stage_id,
        )
        entry_surface_stage = build_stage(
            entry_surface_stage_id,
            8,
            "ENTRY_SURFACE_ALTERATION",
            "ENTRY",
            obfuscated_entry,
            entry_text,
            [entry_stage_id],
            entry_surface_operations,
            surface_rng.seed_sha256,
            {"settings_id": entry_surface_settings["settings_id"]},
        )
        entry_tail_stage_id = entry_surface_stage_id

    compose_stage_id = f"{stage_prefix}.s3.compose"
    composed_text, compose_operations = compose_entry(
        message_text,
        entry_text,
        compose_stage_id,
    )
    compose_stage = build_stage(
        compose_stage_id,
        3,
        "COMPOSE_TEXT_AND_ENTRY",
        "MESSAGE",
        message_text,
        composed_text,
        [message_stage_id, entry_tail_stage_id],
        compose_operations,
        None,
        {"entry_stage_id": entry_tail_stage_id},
    )

    emoji_material = f"{record_seed_material}|{message_id}|emoji"
    emoji_rng = HashRandom(emoji_material)
    emoji_stage_id = f"{stage_prefix}.s4.emoji"
    emoji_text, emoji_operations = insert_platform_emojis(
        composed_text,
        profile,
        config["emoji_insertion"],
        emoji_rng,
        emoji_stage_id,
    )
    emoji_stage = build_stage(
        emoji_stage_id,
        4,
        "PLATFORM_EMOJI_INSERTION",
        "MESSAGE",
        composed_text,
        emoji_text,
        [compose_stage_id],
        emoji_operations,
        emoji_rng.seed_sha256,
        {
            "profile_id": profile["profile_id"],
            "chunk_min_characters": config["emoji_insertion"][
                "chunk_min_characters"
            ],
            "chunk_max_characters": config["emoji_insertion"][
                "chunk_max_characters"
            ],
        },
    )

    reading_order_settings = config.get("reading_order_layout")
    reading_order_stage = None
    reading_order_operations: list[dict[str, Any]] = []
    final_text = emoji_text
    if (
        reading_order_settings
        and float(reading_order_settings.get("probability", 0.0)) > 0
    ):
        reading_order_rng = HashRandom(
            f"{record_seed_material}|{message_id}|reading-order"
        )
        reading_order_stage_id = f"{stage_prefix}.s9.reading_order"
        final_text, reading_order_operations = apply_reading_order_layout(
            emoji_text,
            reading_order_settings,
            reading_order_rng,
            reading_order_stage_id,
        )
        reading_order_stage = build_stage(
            reading_order_stage_id,
            9,
            "READING_ORDER_LAYOUT",
            "MESSAGE",
            emoji_text,
            final_text,
            [emoji_stage_id],
            reading_order_operations,
            reading_order_rng.seed_sha256,
            {
                "settings_id": reading_order_settings["settings_id"],
                "mode": reading_order_settings["mode"],
            },
        )

    obfuscation_types: set[str] = set()
    if homophone_operations:
        obfuscation_types.add("homophone_or_similar_sound")
    if lexical_operations.get("glyph_decomposition"):
        obfuscation_types.add("radical_decomposition")
    if lexical_operations.get("similar_glyph"):
        obfuscation_types.add("similar_han_glyph")
    for operation in lexical_operations.get("redundant_insertion") or []:
        if operation["metadata"]["marker_class"] == "WHITESPACE":
            obfuscation_types.add("whitespace_interleaving")
        else:
            obfuscation_types.add("punctuation_interleaving")
    for operation in entry_surface_operations:
        obfuscation_types.add(ENTRY_SURFACE_TYPE_LABELS[operation["metadata"]["rule_kind"]])
    if entry_operations:
        obfuscation_types.add("unicode_entry_confusable")
    if emoji_operations:
        obfuscation_types.add("platform_emoji_insertion")
        if any(op["metadata"]["line_break_after"] for op in emoji_operations):
            obfuscation_types.add("line_break_layout")
    if reading_order_operations:
        obfuscation_types.add("acrostic_or_grid_layout")

    stages = [source_stage, homophone_stage, entry_stage, compose_stage, emoji_stage]
    # Appended after the frozen core five so that positional record-schema
    # constraints on those five stay valid; ordering lives in parent_stage_ids.
    stages.extend(lexical_stages)
    if entry_surface_stage is not None:
        stages.append(entry_surface_stage)
    if reading_order_stage is not None:
        stages.append(reading_order_stage)
    transformation = {
        "message_id": message_id,
        "source_text_sha256": sha256_text(source_text),
        "final_text_sha256": sha256_text(final_text),
        "stages": stages,
    }
    return final_text, transformation, obfuscation_types


def generate_record(
    source: dict[str, Any],
    config: dict[str, Any],
    config_sha256: str,
    global_seed: str,
    variant_index: int,
    variant_recipe: dict[str, Any] | None = None,
    recipe_record_version: str = "obfuscated-session-generation/v0.1",
) -> dict[str, Any]:
    platform = source["platform"]
    if platform not in config["platform_profiles"]:
        raise ValueError(f"missing platform profile: {platform}")
    profile = config["platform_profiles"][platform]
    sample_id = f"{source['session_id']}--v{variant_index:03d}"
    record_seed_material = f"{global_seed}|{source['session_id']}|{variant_index}"
    record_seed_sha256 = sha256_text(record_seed_material)

    transformed_messages: list[dict[str, Any]] = []
    transformations: list[dict[str, Any]] = []
    obfuscation_types: set[str] = set()
    normalized_messages: list[dict[str, str]] = []
    for message in source["messages"]:
        transformed_message = copy.deepcopy(message)
        if message["transform"]:
            final_text, transformation, message_types = transform_message(
                message,
                source["entry"],
                config,
                profile,
                record_seed_material,
            )
            transformed_message["text"] = final_text
            transformations.append(transformation)
            obfuscation_types.update(message_types)
            normalized_text = message["text"].replace(
                ENTRY_PLACEHOLDER, source["entry"]["value"]
            )
        else:
            normalized_text = message["text"]
        transformed_messages.append(transformed_message)
        normalized_messages.append(
            {"message_id": message["message_id"], "text": normalized_text}
        )

    source_record_sha256 = sha256_text(canonical_json(source))
    record: dict[str, Any] = {
        "schema_version": recipe_record_version,
        "sample_id": sample_id,
        "session_id": source["session_id"],
        "variant_index": variant_index,
        "platform": platform,
        "surface": source["surface"],
        "client_context": copy.deepcopy(source["client_context"]),
        "risk_type": source["risk_type"],
        "intent": source["intent"],
        "entry": copy.deepcopy(source["entry"]),
        "source_provenance": copy.deepcopy(source["source_provenance"]),
        "text_generation": copy.deepcopy(source["text_generation"]),
        "generation": {
            "generator_version": GENERATOR_VERSION,
            "config_id": config["config_id"],
            "config_sha256": config_sha256,
            "global_seed": global_seed,
            "record_seed_sha256": record_seed_sha256,
            "source_record_sha256": source_record_sha256,
            "platform_profile_id": profile["profile_id"],
            "platform_profile_verification_status": profile["source"][
                "verification_status"
            ],
        },
        "safety": {
            "entry_policy": "RESERVED_OR_MASKED_ONLY",
            "contains_live_entry": False,
            "external_network_required": False,
            "downstream_execution_allowed": False,
        },
        "source_session": {"messages": copy.deepcopy(source["messages"])},
        "transformed_session": {"messages": transformed_messages},
        "message_transformations": transformations,
        "gold_reconstruction": {
            "entry_type": source["entry"]["type"],
            "entry_value": source["entry"]["value"],
            "messages": normalized_messages,
        },
        "obfuscation_types": sorted(obfuscation_types),
    }
    if variant_recipe is not None:
        record["generation"].update(
            {
                "variant_recipe_id": variant_recipe["recipe_id"],
                "variant_recipe_family": variant_recipe["family"],
                "variant_difficulty": variant_recipe["difficulty"],
                "variant_recipe_sha256": sha256_text(
                    canonical_json(variant_recipe)
                ),
                "primary_for_e2e": bool(variant_recipe["primary_for_e2e"]),
            }
        )
    record["record_sha256"] = sha256_text(canonical_json(record))
    return record


def verify_stage(stage: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if sha256_text(stage["input_text"]) != stage["input_sha256"]:
        errors.append(f"{stage['stage_id']}: input hash mismatch")
    if sha256_text(stage["output_text"]) != stage["output_sha256"]:
        errors.append(f"{stage['stage_id']}: output hash mismatch")
    try:
        recovered = reverse_operations(stage["output_text"], stage["operations"])
        if recovered != stage["input_text"]:
            errors.append(f"{stage['stage_id']}: reverse recovery mismatch")
    except ValueError as exc:
        errors.append(f"{stage['stage_id']}: {exc}")
    return errors


def verify_record(record: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        validate_with_schema(record, schema, record.get("sample_id", "record"))
    except ValueError as exc:
        errors.append(str(exc))
        return errors

    unhashed = copy.deepcopy(record)
    expected_hash = unhashed.pop("record_sha256")
    if sha256_text(canonical_json(unhashed)) != expected_hash:
        errors.append("record_sha256 mismatch")

    transformed_by_id = {
        message["message_id"]: message
        for message in record["transformed_session"]["messages"]
    }
    for transformation in record["message_transformations"]:
        stages = transformation["stages"]
        for stage in stages:
            errors.extend(verify_stage(stage))
        by_type = {stage["stage_type"]: stage for stage in stages}
        if len(by_type) != len(stages):
            # Keying by stage_type would silently hide a duplicated stage.
            errors.append(f"{transformation['message_id']}: duplicate stage_type")
            continue
        required = (
            "SOURCE_TEMPLATE",
            "HOMOPHONE_REPLACEMENT",
            "ENTRY_CONFUSABLE",
            "COMPOSE_TEXT_AND_ENTRY",
            "PLATFORM_EMOJI_INSERTION",
        )
        absent = [kind for kind in required if kind not in by_type]
        if absent:
            errors.append(
                f"{transformation['message_id']}: missing stages {', '.join(absent)}"
            )
            continue
        source = by_type["SOURCE_TEMPLATE"]
        homophone = by_type["HOMOPHONE_REPLACEMENT"]
        decompose = by_type.get("GLYPH_DECOMPOSITION")
        similar = by_type.get("SIMILAR_GLYPH_SUBSTITUTION")
        redundant = by_type.get("REDUNDANT_INSERTION")
        entry = by_type["ENTRY_CONFUSABLE"]
        entry_surface = by_type.get("ENTRY_SURFACE_ALTERATION")
        compose = by_type["COMPOSE_TEXT_AND_ENTRY"]
        emoji = by_type["PLATFORM_EMOJI_INSERTION"]
        reading_order = by_type.get("READING_ORDER_LAYOUT")
        # The message chain is source -> homophone -> [decompose] -> [similar] ->
        # [redundant] -> compose -> emoji -> [reading order]; the entry chain is
        # entry -> [entry surface] and joins at compose.
        message_chain = [homophone]
        message_chain.extend(
            stage for stage in (decompose, similar, redundant) if stage is not None
        )
        message_tail = message_chain[-1]
        entry_tail = entry_surface if entry_surface is not None else entry
        if homophone["input_text"] != source["output_text"]:
            errors.append(f"{transformation['message_id']}: source/homophone chain mismatch")
        for parent, child in zip(message_chain, message_chain[1:]):
            if child["input_text"] != parent["output_text"]:
                errors.append(
                    f"{transformation['message_id']}: "
                    f"{parent['stage_type']}/{child['stage_type']} chain mismatch"
                )
        if entry["input_text"] != record["entry"]["value"]:
            errors.append(f"{transformation['message_id']}: entry source mismatch")
        if entry_surface is not None and entry_surface["input_text"] != entry["output_text"]:
            errors.append(f"{transformation['message_id']}: entry/entry-surface chain mismatch")
        if compose["input_text"] != message_tail["output_text"]:
            errors.append(f"{transformation['message_id']}: homophone/compose chain mismatch")
        if not compose["operations"] or compose["operations"][0]["after"] != entry_tail["output_text"]:
            errors.append(f"{transformation['message_id']}: entry/compose chain mismatch")
        if emoji["input_text"] != compose["output_text"]:
            errors.append(f"{transformation['message_id']}: compose/emoji chain mismatch")
        if reading_order is not None and reading_order["input_text"] != emoji["output_text"]:
            errors.append(
                f"{transformation['message_id']}: emoji/reading-order chain mismatch"
            )
        final_stage = reading_order if reading_order is not None else emoji
        final_message = transformed_by_id.get(transformation["message_id"])
        if final_message is None or final_message["text"] != final_stage["output_text"]:
            errors.append(f"{transformation['message_id']}: final message mismatch")
        if sha256_text(final_stage["output_text"]) != transformation["final_text_sha256"]:
            errors.append(f"{transformation['message_id']}: final text hash mismatch")
    return errors


def verify_dataset(
    dataset_path: Path,
    manifest_path: Path,
    record_schema: dict[str, Any],
    source_path: Path | None = None,
    config_path: Path | None = None,
    manifest_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    records = read_jsonl(dataset_path)
    errors: list[str] = []
    try:
        validate_with_schema(
            manifest,
            manifest_schema or read_json(DEFAULT_MANIFEST_SCHEMA),
            "manifest",
        )
    except ValueError as exc:
        errors.append(str(exc))
    if sha256_file(dataset_path) != manifest.get("output_sha256"):
        errors.append("dataset file hash does not match manifest")
    if len(records) != manifest.get("record_count"):
        errors.append("record count does not match manifest")
    manifest_hashes = {
        row["sample_id"]: row["record_sha256"] for row in manifest.get("records", [])
    }
    for record in records:
        for error in verify_record(record, record_schema):
            errors.append(f"{record.get('sample_id', '<unknown>')}: {error}")
        if manifest_hashes.get(record.get("sample_id")) != record.get("record_sha256"):
            errors.append(f"{record.get('sample_id')}: manifest record hash mismatch")
    if source_path is not None and sha256_file(source_path) != manifest.get("input_sha256"):
        errors.append("source file hash does not match manifest")
    if config_path is not None and sha256_file(config_path) != manifest.get("config_sha256"):
        errors.append("config file hash does not match manifest")
    return {
        "status": "PASS" if not errors else "FAIL",
        "record_count": len(records),
        "errors": errors,
    }


def generate_dataset(
    input_path: Path,
    config_path: Path,
    output_path: Path,
    manifest_path: Path,
    source_schema_path: Path,
    record_schema_path: Path,
    global_seed: str,
    variants: int,
    manifest_schema_path: Path = DEFAULT_MANIFEST_SCHEMA,
    variant_recipes_path: Path | None = None,
    variant_recipes_schema_path: Path = DEFAULT_VARIANT_RECIPES_SCHEMA,
) -> dict[str, Any]:
    if variants <= 0:
        raise ValueError("variants must be positive")
    config = read_json(config_path)
    validate_config(config)
    source_schema = read_json(source_schema_path)
    record_schema = read_json(record_schema_path)
    manifest_schema = read_json(manifest_schema_path)
    Draft202012Validator.check_schema(source_schema)
    Draft202012Validator.check_schema(record_schema)
    Draft202012Validator.check_schema(manifest_schema)
    recipes_by_index: dict[int, dict[str, Any]] = {}
    # Derived unconditionally so a config-only run (no recipe set) is still stamped
    # with the version its record schema declares.
    expected_record_version = (
        record_schema.get("properties", {}).get("schema_version", {}).get("const")
    )
    optional_lexical_enabled = any(
        float((config.get(section) or {}).get("probability", 0.0)) > 0
        for section in (
            "glyph_decomposition",
            "similar_glyph",
            "redundant_insertion",
            "entry_surface",
            "reading_order_layout",
        )
    )
    if optional_lexical_enabled and variant_recipes_path is None:
        raise ValueError(
            "an optional v0.3 obfuscation stage is enabled, which requires a variant "
            "recipe set so the record carries variant provenance "
            "(pass --variant-recipes)"
        )
    if optional_lexical_enabled and expected_record_version not in RECIPE_RECORD_SCHEMA_VERSIONS[1:]:
        raise ValueError(
            "an optional v0.3 obfuscation stage is enabled, which needs the "
            f"{RECIPE_RECORD_SCHEMA_VERSIONS[1]} record schema "
            "(pass --record-schema schemas/obfuscated_session_generation_v0.3.schema.json)"
        )
    if variant_recipes_path is not None:
        recipe_set = read_json(variant_recipes_path)
        recipe_schema = read_json(
            resolve_variant_recipes_schema(recipe_set, variant_recipes_schema_path)
        )
        Draft202012Validator.check_schema(recipe_schema)
        recipes_by_index = validate_variant_recipes(
            recipe_set,
            recipe_schema,
            variants,
        )
        if expected_record_version not in RECIPE_RECORD_SCHEMA_VERSIONS:
            raise ValueError(
                "variant recipes require one of the record schema versions: "
                + ", ".join(RECIPE_RECORD_SCHEMA_VERSIONS)
            )
    sources = read_jsonl(input_path)
    if not sources:
        raise ValueError("input JSONL contains no source sessions")

    session_ids: set[str] = set()
    records: list[dict[str, Any]] = []
    config_sha256 = sha256_file(config_path)
    for source in sources:
        validate_source_record(source, source_schema)
        if source["session_id"] in session_ids:
            raise ValueError(f"duplicate session_id: {source['session_id']}")
        session_ids.add(source["session_id"])
        for variant_index in range(variants):
            variant_recipe = recipes_by_index.get(variant_index)
            effective_config = (
                apply_variant_recipe(config, variant_recipe)
                if variant_recipe is not None
                else config
            )
            record = generate_record(
                source,
                effective_config,
                config_sha256,
                global_seed,
                variant_index,
                variant_recipe,
                expected_record_version or "obfuscated-session-generation/v0.1",
            )
            errors = verify_record(record, record_schema)
            if errors:
                raise ValueError(
                    f"generated record failed verification {record['sample_id']}: "
                    + "; ".join(errors)
                )
            records.append(record)

    write_jsonl(output_path, records)
    manifest = {
        "schema_version": "obfuscated-session-manifest/v0.1",
        "generator_version": GENERATOR_VERSION,
        "config_id": config["config_id"],
        "global_seed": global_seed,
        "variants_per_session": variants,
        "source_session_count": len(sources),
        "record_count": len(records),
        "input_name": input_path.name,
        "input_sha256": sha256_file(input_path),
        "config_name": config_path.name,
        "config_sha256": config_sha256,
        "source_schema_sha256": sha256_file(source_schema_path),
        "record_schema_sha256": sha256_file(record_schema_path),
        "output_name": output_path.name,
        "output_sha256": sha256_file(output_path),
        "records": [
            {
                "line_number": index,
                "sample_id": record["sample_id"],
                "record_sha256": record["record_sha256"],
            }
            for index, record in enumerate(records, 1)
        ],
    }
    validate_with_schema(manifest, manifest_schema, "manifest")
    write_json(manifest_path, manifest)
    verification = verify_dataset(
        output_path,
        manifest_path,
        record_schema,
        source_path=input_path,
        config_path=config_path,
        manifest_schema=manifest_schema,
    )
    if verification["status"] != "PASS":
        raise ValueError("post-write verification failed: " + "; ".join(verification["errors"]))
    return {
        "status": "PASS",
        "source_session_count": len(sources),
        "record_count": len(records),
        "output": str(output_path),
        "manifest": str(manifest_path),
        "output_sha256": manifest["output_sha256"],
        "variant_recipe_set": (
            str(variant_recipes_path) if variant_recipes_path is not None else None
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate or verify traceable obfuscated private-message sessions."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate a deterministic JSONL dataset")
    generate.add_argument("--input", type=Path, required=True)
    generate.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--manifest", type=Path, required=True)
    generate.add_argument("--source-schema", type=Path, default=DEFAULT_SOURCE_SCHEMA)
    generate.add_argument("--record-schema", type=Path, default=DEFAULT_RECORD_SCHEMA)
    generate.add_argument("--manifest-schema", type=Path, default=DEFAULT_MANIFEST_SCHEMA)
    generate.add_argument("--seed", default="covert-entry-stage1-v0.1")
    generate.add_argument("--variants", type=int, default=1)
    generate.add_argument("--variant-recipes", type=Path)
    generate.add_argument(
        "--variant-recipes-schema",
        type=Path,
        default=DEFAULT_VARIANT_RECIPES_SCHEMA,
    )

    verify = subparsers.add_parser("verify", help="verify hashes, schemas, and reversibility")
    verify.add_argument("--dataset", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--record-schema", type=Path, default=DEFAULT_RECORD_SCHEMA)
    verify.add_argument("--manifest-schema", type=Path, default=DEFAULT_MANIFEST_SCHEMA)
    verify.add_argument("--source", type=Path)
    verify.add_argument("--config", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "generate":
            result = generate_dataset(
                args.input,
                args.config,
                args.output,
                args.manifest,
                args.source_schema,
                args.record_schema,
                args.seed,
                args.variants,
                args.manifest_schema,
                args.variant_recipes,
                args.variant_recipes_schema,
            )
        else:
            schema = read_json(args.record_schema)
            manifest_schema = read_json(args.manifest_schema)
            result = verify_dataset(
                args.dataset,
                args.manifest,
                schema,
                source_path=args.source,
                config_path=args.config,
                manifest_schema=manifest_schema,
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
