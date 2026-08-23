from __future__ import annotations

from datetime import datetime

from polytrading.predictions.candidates_models import (
    CandidateDisposition,
    CandidateLeg,
    CandidateRelationship,
    DeterministicProvenance,
    RelationshipType,
    deterministic_candidate_id,
)
from polytrading.predictions.domain import MarketRecord, PredictionVenue
from polytrading.predictions.propositions import TypedProposition
from polytrading.predictions.registry import PredictionRegistry

_NEGATIVE_RISK_VENUES = (PredictionVenue.POLYMARKET, PredictionVenue.LIMITLESS)


def _leg_or_none(
    registry: PredictionRegistry,
    market: MarketRecord,
    as_of: datetime,
    outcome_index: int | None,
) -> CandidateLeg | None:
    """Build a leg for ``market``, or ``None`` if its current rule version is unresolvable.

    A market whose ``rule_version_id`` is not found in the registry's rule history cannot
    produce a leg -- fail closed rather than guess a source hash.
    """
    history = registry.rule_history(market.venue, market.market_id, as_of)
    rule = next(
        (version for version in history if version.rule_version_id == market.rule_version_id),
        None,
    )
    if rule is None:
        return None

    outcome_token_id = None
    if outcome_index is not None and market.outcome_token_ids is not None:
        outcome_token_id = market.outcome_token_ids[outcome_index]

    return CandidateLeg(
        venue=market.venue,
        market_id=market.market_id,
        outcome_index=outcome_index,
        outcome_token_id=outcome_token_id,
        rule_version_id=market.rule_version_id,
        rule_source_hash=rule.source_hash,
    )


def _all_legs_or_none(
    registry: PredictionRegistry,
    markets: list[MarketRecord],
    as_of: datetime,
) -> tuple[CandidateLeg, ...] | None:
    """Build a leg for every market in ``markets``, or ``None`` if any single one can't.

    A partial leg list for an outcome-set group would misrepresent the venue's actual
    event-group membership -- worse than skipping -- so this is all-or-nothing for the
    whole group, not per-member: one unresolvable member fails the entire group closed.
    """
    legs: list[CandidateLeg] = []
    for market in markets:
        leg = _leg_or_none(registry, market, as_of, None)
        if leg is None:
            return None
        legs.append(leg)
    return tuple(legs)


def _outcome_membership_propositions(
    legs: tuple[CandidateLeg, ...],
) -> tuple[TypedProposition, ...]:
    return tuple(
        TypedProposition(
            schema_version=1,
            kind="outcome_membership",
            subject=leg.market_id,
            predicate="outcome_membership",
            value=None,
            status="unknown",
            supporting_spans=(),
        )
        for leg in legs
    )


def _is_eligible_open_market(market: MarketRecord) -> bool:
    return market.active and not market.closed and market.order_book_enabled


def propose_binary_complements(
    registry: PredictionRegistry,
    venue: PredictionVenue,
    as_of: datetime,
    *,
    trial_family_id: str,
    code_revision: str,
) -> tuple[CandidateRelationship, ...]:
    """Propose one ``BINARY_COMPLEMENT`` candidate per eligible two-outcome market.

    Pure over the registry snapshot at ``as_of``: no network, no clock reads, no storage
    writes. The generator proposes; it never claims a proven terminal partition.
    """
    provenance = DeterministicProvenance(
        kind="deterministic",
        generator="binary_complement",
        generator_version="1",
        code_revision=code_revision,
    )

    candidates: list[CandidateRelationship] = []
    for market in registry.markets_by_venue_as_of(venue, as_of):
        if not _is_eligible_open_market(market):
            continue
        if len(market.outcomes) != 2:
            continue

        leg_0 = _leg_or_none(registry, market, as_of, 0)
        leg_1 = _leg_or_none(registry, market, as_of, 1)
        if leg_0 is None or leg_1 is None:
            continue

        legs = (leg_0, leg_1)
        candidates.append(
            CandidateRelationship(
                schema_version=1,
                candidate_id=deterministic_candidate_id(RelationshipType.BINARY_COMPLEMENT, legs),
                trial_family_id=trial_family_id,
                relationship_type=RelationshipType.BINARY_COMPLEMENT,
                legs=legs,
                information_cutoff=as_of,
                observed_at=as_of,
                provenance=provenance,
                propositions=_outcome_membership_propositions(legs),
                unresolved_fields=("terminal_partition_unproven",),
                contradictions=(),
                invalidation_conditions=(),
                review_status="unreviewed",
                disposition=CandidateDisposition.QUARANTINED,
                superseded_by_candidate_id=None,
            )
        )
    return tuple(candidates)


def propose_venue_native_outcome_sets(
    registry: PredictionRegistry,
    venue: PredictionVenue,
    as_of: datetime,
    *,
    trial_family_id: str,
    code_revision: str,
) -> tuple[CandidateRelationship, ...]:
    """Propose one ``EXHAUSTIVE_OUTCOME_SET`` candidate per eligible ``event_id`` group.

    Polymarket and Limitless groups additionally require every member's ``negative_risk``
    to be ``True``; Kalshi groups purely by ``event_id`` and never consults
    ``negative_risk`` (spec section 6.2). An outcome list that merely looks complete is
    never treated as proof (spec section 4.2) -- every emitted candidate carries
    ``unresolved_fields=("outcome_set_exhaustiveness_unproven",)``.
    """
    provenance = DeterministicProvenance(
        kind="deterministic",
        generator="venue_native_outcome_set",
        generator_version="1",
        code_revision=code_revision,
    )

    groups: dict[str, list[MarketRecord]] = {}
    for market in registry.markets_by_venue_as_of(venue, as_of):
        if not _is_eligible_open_market(market):
            continue
        if market.event_id is None:
            continue
        groups.setdefault(market.event_id, []).append(market)

    candidates: list[CandidateRelationship] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        if venue in _NEGATIVE_RISK_VENUES and not all(
            member.negative_risk is True for member in members
        ):
            continue

        members = sorted(members, key=lambda member: member.market_id)
        legs_tuple = _all_legs_or_none(registry, members, as_of)
        if legs_tuple is None:
            continue
        candidates.append(
            CandidateRelationship(
                schema_version=1,
                candidate_id=deterministic_candidate_id(
                    RelationshipType.EXHAUSTIVE_OUTCOME_SET, legs_tuple
                ),
                trial_family_id=trial_family_id,
                relationship_type=RelationshipType.EXHAUSTIVE_OUTCOME_SET,
                legs=legs_tuple,
                information_cutoff=as_of,
                observed_at=as_of,
                provenance=provenance,
                propositions=_outcome_membership_propositions(legs_tuple),
                unresolved_fields=("outcome_set_exhaustiveness_unproven",),
                contradictions=(),
                invalidation_conditions=(),
                review_status="unreviewed",
                disposition=CandidateDisposition.QUARANTINED,
                superseded_by_candidate_id=None,
            )
        )
    return tuple(candidates)
