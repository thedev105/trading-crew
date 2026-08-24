from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

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
from polytrading.predictions.economics import evaluate_basket_economics
from polytrading.predictions.economics_models import (
    DEFAULT_RESEARCH_POLICY,
    EconomicsResult,
    ScanDecision,
    ScanReport,
    deterministic_scan_report_id,
)
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
            for version in store.rule_versions_for_market(leg.market_id, as_of)
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
