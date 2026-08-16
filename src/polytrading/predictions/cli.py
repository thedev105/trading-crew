from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from polytrading.http_client import make_public_http_client
from polytrading.predictions.adapter import PredictionCollectionGateError
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
from polytrading.predictions.manifest import evaluate_collection_gate
from polytrading.predictions.polymarket import PolymarketAdapter
from polytrading.predictions.storage.store import PredictionMarketStore
from polytrading.trial.writer_lease import database_writer_lease

_WRITER_LEASE_TIMEOUT_SECONDS = 30.0
_ADAPTER_BY_VENUE = {
    PredictionVenue.POLYMARKET: PolymarketAdapter,
    PredictionVenue.KALSHI: KalshiAdapter,
}


class PredictionsUsageError(ValueError):
    """A user-facing prediction-market CLI validation error."""


class PredictionCollectionError(RuntimeError):
    """A prediction-market collection failure, sanitized for CLI output."""


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
    for name in ("polymarket", "kalshi"):
        collector = collect_commands.add_parser(name, help=f"collect {name} public evidence")
        collector.add_argument("--db", required=True, type=Path)

    health = predictions_commands.add_parser(
        "health", help="audit per-venue collection and continuity health"
    )
    health.add_argument("--db", required=True, type=Path)
    health.add_argument("--as-of")
    health.add_argument("--format", choices=("text", "json"), default="text")

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
        for venue in (PredictionVenue.POLYMARKET, PredictionVenue.KALSHI):
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


async def _run_collect(arguments: argparse.Namespace) -> int:
    venue = PredictionVenue(arguments.predictions_collect_command)
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
                async with make_public_http_client() as client:
                    adapter = adapter_cls(client, _utc_now, time.monotonic_ns)
                    batch = await adapter.fetch_markets(information_cutoff=as_of)
                market_count = 0
                with store.transaction() as transaction:
                    for raw in batch.raw:
                        transaction.append_raw(raw)
                    for item in batch.normalized:
                        if isinstance(item, MarketRecord):
                            transaction.append_market(item)
                            market_count += 1
                        elif isinstance(item, RuleVersion):
                            transaction.append_rule_version(item)
                for warning in batch.warnings:
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
