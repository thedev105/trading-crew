from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx

from polytrading.predictions.adapter import (
    PredictionAdapterBatch,
    PredictionAdapterWarning,
    PredictionCollectionGateError,
)
from polytrading.predictions.domain import (
    MarketRecord,
    PredictionRawEnvelope,
    PredictionVenue,
    RuleVersion,
)
from polytrading.predictions.manifest import VenueManifest, evaluate_collection_gate

# Limitless Exchange public API. Field names and pagination shape below were verified
# against the live OpenAPI schema surfaced at
# https://docs.limitless.exchange/api-reference/markets/browse-active and
# https://docs.limitless.exchange/api-reference/markets/get-market (both linked from
# https://docs.limitless.exchange/developers/programmatic-api) on 2026-08-23. Every
# field this adapter reads is declared here, in one place, so an unrecognized or
# missing field fails closed instead of being silently defaulted.
_BASE_URL = "https://api.limitless.exchange"
_MARKETS_ENDPOINT = "/markets/active"
_SOURCE_VERSION = "markets-active-v1"

# "limit" is documented as capped at 25; values above are rejected with 400.
_PAGE_LIMIT = 25
_MAX_PAGES = 1000

# tradeType values a "single" market row may declare. "group" (NegRisk / multi-outcome
# groups) nests its outcomes under a separate `markets` array shaped unlike a single
# market and is out of scope for this increment; such rows are treated as incomplete.
_TRADE_TYPE_CLOB = "clob"
_TRADE_TYPE_AMM = "amm"
_RECOGNIZED_TRADE_TYPES = (_TRADE_TYPE_CLOB, _TRADE_TYPE_AMM)
_SINGLE_MARKET_TYPE = "single"

# Every individual (non-group) market's token pair is documented as always YES/NO;
# there is no separate outcome-label field on the listing endpoint.
_BINARY_OUTCOMES = ("Yes", "No")

# Documented `status` values (get-market.md). RESOLVED marks a closed market; FUNDED
# and FUNDED_FLAGGED are the only states in which the market is currently tradable.
_STATUS_RESOLVED = "RESOLVED"
_TRADABLE_STATUSES = ("FUNDED", "FUNDED_FLAGGED")
_RECOGNIZED_STATUSES = ("FUNDED", "LOCKED", "RESOLVED", "FUNDED_FLAGGED", "DRAFT")

_MISSING = object()


class PaginationStalledError(RuntimeError):
    """Raised when a finite Limitless markets pagination loop cannot make progress."""


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _require_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _best_effort_identifier(row: Mapping[str, object]) -> str | None:
    for key in ("conditionId", "address", "slug"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _fallback_warning_market_id(row: Mapping[str, object]) -> str:
    identifier = row.get("id")
    if isinstance(identifier, int) and not isinstance(identifier, bool):
        return str(identifier)
    return "<unknown>"


@dataclass(frozen=True)
class _ReceivedResponse:
    endpoint: str
    payload: bytes
    document: Any
    observed_at: datetime
    received_monotonic_ns: int
    latency_ms: Decimal

    def raw_envelope(self) -> PredictionRawEnvelope:
        payload_text = self.payload.decode("utf-8")
        return PredictionRawEnvelope(
            schema_version=1,
            event_id=uuid5(
                NAMESPACE_URL,
                f"limitless:{self.endpoint}:{self.received_monotonic_ns}",
            ),
            venue=PredictionVenue.LIMITLESS,
            endpoint=self.endpoint,
            venue_timestamp=None,
            observed_at=self.observed_at,
            received_monotonic_ns=self.received_monotonic_ns,
            request_latency_ms=self.latency_ms,
            source_version=_SOURCE_VERSION,
            payload_json=payload_text,
            source_hash=sha256(payload_text.encode("utf-8")).hexdigest(),
        )


class LimitlessAdapter:
    venue = PredictionVenue.LIMITLESS

    def __init__(
        self,
        client: httpx.AsyncClient,
        utc_now: Callable[[], datetime],
        monotonic_ns: Callable[[], int],
        *,
        max_market_pages: int = _MAX_PAGES,
    ) -> None:
        if isinstance(max_market_pages, bool) or not isinstance(max_market_pages, int):
            raise TypeError("max_market_pages must be an integer")
        if max_market_pages <= 0:
            raise ValueError("max_market_pages must be positive")
        self._client = client
        self._utc_now = utc_now
        self._monotonic_ns = monotonic_ns
        self._max_market_pages = max_market_pages

    async def fetch_manifest_gated(self, manifest: VenueManifest) -> None:
        decision = evaluate_collection_gate(manifest, venue=self.venue)
        if not decision.allowed:
            raise PredictionCollectionGateError(decision.reason or "COLLECTION_NOT_PERMITTED")

    async def _get(self, params: Mapping[str, object]) -> _ReceivedResponse:
        started_ns = self._monotonic_ns()
        response = await self._client.get(f"{_BASE_URL}{_MARKETS_ENDPOINT}", params=params)
        completed_ns = self._monotonic_ns()
        response.raise_for_status()
        observed_at = self._utc_now()
        document = response.json()
        return _ReceivedResponse(
            endpoint=_MARKETS_ENDPOINT,
            payload=response.content,
            document=document,
            observed_at=observed_at,
            received_monotonic_ns=completed_ns,
            latency_ms=Decimal(completed_ns - started_ns) / Decimal(1_000_000),
        )

    async def fetch_markets(self, *, information_cutoff: datetime) -> PredictionAdapterBatch:
        raws: list[PredictionRawEnvelope] = []
        normalized: list[MarketRecord | RuleVersion] = []
        warnings: list[PredictionAdapterWarning] = []
        seen_ids: set[str] = set()
        page = 1
        for _page_number in range(self._max_market_pages):
            received = await self._get({"page": page, "limit": _PAGE_LIMIT})
            raw = received.raw_envelope()
            raws.append(raw)
            document = _require_mapping(received.document, "markets response")
            rows = _require_list(document.get("data"), "markets response 'data'")
            for value in rows:
                row = _require_mapping(value, "market row")
                outcome, row_warnings = _parse_market_row(
                    row,
                    information_cutoff=information_cutoff,
                    raw=raw,
                    seen_ids=seen_ids,
                )
                warnings.extend(row_warnings)
                if outcome is None:
                    continue
                market, rule_version = outcome
                normalized.append(market)
                normalized.append(rule_version)
            if len(rows) < _PAGE_LIMIT:
                break
            page += 1
        else:
            raise PaginationStalledError("Limitless markets pagination did not terminate")
        return PredictionAdapterBatch(
            raw=tuple(raws), normalized=tuple(normalized), warnings=tuple(warnings)
        )

    async def fetch_book_snapshot(
        self,
        market_id: str,
        outcome_token_id: str | None,
        observed_at: datetime,
        cycle_id: UUID,
    ) -> PredictionAdapterBatch:
        raise NotImplementedError(
            "limitless_endpoint_not_collected: book-snapshot collection is deferred "
            "until the Limitless venue manifest moves past READ_ONLY review"
        )

    async def fetch_trades(
        self, market_id: str, start: datetime, end: datetime, observed_at: datetime
    ) -> PredictionAdapterBatch:
        raise NotImplementedError(
            "limitless_endpoint_not_collected: trade collection is deferred until "
            "the Limitless venue manifest moves past READ_ONLY review"
        )

    async def fetch_fee_rate(
        self, market_id: str | None, observed_at: datetime
    ) -> PredictionAdapterBatch:
        raise NotImplementedError(
            "limitless_endpoint_not_collected: fee-rate collection is deferred until "
            "the Limitless venue manifest moves past READ_ONLY review"
        )


def _parse_outcome_token_ids(
    row: Mapping[str, object], *, trade_type: object
) -> tuple[str, str] | None:
    if trade_type == _TRADE_TYPE_CLOB:
        tokens = row.get("tokens")
        if not isinstance(tokens, dict):
            return None
        yes = tokens.get("yes")
        no = tokens.get("no")
        if isinstance(yes, str) and yes and isinstance(no, str) and no:
            return (yes, no)
        return None
    if trade_type == _TRADE_TYPE_AMM:
        position_ids = row.get("positionIds")
        if (
            isinstance(position_ids, list)
            and len(position_ids) == 2
            and all(isinstance(item, str) and item for item in position_ids)
        ):
            return (position_ids[0], position_ids[1])
        return None
    return None


def _parse_negative_risk(
    row: Mapping[str, object], *, identifier: str, venue: PredictionVenue, endpoint: str
) -> tuple[bool | None, str | None, PredictionAdapterWarning | None]:
    value = row.get("negRiskRequestId", _MISSING)
    if value is _MISSING:
        warning = PredictionAdapterWarning(
            code="limitless_negative_risk_unknown",
            venue=venue,
            endpoint=endpoint,
            market_id=identifier,
            message=(
                "negRiskRequestId is absent from this market row; negative-risk "
                "grouping is unknown, not defaulted"
            ),
        )
        return None, None, warning
    if value is None:
        return False, None, None
    if isinstance(value, str) and value:
        return True, value, None
    raise ValueError("market row 'negRiskRequestId' must be a non-empty string or null")


def _optional_datetime_from_ms(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("market row 'expirationTimestamp' must be an integer of milliseconds")
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _parse_market_row(
    row: Mapping[str, object],
    *,
    information_cutoff: datetime,
    raw: PredictionRawEnvelope,
    seen_ids: set[str],
) -> tuple[tuple[MarketRecord, RuleVersion] | None, tuple[PredictionAdapterWarning, ...]]:
    warnings: list[PredictionAdapterWarning] = []
    identifier = _best_effort_identifier(row)
    warning_market_id = identifier or _fallback_warning_market_id(row)
    title = row.get("title")
    trade_type = row.get("tradeType")
    market_type = row.get("marketType")

    def incomplete(message: str) -> tuple[None, tuple[PredictionAdapterWarning, ...]]:
        warnings.append(
            PredictionAdapterWarning(
                code="limitless_market_incomplete",
                venue=PredictionVenue.LIMITLESS,
                endpoint=_MARKETS_ENDPOINT,
                market_id=warning_market_id,
                message=message,
            )
        )
        return None, tuple(warnings)

    if identifier is None:
        return incomplete("market row has no usable identifier (conditionId, address, or slug)")
    if not isinstance(title, str) or not title:
        return incomplete("market row is missing its question/title")
    if trade_type not in _RECOGNIZED_TRADE_TYPES or market_type != _SINGLE_MARKET_TYPE:
        return incomplete(
            "market row is not a recognized single clob/amm market "
            f"(tradeType={trade_type!r}, marketType={market_type!r})"
        )

    if identifier in seen_ids:
        raise ValueError(f"markets page contains a duplicate identifier: {identifier!r}")
    seen_ids.add(identifier)

    status = row.get("status")
    if status not in _RECOGNIZED_STATUSES:
        raise ValueError(f"market row 'status' is not a recognized value: {status!r}")
    expired = row.get("expired")
    if not isinstance(expired, bool):
        raise ValueError("market row 'expired' must be a boolean")
    closed = status == _STATUS_RESOLVED
    active = status in _TRADABLE_STATUSES and not expired

    order_book_enabled = trade_type == _TRADE_TYPE_CLOB
    if trade_type == _TRADE_TYPE_AMM:
        warnings.append(
            PredictionAdapterWarning(
                code="limitless_amm_market",
                venue=PredictionVenue.LIMITLESS,
                endpoint=_MARKETS_ENDPOINT,
                market_id=identifier,
                message="AMM markets have no central limit order book; order_book_enabled=False",
            )
        )

    outcome_token_ids = _parse_outcome_token_ids(row, trade_type=trade_type)
    if outcome_token_ids is None:
        warnings.append(
            PredictionAdapterWarning(
                code="limitless_outcome_tokens_unavailable",
                venue=PredictionVenue.LIMITLESS,
                endpoint=_MARKETS_ENDPOINT,
                market_id=identifier,
                message="outcome token IDs were not present or not well-formed for this market",
            )
        )

    negative_risk, event_id, negative_risk_warning = _parse_negative_risk(
        row, identifier=identifier, venue=PredictionVenue.LIMITLESS, endpoint=_MARKETS_ENDPOINT
    )
    if negative_risk_warning is not None:
        warnings.append(negative_risk_warning)

    # Limitless's public markets API does not publish a per-market jurisdictional
    # restriction flag (unlike Polymarket's Gamma `restricted`); see
    # https://docs.limitless.exchange/api-reference/markets/get-market. Per spec
    # section 6.2, an unsupported venue field stays unknown rather than being
    # defaulted, so this is left None with a structured warning, never guessed.
    warnings.append(
        PredictionAdapterWarning(
            code="limitless_restricted_unknown",
            venue=PredictionVenue.LIMITLESS,
            endpoint=_MARKETS_ENDPOINT,
            market_id=identifier,
            message=(
                "Limitless does not expose a per-market jurisdictional-restriction "
                "field; restricted is unknown, not defaulted"
            ),
        )
    )

    end_at = _optional_datetime_from_ms(row.get("expirationTimestamp"))
    slug = row.get("slug")

    rule_version_id = uuid5(NAMESPACE_URL, f"limitless:{identifier}:{raw.source_hash}")
    market = MarketRecord(
        schema_version=1,
        market_id=identifier,
        venue=PredictionVenue.LIMITLESS,
        underlying_exchange=None,
        event_id=event_id,
        question=title,
        slug=slug if isinstance(slug, str) else None,
        outcomes=_BINARY_OUTCOMES,
        outcome_token_ids=outcome_token_ids,
        negative_risk=negative_risk,
        active=active,
        closed=closed,
        restricted=None,
        order_book_enabled=order_book_enabled,
        start_at=None,
        end_at=end_at,
        resolution_source=None,
        rule_version_id=rule_version_id,
        information_cutoff=information_cutoff,
        source_url=f"{_BASE_URL}{_MARKETS_ENDPOINT}",
        retrieved_at=information_cutoff,
        raw_hash=raw.source_hash,
        normalized_hash=raw.source_hash,
    )
    description = row.get("description")
    rule_version = RuleVersion(
        schema_version=1,
        rule_version_id=rule_version_id,
        market_id=identifier,
        venue=PredictionVenue.LIMITLESS,
        question=title,
        description=description if isinstance(description, str) else "",
        resolution_source=None,
        outcomes=_BINARY_OUTCOMES,
        superseded_rule_version_id=None,
        effective_at=information_cutoff,
        source_hash=raw.source_hash,
    )
    return (market, rule_version), tuple(warnings)
