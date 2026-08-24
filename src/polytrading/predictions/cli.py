from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from polytrading.http_client import make_public_http_client
from polytrading.predictions.adapter import PredictionCollectionGateError
from polytrading.predictions.attestations import RuleAttestation
from polytrading.predictions.candidates import (
    propose_binary_complements,
    propose_venue_native_outcome_sets,
)
from polytrading.predictions.candidates_models import CandidateRelationship, RelationshipType
from polytrading.predictions.dashboard_server import (
    serve_prediction_dashboard,
    validate_prediction_dashboard_database,
)
from polytrading.predictions.domain import MarketRecord, PredictionVenue, RuleVersion
from polytrading.predictions.health import PredictionHealthAuditor, VenueEvidenceStatus
from polytrading.predictions.health_report import (
    render_prediction_health_json,
    render_prediction_health_text,
)
from polytrading.predictions.kalshi import KalshiAdapter
from polytrading.predictions.limitless import LimitlessAdapter
from polytrading.predictions.manifest import evaluate_collection_gate
from polytrading.predictions.polymarket import PolymarketAdapter
from polytrading.predictions.registry import PredictionRegistry
from polytrading.predictions.storage.store import PredictionMarketStore
from polytrading.trial.writer_lease import database_writer_lease

_WRITER_LEASE_TIMEOUT_SECONDS = 30.0
_ADAPTER_BY_VENUE = {
    PredictionVenue.POLYMARKET: PolymarketAdapter,
    PredictionVenue.KALSHI: KalshiAdapter,
    PredictionVenue.LIMITLESS: LimitlessAdapter,
}
_DEFAULT_CANDIDATES_TRIAL_FAMILY_ID = "increment-2-structural"
# No CLI-invoked git-revision source exists yet for deterministic-generator provenance;
# this mirrors scout_bridge.py's `_RETRIEVAL_CODE_REVISION` fixed module constant rather
# than inventing a new pattern.
_CANDIDATES_CODE_REVISION = "unversioned"
_CROSS_VENUE_ABSTENTION_LINE = (
    "cross-venue nomination: abstained (SCOUT_GATE_UNMET: no adjudicated gold evaluation)"
)
_RULE_ATTESTATIONS_ADAPTER: TypeAdapter[list[RuleAttestation]] = TypeAdapter(
    list[RuleAttestation]
)


class PredictionsUsageError(ValueError):
    """A user-facing prediction-market CLI validation error."""


class PredictionCollectionError(RuntimeError):
    """A prediction-market collection failure, sanitized for CLI output."""


class PredictionCandidatesError(RuntimeError):
    """A prediction-market candidate-discovery failure, sanitized for CLI output."""


class PredictionAttestError(RuntimeError):
    """A prediction-market rule-attestation import failure, sanitized for CLI output."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def add_predictions_subcommands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    predictions = subparsers.add_parser("predictions", help="prediction-market evidence operations")
    predictions_commands = predictions.add_subparsers(dest="predictions_command", required=True)

    venues = predictions_commands.add_parser("venues", help="venue manifest operations")
    venues_commands = venues.add_subparsers(dest="predictions_venues_command", required=True)
    venues_status = venues_commands.add_parser(
        "status", help="show venue manifests and collection gates"
    )
    venues_status.add_argument("--db", required=True, type=Path)
    venues_status.add_argument("--format", choices=("text", "json"), default="text")

    collect = predictions_commands.add_parser(
        "collect", help="collect committed venue public evidence"
    )
    collect_commands = collect.add_subparsers(dest="predictions_collect_command", required=True)
    for name in ("polymarket", "kalshi", "limitless"):
        collector = collect_commands.add_parser(name, help=f"collect {name} public evidence")
        collector.add_argument("--db", required=True, type=Path)
        collector.add_argument(
            "--books",
            type=int,
            default=0,
            help=(
                "collect this many order-book-enabled/active/open markets' executable "
                "books and fee rates too (default 0: markets/rules only)"
            ),
        )

    health = predictions_commands.add_parser(
        "health", help="audit per-venue collection and continuity health"
    )
    health.add_argument("--db", required=True, type=Path)
    health.add_argument("--as-of")
    health.add_argument("--format", choices=("text", "json"), default="text")

    candidates = predictions_commands.add_parser(
        "candidates", help="propose and persist deterministic candidate relationships"
    )
    candidates.add_argument("--db", required=True, type=Path)
    candidates.add_argument("--venues", required=True)
    candidates.add_argument("--as-of")
    candidates.add_argument("--trial-family", default=_DEFAULT_CANDIDATES_TRIAL_FAMILY_ID)
    candidates.add_argument("--format", choices=("text", "json"), default="text")

    attest = predictions_commands.add_parser(
        "attest", help="ingest an operator-authored rule attestation file"
    )
    attest.add_argument("--db", required=True, type=Path)
    attest.add_argument("--input", required=True, type=Path)

    dashboard = predictions_commands.add_parser(
        "dashboard", help="serve the loopback-only prediction-market evidence console"
    )
    dashboard.add_argument("--db", required=True, type=Path)
    dashboard.add_argument("--port", required=True, type=int)


def run_predictions_command(arguments: argparse.Namespace) -> int:
    if arguments.predictions_command == "venues":
        return _run_venues_status(arguments)
    if arguments.predictions_command == "collect":
        return asyncio.run(_run_collect(arguments))
    if arguments.predictions_command == "candidates":
        return _run_candidates(arguments)
    if arguments.predictions_command == "attest":
        return _run_attest(arguments)
    if arguments.predictions_command == "dashboard":
        validate_prediction_dashboard_database(arguments.db)
        serve_prediction_dashboard(arguments.db, arguments.port)
        return 0
    return _run_health(arguments)


def _run_venues_status(arguments: argparse.Namespace) -> int:
    if not arguments.db.is_file():
        raise PredictionsUsageError(
            "predictions venues status database is unavailable or not current"
        )
    store = PredictionMarketStore(arguments.db, read_only=True)
    try:
        as_of = _utc_now()
        rows = []
        for venue in PredictionVenue:
            manifest = store.latest_venue_manifest_as_of(venue, as_of)
            gate = evaluate_collection_gate(manifest, venue=venue)
            rows.append(
                {
                    "venue": venue.value,
                    "implementation_state": (
                        manifest.implementation_state.value if manifest is not None else None
                    ),
                    "collection_allowed": gate.allowed,
                    "reason": gate.reason,
                }
            )
    finally:
        store.close()

    if arguments.format == "json":
        print(json.dumps({"as_of": _timestamp(as_of), "venues": rows}, indent=2, sort_keys=True))
    else:
        print(f"predictions venues status | {_timestamp(as_of)}")
        for row in rows:
            print(
                f"{row['venue']} | state={row['implementation_state']} | "
                f"collection_allowed={row['collection_allowed']} | reason={row['reason']}"
            )
    return 0


def _books_eligible(market: MarketRecord) -> bool:
    return market.order_book_enabled and market.active and not market.closed


async def _run_collect(arguments: argparse.Namespace) -> int:
    venue = PredictionVenue(arguments.predictions_collect_command)
    books = arguments.books
    if books < 0:
        raise PredictionsUsageError("--books must be zero or a positive integer")
    if books > 0 and venue is PredictionVenue.LIMITLESS:
        raise PredictionsUsageError(
            "limitless_endpoint_not_collected: books are not collected for the "
            "conditional-token limitless venue by increment-2 ruling"
        )
    adapter_cls = _ADAPTER_BY_VENUE[venue]
    try:
        with database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS):
            store = PredictionMarketStore(arguments.db)
            try:
                as_of = _utc_now()
                manifest = store.latest_venue_manifest_as_of(venue, as_of)
                gate = evaluate_collection_gate(manifest, venue=venue)
                if not gate.allowed:
                    raise PredictionsUsageError(
                        f"{venue.value} collection is not permitted: {gate.reason}"
                    )
                market_count = 0
                extra_warnings: list[object] = []
                async with make_public_http_client() as client:
                    adapter = adapter_cls(client, _utc_now, time.monotonic_ns)
                    batch = await adapter.fetch_markets(information_cutoff=as_of)
                    with store.transaction() as transaction:
                        for raw in batch.raw:
                            transaction.append_raw(raw)
                        for item in batch.normalized:
                            if isinstance(item, MarketRecord):
                                transaction.append_market(item)
                                market_count += 1
                            elif isinstance(item, RuleVersion):
                                transaction.append_rule_version(item)
                        if books > 0:
                            selected_markets = sorted(
                                (
                                    item
                                    for item in batch.normalized
                                    if isinstance(item, MarketRecord) and _books_eligible(item)
                                ),
                                key=lambda market: market.market_id,
                            )[:books]
                            cycle_id = uuid4()
                            books_observed_at = _utc_now()
                            for market in selected_markets:
                                try:
                                    token_ids = (
                                        market.outcome_token_ids
                                        if market.outcome_token_ids is not None
                                        else market.outcomes
                                    )
                                    for token_id in token_ids:
                                        book_batch = await adapter.fetch_book_snapshot(
                                            market.market_id,
                                            token_id,
                                            books_observed_at,
                                            cycle_id,
                                        )
                                        for raw in book_batch.raw:
                                            transaction.append_raw(raw)
                                        for record in book_batch.normalized:
                                            transaction.append_book_snapshot(record)
                                        extra_warnings.extend(book_batch.warnings)
                                    fee_batch = await adapter.fetch_fee_rate(
                                        market.market_id, books_observed_at
                                    )
                                    for raw in fee_batch.raw:
                                        transaction.append_raw(raw)
                                    for record in fee_batch.normalized:
                                        transaction.append_fee_rate(record)
                                    extra_warnings.extend(fee_batch.warnings)
                                except Exception as error:
                                    print(
                                        f"polytrading: warning: {venue.value} "
                                        f"{market.market_id}: book/fee collection failed: "
                                        f"{error}",
                                        file=sys.stderr,
                                    )
                for warning in (*batch.warnings, *extra_warnings):
                    print(
                        f"polytrading: warning: {warning.venue.value} {warning.code} "
                        f"{warning.market_id}: {warning.message}",
                        file=sys.stderr,
                    )
            finally:
                store.close()
    except PredictionsUsageError:
        raise
    except PredictionCollectionGateError as error:
        raise PredictionsUsageError(str(error)) from error
    except Exception as error:
        raise PredictionCollectionError(
            f"{venue.value} collection failed to persist durably"
        ) from error
    print(f"collected {market_count} {venue.value} markets")
    return 0


def _run_health(arguments: argparse.Namespace) -> int:
    if not arguments.db.is_file():
        raise PredictionsUsageError("predictions health database is unavailable or not current")
    as_of = _parse_timestamp(arguments.as_of) if arguments.as_of else _utc_now()
    store = PredictionMarketStore(arguments.db, read_only=True)
    try:
        report = PredictionHealthAuditor(store).audit(as_of)
    finally:
        store.close()

    renderer = (
        render_prediction_health_json
        if arguments.format == "json"
        else render_prediction_health_text
    )
    print(renderer(report))
    return (
        0
        if all(
            venue.status in (VenueEvidenceStatus.CURRENT, VenueEvidenceStatus.STALE)
            for venue in report.venues
        )
        else 1
    )


def _parse_venues(value: str) -> tuple[PredictionVenue, ...]:
    names = [item.strip() for item in value.split(",") if item.strip()]
    if not names:
        raise PredictionsUsageError("--venues requires at least one venue name")
    venues: list[PredictionVenue] = []
    seen: set[PredictionVenue] = set()
    for name in names:
        try:
            venue = PredictionVenue(name)
        except ValueError as error:
            raise PredictionsUsageError(f"unknown venue {name!r}") from error
        if venue not in seen:
            seen.add(venue)
            venues.append(venue)
    return tuple(venues)


def _run_candidates(arguments: argparse.Namespace) -> int:
    venues = _parse_venues(arguments.venues)
    as_of = _parse_timestamp(arguments.as_of) if arguments.as_of else _utc_now()
    trial_family_id = arguments.trial_family

    venue_counts: dict[PredictionVenue, dict[RelationshipType, dict[str, int]]] = {}
    try:
        with database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS):
            store = PredictionMarketStore(arguments.db)
            try:
                registry = PredictionRegistry(store)
                proposed: dict[PredictionVenue, tuple[CandidateRelationship, ...]] = {}
                for venue in venues:
                    binary_complements = propose_binary_complements(
                        registry,
                        venue,
                        as_of,
                        trial_family_id=trial_family_id,
                        code_revision=_CANDIDATES_CODE_REVISION,
                    )
                    outcome_sets = propose_venue_native_outcome_sets(
                        registry,
                        venue,
                        as_of,
                        trial_family_id=trial_family_id,
                        code_revision=_CANDIDATES_CODE_REVISION,
                    )
                    proposed[venue] = binary_complements + outcome_sets

                with store.transaction() as transaction:
                    # Regenerating candidates at a later --as-of reproduces the same
                    # deterministic candidate_id (uuid5 over type+legs) with a different
                    # observed_at/information_cutoff. Appending it again would raise
                    # ConflictingRecordError and roll back genuinely new candidates in
                    # this same transaction, so skip already-known ids up front: the
                    # first-observed record stands, append-only immutability preserved.
                    known_candidate_ids = set(transaction.existing_candidate_ids())
                    for venue in venues:
                        # Pre-seeded with exactly the two generators called above
                        # (propose_binary_complements, propose_venue_native_outcome_sets);
                        # a future generator wired into `proposed` without a matching
                        # key here would KeyError below instead of silently dropping counts.
                        counts: dict[RelationshipType, dict[str, int]] = {
                            RelationshipType.BINARY_COMPLEMENT: {
                                "newly_appended": 0,
                                "already_known": 0,
                            },
                            RelationshipType.EXHAUSTIVE_OUTCOME_SET: {
                                "newly_appended": 0,
                                "already_known": 0,
                            },
                        }
                        for candidate in proposed[venue]:
                            bucket = counts[candidate.relationship_type]
                            if candidate.candidate_id in known_candidate_ids:
                                bucket["already_known"] += 1
                                continue
                            if transaction.append_candidate_relationship(candidate):
                                bucket["newly_appended"] += 1
                                known_candidate_ids.add(candidate.candidate_id)
                            else:
                                bucket["already_known"] += 1
                        venue_counts[venue] = counts
            finally:
                store.close()
    except PredictionsUsageError:
        raise
    except Exception as error:
        raise PredictionCandidatesError("candidate discovery failed to persist durably") from error

    if arguments.format == "json":
        print(
            json.dumps(
                {
                    "as_of": _timestamp(as_of),
                    "trial_family_id": trial_family_id,
                    "venues": {
                        venue.value: {
                            relationship_type.value: bucket
                            for relationship_type, bucket in counts.items()
                        }
                        for venue, counts in venue_counts.items()
                    },
                    "cross_venue_nomination": _CROSS_VENUE_ABSTENTION_LINE,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(
            f"predictions candidates | trial_family={trial_family_id} | as_of={_timestamp(as_of)}"
        )
        for venue, counts in venue_counts.items():
            for relationship_type, bucket in counts.items():
                print(
                    f"{venue.value} | {relationship_type.value} | "
                    f"newly_appended={bucket['newly_appended']} | "
                    f"already_known={bucket['already_known']}"
                )
        print(_CROSS_VENUE_ABSTENTION_LINE)
    return 0


def _run_attest(arguments: argparse.Namespace) -> int:
    input_path: Path = arguments.input
    try:
        raw_text = input_path.read_text(encoding="utf-8")
    except OSError as error:
        raise PredictionsUsageError(
            f"attestation input file is unavailable: {input_path}"
        ) from error
    try:
        attestations = _RULE_ATTESTATIONS_ADAPTER.validate_json(raw_text)
    except ValidationError as error:
        raise PredictionsUsageError(
            f"attestation input file {input_path} is not a valid JSON array of "
            f"rule attestations: {error}"
        ) from error

    appended = 0
    already_known = 0
    try:
        with database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS):
            store = PredictionMarketStore(arguments.db)
            try:
                # Verify every attestation against the immutable rule-version registry
                # before appending any of them: a mismatch or unknown id anywhere in the
                # batch must fail the whole import as a usage error, not partially persist.
                for attestation in attestations:
                    stored_rule_version = store.rule_version_by_id(attestation.rule_version_id)
                    if stored_rule_version is None:
                        raise PredictionsUsageError(
                            f"unknown rule_version_id {attestation.rule_version_id}"
                        )
                    if stored_rule_version.source_hash != attestation.rule_source_hash:
                        raise PredictionsUsageError(
                            f"rule_source_hash mismatch for rule_version_id "
                            f"{attestation.rule_version_id}"
                        )
                    if stored_rule_version.venue != attestation.venue:
                        raise PredictionsUsageError(
                            f"attestation {attestation.attestation_id} venue mismatch: "
                            f"rule_version_id {attestation.rule_version_id} is bound to "
                            f"venue {stored_rule_version.venue.value!r}, attestation "
                            f"declares {attestation.venue.value!r}"
                        )
                    if stored_rule_version.market_id != attestation.market_id:
                        raise PredictionsUsageError(
                            f"attestation {attestation.attestation_id} market_id mismatch: "
                            f"rule_version_id {attestation.rule_version_id} is bound to "
                            f"market_id {stored_rule_version.market_id!r}, attestation "
                            f"declares {attestation.market_id!r}"
                        )
                with store.transaction() as transaction:
                    for attestation in attestations:
                        if transaction.append_rule_attestation(attestation):
                            appended += 1
                        else:
                            already_known += 1
            finally:
                store.close()
    except PredictionsUsageError:
        raise
    except Exception as error:
        raise PredictionAttestError("rule attestation import failed to persist durably") from error

    print(f"appended {appended} attestations, already_known={already_known}")
    return 0


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PredictionsUsageError(f"invalid timestamp {value!r}") from error
    if parsed.tzinfo is None:
        raise PredictionsUsageError(f"timestamp {value!r} must be timezone-aware")
    return parsed.astimezone(UTC)
