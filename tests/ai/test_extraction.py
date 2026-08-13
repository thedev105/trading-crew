from decimal import Decimal

import pytest

from polytrading.ai.extraction import RegexRuleExtractor, build_regex_model_card
from polytrading.ai.spans import validate_rule_fields

MANIFEST_HASH = "b" * 64


def test_extracts_supported_rule_fields_with_exact_spans() -> None:
    text = (
        "BTC-USD resolves YES if the closing price according to Coinbase on 2026-08-12 "
        "at 16:00 Europe/Berlin is >= $100,000.50, rounded to 2 decimal places. "
        "If Coinbase is unavailable, use Kraken instead. "
        "If the market is cancelled, all positions are refunded."
    )

    result = RegexRuleExtractor().extract(text)

    assert result.abstained is False
    assert result.fields.source_instrument.value == "BTC-USD"
    assert result.fields.oracle.value == "Coinbase"
    assert result.fields.observation_date.value == "2026-08-12"
    assert result.fields.observation_time.value == "16:00"
    assert result.fields.timezone.value == "Europe/Berlin"
    assert result.fields.operator.value == ">="
    assert result.fields.inclusivity.value == "inclusive"
    assert result.fields.threshold.value == "100000.50"
    assert result.fields.unit.value == "USD"
    assert result.fields.precision.value == "2_decimal_places"
    assert result.fields.rounding.value == "nearest"
    assert result.fields.fallback_clause.value == "use Kraken instead"
    assert result.fields.cancellation_clause.value == "all positions are refunded"
    assert validate_rule_fields(result.fields, text) == result.fields
    for _, field in result.fields:
        if field.status == "known":
            assert field.supporting_spans
            for span in field.supporting_spans:
                assert text[span.start_char : span.end_char] == span.exact_text


@pytest.mark.parametrize(
    ("clause", "operator", "inclusivity", "threshold", "unit"),
    [
        ("The ETH price is > $2,500.", ">", "exclusive", "2500", "USD"),
        ("The ETH price is at least USD 2500.", ">=", "inclusive", "2500", "USD"),
        ("BTC dominance is below 50.5%.", "<", "exclusive", "50.5", "percent"),
        ("BTC dominance is at most 50%.", "<=", "inclusive", "50", "percent"),
    ],
)
def test_normalizes_supported_operators_currency_and_percentages(
    clause: str,
    operator: str,
    inclusivity: str,
    threshold: str,
    unit: str,
) -> None:
    result = RegexRuleExtractor().extract(clause)

    assert result.fields.operator.value == operator
    assert result.fields.inclusivity.value == inclusivity
    assert result.fields.threshold.value == threshold
    assert result.fields.unit.value == unit
    assert validate_rule_fields(result.fields, clause) == result.fields


def test_conflicting_rules_abstain_instead_of_selecting_a_clause() -> None:
    text = "The market resolves YES if BTC is > $100, but resolves NO if BTC is <= $100."

    result = RegexRuleExtractor().extract(text)

    assert result.abstained is True
    assert set(result.abstention_reasons) >= {"conflict:operator"}
    assert all(field.status == "unknown" for _, field in result.fields)


def test_unsupported_text_abstains_without_inventing_values() -> None:
    result = RegexRuleExtractor().extract("The result follows the complete official rules.")

    assert result.abstained is True
    assert result.abstention_reasons == ("no_supported_fields",)
    assert all(field.status == "unknown" for _, field in result.fields)


def test_regex_baseline_model_card_is_deterministic_draft_and_zero_provider_cost() -> None:
    card = build_regex_model_card(MANIFEST_HASH, "deadbeef")
    repeated = build_regex_model_card(MANIFEST_HASH, "deadbeef")

    assert card == repeated
    assert card.model_id == "rule-regex-baseline"
    assert card.version == "1.0.0"
    assert card.authority == "research_only"
    assert card.status == "draft"
    assert card.validation_dataset_hash == MANIFEST_HASH
    assert card.feature_version
    assert Decimal("0") == RegexRuleExtractor.inference_cost_usd
    dumped = card.model_dump_json()
    for prohibited_field in ("eligible", "equivalent", "guaranteed"):
        assert f'"{prohibited_field}"' not in dumped
