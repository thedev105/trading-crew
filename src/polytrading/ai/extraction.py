from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import ClassVar

from pydantic import model_validator

from polytrading.ai.models import (
    CriticalField,
    ModelCard,
    NonEmptyString,
    RuleFieldSet,
    SourceSpan,
)
from polytrading.ai.security import find_untrusted_text_markers
from polytrading.domain.models import StrictRecord

_PROHIBITED_USES = (
    "credential_access",
    "order_submission",
    "risk_limit_changes",
    "trade_approval",
)
_DATE_PATTERN = r"\b(?P<date>\d{4}-\d{2}-\d{2})\b"
_TIME_PATTERN = r"\b(?P<time>(?:[01]\d|2[0-3]):[0-5]\d)\b"
_TIMEZONE_PATTERN = (
    r"\b(?P<timezone>Europe/Berlin|America/New_York|UTC|GMT|CET|CEST|EST|EDT|PST|PDT)\b"
)
_INSTRUMENT_PATTERN = r"\b(?P<instrument>[A-Z]{2,10}(?:\d{1,2})?[-/][A-Z]{2,10}(?:\d{1,2})?)\b"
_ORACLE_PATTERN = (
    r"\b(?:according\s+to|reported\s+by|published\s+by)\s+"
    r"(?P<oracle>[A-Z][A-Za-z0-9_-]*(?:\s+[A-Z][A-Za-z0-9_-]*){0,2}?)"
    r"(?=\s+(?:on|at|for|as\s+of)\b|[,.;]|$)"
)
_OPERATOR_PATTERN = (
    r"(?P<symbol>>=|<=|(?<![<>=])>(?!=)|(?<![<>=])<(?!=))|"
    r"\b(?P<at_least>at\s+least)\b|\b(?P<at_most>at\s+most)\b|"
    r"\b(?P<above>above|greater\s+than|higher\s+than|exceeds?)\b|"
    r"\b(?P<below>below|less\s+than)\b"
)
_THRESHOLD_PATTERN = (
    r"(?<!\w)(?:"
    r"(?P<dollar>\$)\s*(?P<dollar_number>\d[\d,]*(?:\.\d+)?)|"
    r"(?P<usd>USD)\s+(?P<usd_number>\d[\d,]*(?:\.\d+)?)|"
    r"(?P<percent_number>\d[\d,]*(?:\.\d+)?)\s*(?P<percent>%)"
    r")"
)
_PRECISION_PATTERN = (
    r"(?P<decimal_phrase>rounded\s+to\s+(?P<decimal_count>\d+)\s+decimal\s+places?)|"
    r"(?P<cent_phrase>(?:rounded\s+)?to\s+the\s+nearest\s+cent)"
)
_FALLBACK_PATTERN = r"\b(?P<fallback>use\s+[A-Za-z0-9._/-]+\s+instead)\b"
_CANCELLATION_PATTERN = (
    r"\b(?P<cancellation>all\s+positions\s+are\s+refunded|"
    r"(?:the\s+)?market\s+is\s+cancell?ed|the\s+contract\s+is\s+cancell?ed)\b"
)

_PATTERN_SPEC = {
    "cancellation": _CANCELLATION_PATTERN,
    "date": _DATE_PATTERN,
    "fallback": _FALLBACK_PATTERN,
    "instrument": _INSTRUMENT_PATTERN,
    "operator": _OPERATOR_PATTERN,
    "oracle": _ORACLE_PATTERN,
    "precision": _PRECISION_PATTERN,
    "threshold": _THRESHOLD_PATTERN,
    "time": _TIME_PATTERN,
    "timezone": _TIMEZONE_PATTERN,
}
_PATTERN_HASH = hashlib.sha256(
    json.dumps(_PATTERN_SPEC, separators=(",", ":"), sort_keys=True).encode("utf-8")
).hexdigest()

_DATE_RE = re.compile(_DATE_PATTERN)
_TIME_RE = re.compile(_TIME_PATTERN)
_TIMEZONE_RE = re.compile(_TIMEZONE_PATTERN)
_INSTRUMENT_RE = re.compile(_INSTRUMENT_PATTERN)
_ORACLE_RE = re.compile(_ORACLE_PATTERN, re.IGNORECASE)
_OPERATOR_RE = re.compile(_OPERATOR_PATTERN, re.IGNORECASE)
_THRESHOLD_RE = re.compile(_THRESHOLD_PATTERN, re.IGNORECASE)
_PRECISION_RE = re.compile(_PRECISION_PATTERN, re.IGNORECASE)
_FALLBACK_RE = re.compile(_FALLBACK_PATTERN, re.IGNORECASE)
_CANCELLATION_RE = re.compile(_CANCELLATION_PATTERN, re.IGNORECASE)


@dataclass(frozen=True)
class _Candidate:
    value: str
    start_char: int
    end_char: int
    exact_text: str


class RegexExtractionResult(StrictRecord):
    fields: RuleFieldSet
    abstained: bool
    abstention_reasons: tuple[NonEmptyString, ...]
    parser_pattern_hash: NonEmptyString

    @model_validator(mode="after")
    def require_consistent_abstention(self) -> RegexExtractionResult:
        if self.abstained != bool(self.abstention_reasons):
            raise ValueError("abstention state and reasons must agree")
        return self


class RegexRuleExtractor:
    inference_cost_usd: ClassVar[Decimal] = Decimal(0)

    def extract(self, canonical_text: str) -> RegexExtractionResult:
        markers = find_untrusted_text_markers(canonical_text)
        if markers:
            return RegexExtractionResult(
                fields=_unknown_field_set(),
                abstained=True,
                abstention_reasons=("untrusted_text_markers", *markers),
                parser_pattern_hash=_PATTERN_HASH,
            )

        candidates = _extract_candidates(canonical_text)
        conflicts = tuple(
            sorted(
                field_name
                for field_name, values in candidates.items()
                if len({candidate.value for candidate in values}) > 1
            )
        )
        if conflicts:
            return RegexExtractionResult(
                fields=_unknown_field_set(),
                abstained=True,
                abstention_reasons=tuple(f"conflict:{field_name}" for field_name in conflicts),
                parser_pattern_hash=_PATTERN_HASH,
            )

        fields = _build_fields(canonical_text, candidates)
        if not any(field.status == "known" for _, field in fields):
            return RegexExtractionResult(
                fields=fields,
                abstained=True,
                abstention_reasons=("no_supported_fields",),
                parser_pattern_hash=_PATTERN_HASH,
            )
        return RegexExtractionResult(
            fields=fields,
            abstained=False,
            abstention_reasons=(),
            parser_pattern_hash=_PATTERN_HASH,
        )


def build_regex_model_card(validation_dataset_hash: str, code_revision: str) -> ModelCard:
    return ModelCard(
        schema_version=1,
        model_id="rule-regex-baseline",
        version="1.0.0",
        owner="polytrading-research",
        intended_use="offline fail-closed rule-field extraction",
        prohibited_uses=_PROHIBITED_USES,
        authority="research_only",
        implementation_kind="deterministic_baseline",
        training_cutoff=None,
        prompt_version="regex-v1",
        feature_version=_feature_version(code_revision),
        validation_dataset_hash=validation_dataset_hash,
        status="draft",
        approved_at=None,
        expires_at=None,
    )


def _extract_candidates(canonical_text: str) -> dict[str, tuple[_Candidate, ...]]:
    extracted: dict[str, list[_Candidate]] = {field: [] for field in RuleFieldSet.model_fields}

    for match in _DATE_RE.finditer(canonical_text):
        exact = match.group("date")
        try:
            normalized = date.fromisoformat(exact).isoformat()
        except ValueError:
            continue
        extracted["observation_date"].append(_group_candidate(match, "date", normalized))

    for match in _TIME_RE.finditer(canonical_text):
        extracted["observation_time"].append(_group_candidate(match, "time", match.group("time")))
    for match in _TIMEZONE_RE.finditer(canonical_text):
        extracted["timezone"].append(_group_candidate(match, "timezone", match.group("timezone")))
    for match in _INSTRUMENT_RE.finditer(canonical_text):
        extracted["source_instrument"].append(
            _group_candidate(match, "instrument", match.group("instrument"))
        )
    for match in _ORACLE_RE.finditer(canonical_text):
        extracted["oracle"].append(_group_candidate(match, "oracle", match.group("oracle")))

    for match in _OPERATOR_RE.finditer(canonical_text):
        value = _normalized_operator(match)
        candidate = _match_candidate(match, value)
        extracted["operator"].append(candidate)
        extracted["inclusivity"].append(
            _Candidate(
                value="inclusive" if value in {">=", "<="} else "exclusive",
                start_char=candidate.start_char,
                end_char=candidate.end_char,
                exact_text=candidate.exact_text,
            )
        )

    for match in _THRESHOLD_RE.finditer(canonical_text):
        number_group = next(
            group
            for group in ("dollar_number", "usd_number", "percent_number")
            if match.group(group) is not None
        )
        normalized_number = _normalize_number(match.group(number_group))
        extracted["threshold"].append(_match_candidate(match, normalized_number))
        unit_group = (
            "percent"
            if match.group("percent") is not None
            else ("dollar" if match.group("dollar") is not None else "usd")
        )
        unit = "percent" if unit_group == "percent" else "USD"
        extracted["unit"].append(_group_candidate(match, unit_group, unit))

    for match in _PRECISION_RE.finditer(canonical_text):
        if match.group("decimal_phrase") is not None:
            count = int(match.group("decimal_count"))
            candidate = _group_candidate(match, "decimal_phrase", f"{count}_decimal_places")
        else:
            candidate = _group_candidate(match, "cent_phrase", "2_decimal_places")
        extracted["precision"].append(candidate)
        extracted["rounding"].append(
            _Candidate(
                value="nearest",
                start_char=candidate.start_char,
                end_char=candidate.end_char,
                exact_text=candidate.exact_text,
            )
        )

    for match in _FALLBACK_RE.finditer(canonical_text):
        extracted["fallback_clause"].append(
            _group_candidate(match, "fallback", match.group("fallback"))
        )
    cancellation_matches = tuple(_CANCELLATION_RE.finditer(canonical_text))
    refund_matches = tuple(
        match
        for match in cancellation_matches
        if match.group("cancellation").casefold() == "all positions are refunded"
    )
    for match in refund_matches or cancellation_matches:
        extracted["cancellation_clause"].append(
            _group_candidate(match, "cancellation", match.group("cancellation"))
        )

    return {field: tuple(values) for field, values in extracted.items()}


def _normalized_operator(match: re.Match[str]) -> str:
    symbol = match.group("symbol")
    if symbol is not None:
        return symbol
    if match.group("at_least") is not None:
        return ">="
    if match.group("at_most") is not None:
        return "<="
    if match.group("above") is not None:
        return ">"
    return "<"


def _normalize_number(raw: str) -> str:
    value = Decimal(raw.replace(",", ""))
    return format(value, "f")


def _match_candidate(match: re.Match[str], value: str) -> _Candidate:
    return _Candidate(value, match.start(), match.end(), match.group(0))


def _group_candidate(match: re.Match[str], group: str, value: str) -> _Candidate:
    return _Candidate(value, match.start(group), match.end(group), match.group(group))


def _build_fields(
    canonical_text: str, candidates: dict[str, tuple[_Candidate, ...]]
) -> RuleFieldSet:
    text_hash = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    values: dict[str, CriticalField] = {}
    for field_name in RuleFieldSet.model_fields:
        matches = candidates[field_name]
        if not matches:
            values[field_name] = _unknown_field()
            continue
        value = matches[0].value
        spans = tuple(
            SourceSpan(
                start_char=match.start_char,
                end_char=match.end_char,
                exact_text=match.exact_text,
                canonical_text_hash=text_hash,
            )
            for match in matches
        )
        values[field_name] = CriticalField(
            status="known",
            value=value,
            supporting_spans=spans,
        )
    return RuleFieldSet(**values)


def _unknown_field() -> CriticalField:
    return CriticalField(status="unknown", value=None, supporting_spans=())


def _unknown_field_set() -> RuleFieldSet:
    return RuleFieldSet(**{field: _unknown_field() for field in RuleFieldSet.model_fields})


def _feature_version(code_revision: str) -> str:
    payload = json.dumps(
        {"code_revision": code_revision, "parser_pattern_hash": _PATTERN_HASH},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
