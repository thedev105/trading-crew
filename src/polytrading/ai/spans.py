from __future__ import annotations

from hashlib import sha256

from polytrading.ai.models import RuleFieldSet, SourceSpan


class SourceSpanValidationError(ValueError):
    """Base class for exact source-span validation failures."""


class SourceHashMismatchError(SourceSpanValidationError):
    def __init__(self, actual_hash: str) -> None:
        self.actual_hash = actual_hash
        super().__init__(f"canonical source hash mismatch: {actual_hash}")


class SourceSpanBoundsError(SourceSpanValidationError):
    def __init__(self, start_char: int, end_char: int) -> None:
        self.start_char = start_char
        self.end_char = end_char
        super().__init__(f"source span is out of bounds: {start_char}:{end_char}")


class SourceSpanTextMismatchError(SourceSpanValidationError):
    def __init__(self, start_char: int, end_char: int) -> None:
        self.start_char = start_char
        self.end_char = end_char
        super().__init__(f"source span text mismatch: {start_char}:{end_char}")


def validate_span(span: SourceSpan, canonical_text: str) -> None:
    actual_hash = sha256(canonical_text.encode("utf-8")).hexdigest()
    if span.canonical_text_hash != actual_hash:
        raise SourceHashMismatchError(actual_hash)
    if not 0 <= span.start_char < span.end_char <= len(canonical_text):
        raise SourceSpanBoundsError(span.start_char, span.end_char)
    if canonical_text[span.start_char : span.end_char] != span.exact_text:
        raise SourceSpanTextMismatchError(span.start_char, span.end_char)


def validate_rule_fields(fields: RuleFieldSet, canonical_text: str) -> RuleFieldSet:
    """Validate every known field before returning the immutable field set."""

    spans = tuple(
        span for _, field in fields if field.status == "known" for span in field.supporting_spans
    )
    for span in spans:
        validate_span(span, canonical_text)
    return fields
