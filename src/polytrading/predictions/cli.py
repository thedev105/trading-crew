from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from pydantic import TypeAdapter, ValidationError

from polytrading.http_client import make_public_http_client
from polytrading.predictions.adapter import PredictionCollectionGateError
from polytrading.predictions.attestations import RuleAttestation
from polytrading.predictions.candidates import (
    propose_binary_complements,
    propose_venue_native_outcome_sets,
)
from polytrading.predictions.candidates_models import (
    CandidateLeg,
    CandidateRelationship,
    RelationshipType,
)
from polytrading.predictions.dashboard_server import (
    serve_prediction_dashboard,
    validate_prediction_dashboard_database,
)
from polytrading.predictions.domain import (
    MarketRecord,
    PredictionBookSnapshot,
    PredictionFeeRate,
    PredictionRecord,
    PredictionVenue,
    RuleVersion,
)
from polytrading.predictions.economics import evaluate_basket_economics
from polytrading.predictions.economics_models import (
    DEFAULT_RESEARCH_POLICY,
    EconomicsResult,
    ScanDecision,
    ScanReport,
    deterministic_scan_report_id,
)
from polytrading.predictions.experiments import ShadowExperiment, TrialFamily
from polytrading.predictions.health import PredictionHealthAuditor, VenueEvidenceStatus
from polytrading.predictions.health_report import (
    render_prediction_health_json,
    render_prediction_health_text,
)
from polytrading.predictions.kalshi import KalshiAdapter
from polytrading.predictions.limitless import LimitlessAdapter
from polytrading.predictions.manifest import evaluate_collection_gate
from polytrading.predictions.polymarket import PolymarketAdapter
from polytrading.predictions.proofs import compile_proof
from polytrading.predictions.proofs_models import ProofArtifact
from polytrading.predictions.registry import PredictionRegistry
from polytrading.predictions.risk import (
    DEFAULT_RISK_POLICY,
    PredictionRiskPolicy,
    ShadowPortfolioState,
)
from polytrading.predictions.shadow_ledger import (
    LedgerPosting,
    ShadowReconciliation,
    postings_for_events,
    proposal_paper_pnl,
    reconcile_proposal,
    reconciled_event_for,
)
from polytrading.predictions.shadow_models import (
    ShadowEvent,
    ShadowLegPlan,
    ShadowPlan,
    ShadowState,
)
from polytrading.predictions.shadow_planner import PlanRefusal, plan_shadow_proposal
from polytrading.predictions.shadow_simulator import (
    BASELINE,
    LATENCY_1S,
    LATENCY_5S,
    PARTIAL_FILL_50,
    SECOND_LEG_REJECT,
    UNKNOWN_AFTER_FIRST,
    StressScenario,
    simulate_shadow_proposal,
)
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
# No CLI-invoked reviewer-identity source exists yet (no auth/session layer); this
# mirrors _CANDIDATES_CODE_REVISION's fixed placeholder-constant pattern rather than
# inventing a new one. An operator who needs a real reviewer identity on record must
# pass --review-identity explicitly.
_DEFAULT_REVIEW_IDENTITY = "cli-operator"
_RULE_ATTESTATIONS_ADAPTER: TypeAdapter[list[RuleAttestation]] = TypeAdapter(list[RuleAttestation])
_SHADOW_SCENARIOS: dict[str, StressScenario] = {
    scenario.scenario_id: scenario
    for scenario in (
        BASELINE,
        LATENCY_1S,
        LATENCY_5S,
        PARTIAL_FILL_50,
        SECOND_LEG_REJECT,
        UNKNOWN_AFTER_FIRST,
    )
}
_SHADOW_EXPERIMENT_NAMESPACE = UUID("f0a29b77-1936-4b83-994c-129eaeb0ee08")


class PredictionsUsageError(ValueError):
    """A user-facing prediction-market CLI validation error."""


class PredictionCollectionError(RuntimeError):
    """A prediction-market collection failure, sanitized for CLI output."""


class PredictionCandidatesError(RuntimeError):
    """A prediction-market candidate-discovery failure, sanitized for CLI output."""


class PredictionAttestError(RuntimeError):
    """A prediction-market rule-attestation import failure, sanitized for CLI output."""


class PredictionProveError(RuntimeError):
    """A prediction-market proof-compilation failure, sanitized for CLI output."""


class PredictionScanError(RuntimeError):
    """A prediction-market scan failure, sanitized for CLI output."""


class PredictionShadowError(RuntimeError):
    """A shadow run/replay failure, sanitized for CLI output."""


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

    prove = predictions_commands.add_parser(
        "prove", help="compile and persist a deterministic payoff proof for one candidate"
    )
    prove.add_argument("--db", required=True, type=Path)
    prove.add_argument("--candidate-id", required=True)
    prove.add_argument("--as-of")
    prove.add_argument("--review-identity")
    prove.add_argument("--format", choices=("text", "json"), default="text")

    scan = predictions_commands.add_parser(
        "scan",
        help=(
            "join every candidate's latest proof with fresh books/fees/economics into "
            "a persisted per-candidate scan decision"
        ),
    )
    scan.add_argument("--db", required=True, type=Path)
    scan.add_argument("--as-of")
    scan.add_argument("--format", choices=("text", "json"), default="text")

    shadow = predictions_commands.add_parser(
        "shadow", help="run or replay deterministic local shadow experiments"
    )
    shadow_commands = shadow.add_subparsers(dest="predictions_shadow_command", required=True)
    shadow_register = shadow_commands.add_parser(
        "register-family", help="preregister one operator-authored trial family"
    )
    shadow_register.add_argument("--db", required=True, type=Path)
    shadow_register.add_argument("--input", required=True, type=Path)
    shadow_run = shadow_commands.add_parser("run", help="run stored shadow evidence")
    shadow_run.add_argument("--db", required=True, type=Path)
    shadow_run.add_argument("--trial-family", required=True)
    shadow_run.add_argument("--as-of")
    shadow_run.add_argument("--expiry-seconds", type=int, default=30)
    shadow_run.add_argument("--scenario", choices=tuple(_SHADOW_SCENARIOS), default="baseline")
    shadow_run.add_argument("--format", choices=("text", "json"), default="text")
    shadow_replay = shadow_commands.add_parser(
        "replay", help="replay one stored shadow proposal without writes"
    )
    shadow_replay.add_argument("--db", required=True, type=Path)
    shadow_replay.add_argument("--proposal-id", required=True)
    shadow_replay.add_argument("--scenario", choices=tuple(_SHADOW_SCENARIOS))
    shadow_replay.add_argument("--format", choices=("text", "json"), default="text")

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
    if arguments.predictions_command == "prove":
        return _run_prove(arguments)
    if arguments.predictions_command == "scan":
        return _run_scan(arguments)
    if arguments.predictions_command == "shadow":
        if arguments.predictions_shadow_command == "register-family":
            return _run_shadow_register_family(arguments)
        if arguments.predictions_shadow_command == "run":
            return _run_shadow_run(arguments)
        return _run_shadow_replay(arguments)
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
                                except Exception:
                                    # Sanitized per §14: never print raw exception text here,
                                    # which may carry URLs or response fragments -- a stable
                                    # code plus venue and market id only.
                                    print(
                                        "polytrading: warning: "
                                        f"{venue.value} BOOK_FEE_COLLECTION_FAILED "
                                        f"{market.market_id}",
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


def _current_rule_versions_for_candidate(
    store: PredictionMarketStore, candidate: CandidateRelationship, as_of: datetime
) -> dict[UUID, RuleVersion]:
    """The candidate's legs' markets' presently-effective rule versions, keyed by id.

    ``compile_proof`` treats ``rule_versions`` as the caller's current registry state:
    a leg whose own ``rule_version_id`` isn't its market's latest-effective version as
    of ``as_of`` must be absent from this mapping, so the compiler correctly rejects
    RULE_VERSION_CHANGED rather than silently proving against a superseded rule.
    """
    current: dict[UUID, RuleVersion] = {}
    seen_markets: set[tuple[PredictionVenue, str]] = set()
    for leg in candidate.legs:
        market_key = (leg.venue, leg.market_id)
        if market_key in seen_markets:
            continue
        seen_markets.add(market_key)
        venue_versions = tuple(
            version
            for version in store.verified_rule_versions_for_market(leg.market_id, as_of)
            if version.venue is leg.venue
        )
        if venue_versions:
            # rule_versions_for_market orders ascending by effective_at; the last
            # entry at-or-before as_of is the market's presently-effective version.
            latest = venue_versions[-1]
            current[latest.rule_version_id] = latest
    return current


def _run_prove(arguments: argparse.Namespace) -> int:
    as_of = _parse_timestamp(arguments.as_of) if arguments.as_of else _utc_now()
    review_identity = arguments.review_identity or _DEFAULT_REVIEW_IDENTITY
    try:
        candidate_id = UUID(arguments.candidate_id)
    except (TypeError, ValueError, AttributeError) as error:
        raise PredictionsUsageError(f"invalid --candidate-id {arguments.candidate_id!r}") from error

    artifact: ProofArtifact | None = None
    persisted = False
    try:
        with database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS):
            store = PredictionMarketStore(arguments.db)
            try:
                candidate = next(
                    (
                        item
                        for item in store.candidate_relationships_as_of(as_of)
                        if item.candidate_id == candidate_id
                    ),
                    None,
                )
                if candidate is None:
                    raise PredictionsUsageError(f"unknown candidate_id {candidate_id}")

                rule_versions = _current_rule_versions_for_candidate(store, candidate, as_of)
                attestations = {
                    leg.rule_version_id: attestation
                    for leg in candidate.legs
                    if (
                        attestation := store.latest_attestation_for_rule_version(
                            leg.rule_version_id, as_of
                        )
                    )
                    is not None
                }

                artifact = compile_proof(
                    candidate,
                    rule_versions,
                    attestations,
                    as_of=as_of,
                    review_identity=review_identity,
                )

                with store.transaction() as transaction:
                    persisted = transaction.append_proof_artifact(artifact)
            finally:
                store.close()
    except PredictionsUsageError:
        raise
    except Exception as error:
        raise PredictionProveError("proof compilation failed to persist durably") from error

    assert artifact is not None  # narrowed: only PredictionsUsageError exits before this point
    if arguments.format == "json":
        print(
            json.dumps(
                {
                    "candidate_id": str(candidate_id),
                    "proof_id": str(artifact.proof_id),
                    "status": artifact.status,
                    "rejection_reason": artifact.rejection_reason,
                    "minimum_basket_payout": (
                        str(artifact.minimum_basket_payout)
                        if artifact.minimum_basket_payout is not None
                        else None
                    ),
                    "persisted": persisted,
                    "as_of": _timestamp(as_of),
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(
            f"predictions prove | candidate_id={candidate_id} | status={artifact.status} | "
            f"reason={artifact.rejection_reason} | "
            f"minimum_basket_payout={artifact.minimum_basket_payout} | persisted={persisted}"
        )
    return 0


def _scan_one_candidate(
    store: PredictionMarketStore, candidate: CandidateRelationship, as_of: datetime
) -> ScanReport:
    """Join one candidate's latest proof with fresh evidence into a scan decision.

    Read-only over already-persisted proofs (never calls ``compile_proof``): a scan
    reports on what ``predictions prove`` has already established, joined with
    current books/fees/economics -- it never mints new proof evidence itself.

    Before running economics on a ``proof_ready`` proof, this re-validates rule-version
    currency: if any candidate leg's ``rule_version_id`` is no longer its market's
    presently-effective version as of ``as_of``, the decision is ``REJECTED`` /
    ``RULE_VERSION_CHANGED`` and economics never runs. A proof is compiled once and
    persisted append-only, so a rule version can be superseded after a proof went
    ``proof_ready`` but before a later scan runs; without this re-check, that stale
    proof would silently survive into a persisted ``SHADOW_CANDIDATE`` (spec §14),
    even though the proof's own ``invalidation_conditions`` name exactly this case.
    """
    proof = store.latest_proof_for_candidate(candidate.candidate_id, as_of)
    economics: EconomicsResult | None = None
    proof_id: UUID | None = None
    decision: ScanDecision
    reason: str

    if proof is None:
        decision, reason = "INSUFFICIENT_EVIDENCE", "no proof compiled"
    else:
        proof_id = proof.proof_id
        if proof.status == "insufficient_evidence":
            assert proof.rejection_reason is not None  # ProofArtifact invariant
            decision, reason = "INSUFFICIENT_EVIDENCE", proof.rejection_reason
        elif proof.status == "rejected":
            assert proof.rejection_reason is not None  # ProofArtifact invariant
            decision, reason = "REJECTED", proof.rejection_reason
        else:  # proof_ready
            current_rule_versions = _current_rule_versions_for_candidate(store, candidate, as_of)
            if any(leg.rule_version_id not in current_rule_versions for leg in candidate.legs):
                # A proof compiled against a since-superseded rule version must never
                # silently survive into a persisted SHADOW_CANDIDATE (spec §14); a scan
                # is read-only over the proof, so it re-validates currency here rather
                # than trusting the proof's own now-possibly-stale rule_version_ids.
                decision, reason = "REJECTED", "RULE_VERSION_CHANGED"
            else:
                books = {
                    i: store.latest_book_as_of(
                        leg.venue, leg.market_id, leg.outcome_token_id, as_of
                    )
                    for i, leg in enumerate(candidate.legs)
                }
                fees = {
                    i: store.latest_fee_rate_as_of(leg.venue, leg.market_id, as_of)
                    for i, leg in enumerate(candidate.legs)
                }
                economics = evaluate_basket_economics(
                    proof,
                    candidate,
                    books=books,
                    fees=fees,
                    policy=DEFAULT_RESEARCH_POLICY,
                    as_of=as_of,
                )
                if economics.status == "insufficient_evidence":
                    assert economics.insufficiency_reason is not None  # EconomicsResult invariant
                    decision, reason = "INSUFFICIENT_EVIDENCE", economics.insufficiency_reason
                elif economics.conservative_surplus_usd > 0:
                    decision = "SHADOW_CANDIDATE"
                    reason = "conservative surplus positive at current depth"
                else:
                    decision, reason = "REJECTED", "conservative surplus not positive"

    report_id = deterministic_scan_report_id(
        candidate_id=candidate.candidate_id,
        proof_id=proof_id,
        decision=decision,
        reason=reason,
        economics=economics,
        policy_id=DEFAULT_RESEARCH_POLICY.policy_id,
        policy_version=DEFAULT_RESEARCH_POLICY.policy_version,
        as_of=as_of,
    )
    return ScanReport(
        report_id=report_id,
        candidate_id=candidate.candidate_id,
        proof_id=proof_id,
        decision=decision,
        reason=reason,
        economics=economics,
        policy_id=DEFAULT_RESEARCH_POLICY.policy_id,
        policy_version=DEFAULT_RESEARCH_POLICY.policy_version,
        as_of=as_of,
        observed_at=as_of,
    )


def _run_scan(arguments: argparse.Namespace) -> int:
    as_of = _parse_timestamp(arguments.as_of) if arguments.as_of else _utc_now()

    tally: dict[str, int] = {"SHADOW_CANDIDATE": 0, "REJECTED": 0, "INSUFFICIENT_EVIDENCE": 0}
    shadow_candidates: list[dict[str, str]] = []
    try:
        with database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS):
            store = PredictionMarketStore(arguments.db)
            try:
                candidates = store.candidate_relationships_as_of(as_of)
                with store.transaction() as transaction:
                    known_report_ids = set(transaction.existing_scan_report_ids())
                    for candidate in candidates:
                        report = _scan_one_candidate(transaction, candidate, as_of)
                        tally[report.decision] += 1
                        if report.decision == "SHADOW_CANDIDATE":
                            assert report.economics is not None  # narrowed by ScanReport validator
                            shadow_candidates.append(
                                {
                                    "candidate_id": str(report.candidate_id),
                                    "conservative_surplus_usd": str(
                                        report.economics.conservative_surplus_usd
                                    ),
                                    "capacity_usd_at_current_depth": str(
                                        report.economics.capacity_usd_at_current_depth
                                    ),
                                }
                            )
                        already_known = report.report_id in known_report_ids
                        if not already_known and transaction.append_scan_report(report):
                            known_report_ids.add(report.report_id)
            finally:
                store.close()
    except PredictionsUsageError:
        raise
    except Exception as error:
        raise PredictionScanError("scan failed to persist durably") from error

    if arguments.format == "json":
        print(
            json.dumps(
                {
                    "as_of": _timestamp(as_of),
                    "tally": tally,
                    "shadow_candidates": shadow_candidates,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"predictions scan | as_of={_timestamp(as_of)}")
        for decision, count in tally.items():
            print(f"{decision}: {count}")
        for entry in shadow_candidates:
            print(
                f"shadow candidate {entry['candidate_id']} | "
                f"surplus={entry['conservative_surplus_usd']} | "
                f"capacity={entry['capacity_usd_at_current_depth']}"
            )
    return 0


def _run_shadow_run(arguments: argparse.Namespace) -> int:
    as_of = _parse_timestamp(arguments.as_of) if arguments.as_of else _utc_now()
    if arguments.expiry_seconds <= 0:
        raise PredictionsUsageError("--expiry-seconds must be a positive integer")
    _require_current_shadow_database(arguments.db)
    scenario = _SHADOW_SCENARIOS[arguments.scenario]
    result = {
        "as_of": _timestamp(as_of),
        "scenario": scenario.scenario_id,
        "planned": 0,
        "existing": 0,
        "refused": {},
        "terminal_states": {},
        "reconciled_paper_pnl_usd": "0",
    }
    refusals: Counter[str] = Counter()
    terminals: Counter[str] = Counter()
    run_pnl = Decimal("0")

    try:
        with database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS):
            store = PredictionMarketStore(arguments.db)
            try:
                with store.transaction() as transaction:
                    if (
                        transaction.verified_trial_family_by_id(arguments.trial_family, as_of)
                        is None
                    ):
                        raise PredictionsUsageError("trial family is not preregistered")
                    reports = _effective_shadow_candidate_reports(transaction, as_of)
                    report_ids = {report.report_id for report in reports}
                    batch_proposal_ids = {
                        plan.proposal_id
                        for plan in transaction.verified_shadow_plans_as_of(as_of)
                        if plan.scan_report_id in report_ids
                    }
                    batch_results: list[
                        tuple[ShadowPlan, ShadowReconciliation, Decimal | None]
                    ] = []
                    for report in reports:
                        evidence = _run_evidence(transaction, report, as_of)
                        if isinstance(evidence, str):
                            refusals[evidence] += 1
                            continue
                        candidate, proof, books, fees = evidence
                        if not _candidate_rules_are_current(transaction, candidate, as_of):
                            refusals["PROOF_NOT_CURRENT"] += 1
                            continue
                        portfolio = _shadow_portfolio_state(
                            transaction,
                            as_of,
                            DEFAULT_RISK_POLICY,
                            excluding_scan_report_id=report.report_id,
                            excluding_proposal_ids=frozenset(batch_proposal_ids),
                            batch_results=tuple(batch_results),
                        )
                        plan_or_refusal = plan_shadow_proposal(
                            scan_report=report,
                            candidate=candidate,
                            proof=proof,
                            books=books,
                            fees=fees,
                            economics_policy=DEFAULT_RESEARCH_POLICY,
                            risk_policy=DEFAULT_RISK_POLICY,
                            portfolio=portfolio,
                            as_of=as_of,
                            expiry_window_seconds=arguments.expiry_seconds,
                            event_cluster_id=_event_cluster_id(transaction, candidate, as_of),
                        )
                        if isinstance(plan_or_refusal, PlanRefusal):
                            refusals[plan_or_refusal.reason] += 1
                            continue
                        plan = plan_or_refusal
                        existing_plan = transaction.verified_shadow_plan_by_proposal(
                            plan.proposal_id
                        )
                        if existing_plan is not None:
                            if existing_plan != plan:
                                raise ValueError(
                                    "existing proposal does not match deterministic plan"
                                )
                            existing_reconciliation, existing_pnl = (
                                _validate_existing_shadow_bundle(
                                    transaction,
                                    plan=existing_plan,
                                    candidate=candidate,
                                    proof=proof,
                                    fees=fees,
                                    scenario=scenario,
                                    family_id=arguments.trial_family,
                                    as_of=as_of,
                                )
                            )
                            batch_results.append(
                                (existing_plan, existing_reconciliation, existing_pnl)
                            )
                            result["existing"] += 1
                            continue

                        events = simulate_shadow_proposal(
                            plan,
                            proof=proof,
                            candidate=candidate,
                            fees={index: fee for index, fee in fees.items() if fee is not None},
                            economics_policy=DEFAULT_RESEARCH_POLICY,
                            books=lambda index, at, candidate=candidate: _runtime_book(
                                transaction, candidate, index, at
                            ),
                            scenario=scenario,
                            started_at=as_of,
                        )
                        postings = postings_for_events(
                            plan,
                            events,
                            {index: fee for index, fee in fees.items() if fee is not None},
                        )
                        reconciliation = reconcile_proposal(
                            plan,
                            events,
                            postings,
                            {index: fee for index, fee in fees.items() if fee is not None},
                        )
                        pnl = proposal_paper_pnl(postings, reconciliation, events)
                        persisted_events = events
                        if reconciliation.complete:
                            persisted_events = (
                                *events,
                                reconciled_event_for(plan, events, reconciliation),
                            )
                        experiment = _shadow_experiment(
                            family_id=arguments.trial_family,
                            plan=plan,
                            scenario=scenario,
                            reconciliation=reconciliation,
                            pnl=pnl,
                            as_of=as_of,
                        )

                        transaction.append_shadow_plan(plan)
                        for event in persisted_events:
                            transaction.append_shadow_event(event)
                        for posting in postings:
                            transaction.append_ledger_posting(posting)
                        transaction.append_reconciliation(reconciliation)
                        transaction.append_shadow_experiment(experiment)

                        batch_proposal_ids.add(plan.proposal_id)
                        batch_results.append((plan, reconciliation, pnl))
                        result["planned"] += 1
                        terminals[reconciliation.terminal_state.value] += 1
                        if pnl is not None:
                            run_pnl += pnl
            finally:
                store.close()
    except PredictionsUsageError:
        raise
    except Exception as error:
        raise PredictionShadowError("shadow run failed to persist atomically") from error

    result["refused"] = dict(sorted(refusals.items()))
    result["terminal_states"] = dict(sorted(terminals.items()))
    result["reconciled_paper_pnl_usd"] = str(run_pnl)
    _render_shadow_run(result, arguments.format)
    return 0


def _require_current_shadow_database(path: Path) -> None:
    if not path.is_file():
        raise PredictionsUsageError("shadow database is unavailable or not current")
    try:
        store = PredictionMarketStore(path, read_only=True)
    except Exception as error:
        raise PredictionsUsageError("shadow database is unavailable or not current") from error
    else:
        store.close()


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _run_shadow_register_family(arguments: argparse.Namespace) -> int:
    _require_current_shadow_database(arguments.db)
    try:
        raw_bytes = arguments.input.read_bytes()
    except OSError as error:
        raise PredictionsUsageError("trial-family input file is unavailable") from error
    try:
        json.loads(
            raw_bytes,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
        family = TrialFamily.model_validate_json(raw_bytes)
    except (TypeError, ValueError, ValidationError) as error:
        raise PredictionsUsageError("trial-family input is not valid") from error

    appended = False
    try:
        with database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS):
            store = PredictionMarketStore(arguments.db)
            try:
                with store.transaction() as transaction:
                    appended = transaction.append_trial_family(family)
            finally:
                store.close()
    except PredictionsUsageError:
        raise
    except Exception as error:
        raise PredictionShadowError(
            "trial-family registration failed to persist atomically"
        ) from error

    print(
        f"appended {int(appended)} trial famil{'y' if appended else 'ies'}, "
        f"already_known={int(not appended)}"
    )
    return 0


def _run_evidence(
    store: PredictionMarketStore,
    report: ScanReport,
    as_of: datetime,
) -> (
    tuple[
        CandidateRelationship,
        ProofArtifact,
        dict[int, PredictionBookSnapshot | None],
        dict[int, PredictionFeeRate | None],
    ]
    | str
):
    candidate = store.verified_candidate_relationship_by_id(report.candidate_id, as_of)
    if candidate is None or report.proof_id is None:
        return "MISSING_EVIDENCE"
    proof = store.verified_proof_artifact_by_id(report.proof_id, as_of)
    exact_report = store.verified_scan_report_by_id(report.report_id, as_of)
    if proof is None or exact_report != report:
        return "MISSING_EVIDENCE"
    books = {
        index: _verified_latest_book(store, leg, as_of) for index, leg in enumerate(candidate.legs)
    }
    fees = {
        index: _verified_latest_fee(store, leg, as_of) for index, leg in enumerate(candidate.legs)
    }
    return candidate, proof, books, fees


def _effective_shadow_candidate_reports(
    store: PredictionMarketStore, as_of: datetime
) -> tuple[ScanReport, ...]:
    latest: dict[UUID, ScanReport] = {}
    for report in store.verified_scan_reports_as_of(as_of):
        current = latest.get(report.candidate_id)
        if current is None or (report.observed_at, str(report.report_id)) > (
            current.observed_at,
            str(current.report_id),
        ):
            latest[report.candidate_id] = report
    return tuple(
        report
        for _, report in sorted(latest.items(), key=lambda item: str(item[0]))
        if report.decision == "SHADOW_CANDIDATE"
    )


def _verified_latest_book(
    store: PredictionMarketStore,
    leg: CandidateLeg,
    as_of: datetime,
) -> PredictionBookSnapshot | None:
    latest = store.latest_book_as_of(leg.venue, leg.market_id, leg.outcome_token_id, as_of)
    if latest is None:
        return None
    return store.verified_book_snapshot_by_source_hash(
        leg.venue,
        leg.market_id,
        leg.outcome_token_id,
        latest.source_hash,
        as_of,
    )


def _verified_latest_fee(
    store: PredictionMarketStore,
    leg: CandidateLeg,
    as_of: datetime,
) -> PredictionFeeRate | None:
    latest = store.latest_fee_rate_as_of(leg.venue, leg.market_id, as_of)
    if latest is None:
        return None
    return store.verified_fee_rate_by_source_hash(
        leg.venue,
        leg.market_id,
        latest.source_hash,
        as_of,
    )


def _candidate_rules_are_current(
    store: PredictionMarketStore,
    candidate: CandidateRelationship,
    as_of: datetime,
) -> bool:
    current = _current_rule_versions_for_candidate(store, candidate, as_of)
    return all(leg.rule_version_id in current for leg in candidate.legs)


def _runtime_book(
    store: PredictionMarketStore,
    candidate: CandidateRelationship,
    leg_index: int,
    at: datetime,
) -> PredictionBookSnapshot | None:
    if leg_index < 0 or leg_index >= len(candidate.legs):
        return None
    leg = candidate.legs[leg_index]
    return _verified_latest_book(store, leg, at)


def _event_cluster_id(
    store: PredictionMarketStore,
    candidate: CandidateRelationship,
    as_of: datetime,
) -> str:
    markets_by_key: dict[tuple[PredictionVenue, str], MarketRecord] = {}
    for venue in {leg.venue for leg in candidate.legs}:
        markets_by_key.update(
            {
                (market.venue, market.market_id): market
                for market in store.verified_markets_as_of(venue, as_of)
            }
        )
    event_ids = {
        market.event_id
        for leg in candidate.legs
        if (market := markets_by_key.get((leg.venue, leg.market_id))) is not None
        and market.event_id
    }
    if len(event_ids) == 1 and all(
        (market := markets_by_key.get((leg.venue, leg.market_id))) is not None
        and market.event_id in event_ids
        for leg in candidate.legs
    ):
        return next(iter(event_ids))
    return str(candidate.candidate_id)


def _shadow_portfolio_state(
    store: PredictionMarketStore,
    as_of: datetime,
    policy: PredictionRiskPolicy,
    *,
    excluding_proposal_id: UUID | None = None,
    excluding_scan_report_id: UUID | None = None,
    excluding_proposal_ids: frozenset[UUID] = frozenset(),
    batch_results: Sequence[tuple[ShadowPlan, ShadowReconciliation, Decimal | None]] = (),
) -> ShadowPortfolioState:
    excluded = set(excluding_proposal_ids)
    if excluding_proposal_id is not None:
        excluded.add(excluding_proposal_id)
    all_experiments = store.verified_shadow_experiments_as_of(as_of)
    experiment_counts = Counter(item.proposal_id for item in all_experiments)
    if any(count != 1 for count in experiment_counts.values()):
        raise ValueError("multiple experiments exist for one shadow proposal")
    experiments = tuple(item for item in all_experiments if item.proposal_id not in excluded)
    running = policy.starting_equity_usd
    peak = running
    equity_24h = running
    day_cutoff = as_of - timedelta(hours=24)
    for experiment in sorted(experiments, key=lambda item: (item.observed_at, item.experiment_id)):
        if experiment.reconciled and experiment.paper_pnl_usd is not None:
            running += experiment.paper_pnl_usd
            peak = max(peak, running)
            if experiment.observed_at <= day_cutoff:
                equity_24h += experiment.paper_pnl_usd
    for _, _, pnl in batch_results:
        if pnl is not None:
            running += pnl
            peak = max(peak, running)

    exposure: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    open_count = 0
    for plan in store.verified_shadow_plans_as_of(as_of):
        if plan.proposal_id in excluded or plan.scan_report_id == excluding_scan_report_id:
            continue
        reconciliations = store.verified_shadow_reconciliations_for_proposal(
            plan.proposal_id, as_of
        )
        if len(reconciliations) > 1:
            raise ValueError("multiple reconciliations exist for one shadow proposal")
        reconciliation = reconciliations[0] if reconciliations else None
        if reconciliation is not None and reconciliation.complete:
            continue
        candidate = store.verified_candidate_relationship_by_id(
            plan.candidate_id, plan.information_cutoff
        )
        cluster = (
            str(plan.candidate_id)
            if candidate is None
            else _event_cluster_id(store, candidate, plan.information_cutoff)
        )
        frozen_basket_notional = sum(
            (price * quantity for leg in plan.legs for price, quantity in leg.limit_price_levels),
            Decimal("0"),
        )
        exposure[cluster] += max(
            plan.max_incomplete_exposure_usd,
            frozen_basket_notional,
        )
        open_count += 1
    for plan, reconciliation, _ in batch_results:
        if reconciliation.complete:
            continue
        candidate = store.verified_candidate_relationship_by_id(
            plan.candidate_id, plan.information_cutoff
        )
        cluster = (
            str(plan.candidate_id)
            if candidate is None
            else _event_cluster_id(store, candidate, plan.information_cutoff)
        )
        frozen_basket_notional = sum(
            (price * quantity for leg in plan.legs for price, quantity in leg.limit_price_levels),
            Decimal("0"),
        )
        exposure[cluster] += max(
            plan.max_incomplete_exposure_usd,
            frozen_basket_notional,
        )
        open_count += 1

    return ShadowPortfolioState(
        total_equity_usd=running,
        open_exposure_usd_by_cluster=dict(exposure),
        peak_equity_usd=max(peak, running),
        equity_24h_ago_usd=equity_24h,
        open_proposal_count=open_count,
    )


def _shadow_experiment(
    *,
    family_id: str,
    plan: ShadowPlan,
    scenario: StressScenario,
    reconciliation: ShadowReconciliation,
    pnl: Decimal | None,
    as_of: datetime,
) -> ShadowExperiment:
    terminal_state = (
        ShadowState.RECONCILED if reconciliation.complete else reconciliation.terminal_state
    )
    fields = [
        family_id,
        str(plan.proposal_id),
        scenario.scenario_id,
        terminal_state.value,
        str(pnl) if pnl is not None else None,
        reconciliation.complete,
        as_of.isoformat(),
        reconciliation.observed_at.isoformat(),
    ]
    return ShadowExperiment(
        experiment_id=uuid5(_SHADOW_EXPERIMENT_NAMESPACE, _canonical_json_value(fields)),
        family_id=family_id,
        proposal_id=plan.proposal_id,
        scenario_id=scenario.scenario_id,
        terminal_state=terminal_state,
        paper_pnl_usd=pnl,
        reconciled=reconciliation.complete,
        as_of=as_of,
        observed_at=reconciliation.observed_at,
    )


def _validate_existing_shadow_bundle(
    store: PredictionMarketStore,
    *,
    plan: ShadowPlan,
    candidate: CandidateRelationship,
    proof: ProofArtifact,
    fees: Mapping[int, PredictionFeeRate],
    scenario: StressScenario,
    family_id: str,
    as_of: datetime,
) -> tuple[ShadowReconciliation, Decimal | None]:
    far_future = datetime.max.replace(tzinfo=UTC)
    stored_events = store.shadow_events_for_proposal(plan.proposal_id, far_future)
    if not stored_events:
        raise ValueError("existing proposal is missing its event chain")
    execution_events = tuple(
        event for event in stored_events if event.to_state is not ShadowState.RECONCILED
    )
    if _stored_scenario(execution_events) != scenario:
        raise ValueError("existing proposal scenario is inconsistent")
    allowed_runtime_hashes = {
        source_hash for event in execution_events for source_hash in event.evidence_hashes
    }
    replayed = simulate_shadow_proposal(
        plan,
        proof=proof,
        candidate=candidate,
        fees=fees,
        economics_policy=DEFAULT_RESEARCH_POLICY,
        books=lambda index, at: _runtime_book_by_hashes(
            store,
            candidate,
            index,
            at,
            allowed_runtime_hashes,
        ),
        scenario=scenario,
        started_at=plan.information_cutoff,
    )
    if _first_event_divergence(replayed, execution_events) is not None:
        raise ValueError("existing proposal event chain is inconsistent")

    stored_postings = store.ledger_postings_for_proposal(plan.proposal_id, far_future)
    expected_postings = postings_for_events(plan, execution_events, fees)

    def posting_key(posting: LedgerPosting) -> str:
        return str(posting.posting_id)

    if sorted(stored_postings, key=posting_key) != sorted(expected_postings, key=posting_key):
        raise ValueError("existing proposal postings are inconsistent")

    expected_reconciliation = reconcile_proposal(
        plan,
        execution_events,
        stored_postings,
        fees,
    )
    stored_reconciliations = store.verified_shadow_reconciliations_for_proposal(
        plan.proposal_id, far_future
    )
    if stored_reconciliations != (expected_reconciliation,):
        raise ValueError("existing proposal reconciliation is inconsistent")

    trailing = tuple(event for event in stored_events if event.to_state is ShadowState.RECONCILED)
    if expected_reconciliation.complete:
        expected_terminal = reconciled_event_for(plan, execution_events, expected_reconciliation)
        if trailing != (expected_terminal,):
            raise ValueError("existing proposal reconciliation event is inconsistent")
    elif trailing:
        raise ValueError("incomplete proposal has a reconciliation event")

    pnl = proposal_paper_pnl(stored_postings, expected_reconciliation, stored_events)
    expected_experiment = _shadow_experiment(
        family_id=family_id,
        plan=plan,
        scenario=scenario,
        reconciliation=expected_reconciliation,
        pnl=pnl,
        as_of=as_of,
    )
    experiments = store.verified_shadow_experiments_for_proposal(plan.proposal_id, far_future)
    if experiments != (expected_experiment,):
        raise ValueError("existing proposal experiment is inconsistent")
    return expected_reconciliation, pnl


def _canonical_json_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _render_shadow_run(result: Mapping[str, object], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print(f"predictions shadow run | as_of={result['as_of']} | scenario={result['scenario']}")
    print(f"planned={result['planned']} | existing={result['existing']}")
    for reason, count in dict(result["refused"]).items():
        print(f"refused {reason}: {count}")
    for state, count in dict(result["terminal_states"]).items():
        print(f"terminal {state}: {count}")
    print(f"reconciled_paper_pnl_usd={result['reconciled_paper_pnl_usd']}")


def _run_shadow_replay(arguments: argparse.Namespace) -> int:
    try:
        proposal_id = UUID(arguments.proposal_id)
    except (TypeError, ValueError, AttributeError) as error:
        raise PredictionsUsageError(f"invalid --proposal-id {arguments.proposal_id!r}") from error
    _require_current_shadow_database(arguments.db)

    try:
        store = PredictionMarketStore(arguments.db, read_only=True)
        try:
            plan = store.verified_shadow_plan_by_proposal(proposal_id)
            if plan is None:
                raise LookupError("proposal not found")
            report, candidate, proof, planning_books, fees = _replay_evidence(store, plan)
            _validate_rederived_plan(
                store,
                plan,
                report,
                candidate,
                proof,
                planning_books,
                fees,
            )
            far_future = datetime.max.replace(tzinfo=UTC)
            stored_events = store.shadow_events_for_proposal(proposal_id, far_future)
            if not stored_events:
                raise LookupError("event chain not found")
            execution_events = tuple(
                event for event in stored_events if event.to_state is not ShadowState.RECONCILED
            )
            stored_scenario = _stored_scenario(execution_events)
            what_if = arguments.scenario is not None
            scenario = _SHADOW_SCENARIOS[arguments.scenario] if what_if else stored_scenario
            allowed_runtime_hashes = {
                source_hash for event in execution_events for source_hash in event.evidence_hashes
            }

            def replay_book(index: int, at: datetime) -> PredictionBookSnapshot | None:
                if what_if:
                    return _runtime_book(store, candidate, index, at)
                return _runtime_book_by_hashes(
                    store,
                    candidate,
                    index,
                    at,
                    allowed_runtime_hashes,
                )

            replayed = simulate_shadow_proposal(
                plan,
                proof=proof,
                candidate=candidate,
                fees=fees,
                economics_policy=DEFAULT_RESEARCH_POLICY,
                books=replay_book,
                scenario=scenario,
                started_at=plan.information_cutoff,
            )
            if what_if:
                _render_shadow_replay_what_if(plan, scenario, replayed, arguments.format)
                return 0

            divergence = _first_event_divergence(replayed, execution_events)
            displayed = list(replayed)
            trailing = tuple(
                event for event in stored_events if event.to_state is ShadowState.RECONCILED
            )
            stored_reconciliations = store.verified_shadow_reconciliations_for_proposal(
                proposal_id, far_future
            )
            if len(stored_reconciliations) != 1:
                raise LookupError("stored reconciliation is missing or ambiguous")
            stored_reconciliation = stored_reconciliations[0]
            stored_postings = store.ledger_postings_for_proposal(proposal_id, far_future)
            replayed_reconciliation = reconcile_proposal(
                plan,
                execution_events,
                stored_postings,
                fees,
            )
            if stored_reconciliation != replayed_reconciliation and divergence is None:
                divergence = len(execution_events)
            if replayed_reconciliation.complete:
                expected_reconciled = reconciled_event_for(
                    plan, execution_events, replayed_reconciliation
                )
                displayed.append(expected_reconciled)
                if (
                    len(trailing) != 1 or expected_reconciled != trailing[0]
                ) and divergence is None:
                    divergence = expected_reconciled.sequence
            elif trailing and divergence is None:
                divergence = trailing[0].sequence
            _render_shadow_replay_verdict(
                plan,
                tuple(displayed),
                divergence,
                arguments.format,
            )
            return 0 if divergence is None else 1
        finally:
            store.close()
    except PredictionsUsageError:
        raise
    except Exception as error:
        raise PredictionShadowError(
            "shadow replay evidence is unavailable or inconsistent"
        ) from error


def _replay_evidence(
    store: PredictionMarketStore,
    plan: ShadowPlan,
) -> tuple[
    ScanReport,
    CandidateRelationship,
    ProofArtifact,
    dict[int, PredictionBookSnapshot],
    dict[int, PredictionFeeRate],
]:
    cutoff = plan.information_cutoff
    report = store.verified_scan_report_by_id(plan.scan_report_id, cutoff)
    candidate = store.verified_candidate_relationship_by_id(plan.candidate_id, cutoff)
    proof = store.verified_proof_artifact_by_id(plan.proof_id, cutoff)
    if report is None or candidate is None or proof is None:
        raise LookupError("plan lineage record missing")
    if (
        report.candidate_id != candidate.candidate_id
        or report.proof_id != proof.proof_id
        or proof.candidate_id != candidate.candidate_id
    ):
        raise ValueError("plan lineage identities disagree")

    books: dict[int, PredictionBookSnapshot] = {}
    fees: dict[int, PredictionFeeRate] = {}
    for leg in plan.legs:
        book = _book_from_frozen_hashes(store, leg, plan.frozen_hashes, cutoff)
        fee = _fee_from_frozen_hashes(store, leg, plan.frozen_hashes, cutoff)
        if book is None or fee is None:
            raise LookupError("frozen plan evidence missing")
        books[leg.leg_index] = book
        fees[leg.leg_index] = fee
    return report, candidate, proof, books, fees


def _book_from_frozen_hashes(
    store: PredictionMarketStore,
    leg: ShadowLegPlan,
    frozen_hashes: Sequence[str],
    cutoff: datetime,
) -> PredictionBookSnapshot | None:
    matches = [
        book
        for source_hash in frozen_hashes
        if (
            book := store.verified_book_snapshot_by_source_hash(
                leg.venue,
                leg.market_id,
                leg.outcome_token_id,
                source_hash,
                cutoff,
            )
        )
        is not None
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: (item.observed_at, str(item.cycle_id)))


def _fee_from_frozen_hashes(
    store: PredictionMarketStore,
    leg: ShadowLegPlan,
    frozen_hashes: Sequence[str],
    cutoff: datetime,
) -> PredictionFeeRate | None:
    matches = [
        fee
        for source_hash in frozen_hashes
        if (
            fee := store.verified_fee_rate_by_source_hash(
                leg.venue,
                leg.market_id,
                source_hash,
                cutoff,
            )
        )
        is not None
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: item.observed_at)


def _validate_rederived_plan(
    store: PredictionMarketStore,
    stored: ShadowPlan,
    report: ScanReport,
    candidate: CandidateRelationship,
    proof: ProofArtifact,
    books: Mapping[int, PredictionBookSnapshot],
    fees: Mapping[int, PredictionFeeRate],
) -> None:
    if (
        stored.policy_id != DEFAULT_RESEARCH_POLICY.policy_id
        or stored.policy_version != DEFAULT_RESEARCH_POLICY.policy_version
        or stored.risk_policy_version != DEFAULT_RISK_POLICY.policy_version
        or _policy_hash(DEFAULT_RESEARCH_POLICY) not in stored.frozen_hashes
        or _policy_hash(DEFAULT_RISK_POLICY) not in stored.frozen_hashes
    ):
        raise ValueError("stored policy lineage is not available")
    if not _candidate_rules_are_current(store, candidate, stored.information_cutoff):
        raise ValueError("stored candidate rules were not current at planning")
    expiry_seconds = int((stored.expires_at - stored.information_cutoff).total_seconds())
    portfolio = _shadow_portfolio_state(
        store,
        stored.information_cutoff,
        DEFAULT_RISK_POLICY,
        excluding_proposal_id=stored.proposal_id,
    )
    rederived = plan_shadow_proposal(
        scan_report=report,
        candidate=candidate,
        proof=proof,
        books=books,
        fees=fees,
        economics_policy=DEFAULT_RESEARCH_POLICY,
        risk_policy=DEFAULT_RISK_POLICY,
        portfolio=portfolio,
        as_of=stored.information_cutoff,
        expiry_window_seconds=expiry_seconds,
        event_cluster_id=_event_cluster_id(store, candidate, stored.information_cutoff),
    )
    if not isinstance(rederived, ShadowPlan) or rederived != stored:
        raise ValueError("stored plan does not match exact frozen lineage")


def _stored_scenario(events: Sequence[ShadowEvent]) -> StressScenario:
    scenario_ids = {event.scenario_id for event in events if event.scenario_id is not None}
    if len(scenario_ids) != 1:
        raise ValueError("stored execution does not identify one scenario")
    scenario_id = next(iter(scenario_ids))
    try:
        return _SHADOW_SCENARIOS[scenario_id]
    except KeyError as error:
        raise ValueError("stored scenario is not supported") from error


def _runtime_book_by_hashes(
    store: PredictionMarketStore,
    candidate: CandidateRelationship,
    leg_index: int,
    at: datetime,
    source_hashes: set[str],
) -> PredictionBookSnapshot | None:
    if leg_index < 0 or leg_index >= len(candidate.legs):
        return None
    leg = candidate.legs[leg_index]
    matches = [
        book
        for source_hash in source_hashes
        if (
            book := store.verified_book_snapshot_by_source_hash(
                leg.venue,
                leg.market_id,
                leg.outcome_token_id,
                source_hash,
                at,
            )
        )
        is not None
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: (item.observed_at, str(item.cycle_id)))


def _first_event_divergence(
    replayed: Sequence[ShadowEvent],
    stored: Sequence[ShadowEvent],
) -> int | None:
    for expected, actual in zip(replayed, stored, strict=False):
        if expected != actual:
            return min(expected.sequence, actual.sequence)
    if len(replayed) != len(stored):
        return min(len(replayed), len(stored))
    return None


def _render_shadow_replay_verdict(
    plan: ShadowPlan,
    events: Sequence[ShadowEvent],
    divergence: int | None,
    output_format: str,
) -> None:
    verdict = (
        "replay MATCHES stored events"
        if divergence is None
        else f"replay DIVERGES at sequence {divergence}"
    )
    if output_format == "json":
        print(
            json.dumps(
                {
                    "mode": "verification",
                    "proposal_id": str(plan.proposal_id),
                    "events": [event.model_dump(mode="json") for event in events],
                    "verdict": verdict,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    print(f"predictions shadow replay | proposal_id={plan.proposal_id}")
    for event in events:
        print(
            f"sequence={event.sequence} | from="
            f"{event.from_state.value if event.from_state is not None else '-'} | "
            f"to={event.to_state.value} | occurred_at={_timestamp(event.occurred_at)}"
        )
    print(verdict)


def _render_shadow_replay_what_if(
    plan: ShadowPlan,
    scenario: StressScenario,
    events: Sequence[ShadowEvent],
    output_format: str,
) -> None:
    if output_format == "json":
        print(
            json.dumps(
                {
                    "mode": "what_if",
                    "persisted": False,
                    "proposal_id": str(plan.proposal_id),
                    "scenario": scenario.scenario_id,
                    "events": [event.model_dump(mode="json") for event in events],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    print(
        f"predictions shadow replay what-if | proposal_id={plan.proposal_id} | "
        f"scenario={scenario.scenario_id} | persisted=false"
    )
    for event in events:
        print(
            f"sequence={event.sequence} | from="
            f"{event.from_state.value if event.from_state is not None else '-'} | "
            f"to={event.to_state.value} | occurred_at={_timestamp(event.occurred_at)}"
        )


def _policy_hash(policy: PredictionRecord) -> str:
    canonical = _canonical_json_value(policy.model_dump(mode="json"))
    return hashlib.sha256(canonical.encode()).hexdigest()


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
