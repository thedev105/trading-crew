from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid5

from polytrading.carry.economics_execution import (
    InsufficientDepthError,
    PairedBookObservation,
    WalkedQuote,
    walk_book,
)
from polytrading.carry.economics_funding import exact_median
from polytrading.carry.economics_models import (
    CandidateEconomicsReport,
    EconomicsDecision,
    FundingDirection,
)
from polytrading.domain.models import (
    InstrumentSpec,
    Level2BookSnapshot,
    Venue,
    normalize_utc_timestamp,
)
from polytrading.ledger.models import JournalPosting, JournalTransaction
from polytrading.trial.paper_models import PaperCloseReason, PaperPosition, PaperPositionClosure

PAPER_RESEARCH_WARNING = (
    "Research only — simulated paper position, not a live fill or trading authorization."
)


class PaperOpenRejected(ValueError):
    """Raised when a paper position cannot be opened from the given report/book."""


def current_regime_reversed(oriented_hourly_rates: tuple[Decimal, ...]) -> bool:
    """True when the trailing frozen-direction funding median is not positive."""
    return exact_median(oriented_hourly_rates) <= 0


def _entry_levels(
    direction: FundingDirection, lighter_book: Level2BookSnapshot, dydx_book: Level2BookSnapshot
) -> tuple[tuple, tuple]:
    if direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX:
        return lighter_book.bids[:20], dydx_book.asks[:20]
    return lighter_book.asks[:20], dydx_book.bids[:20]


def _exit_levels(
    direction: FundingDirection, lighter_book: Level2BookSnapshot, dydx_book: Level2BookSnapshot
) -> tuple[tuple, tuple]:
    if direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX:
        return lighter_book.asks[:20], dydx_book.bids[:20]
    return lighter_book.bids[:20], dydx_book.asks[:20]


def _walk_base(levels: tuple, base_quantity: Decimal, instrument: InstrumentSpec) -> WalkedQuote:
    walked = walk_book(levels, base_quantity / instrument.contract_multiplier)
    return WalkedQuote(
        quantity=base_quantity,
        notional=walked.notional * instrument.contract_multiplier,
        weighted_average_price=walked.weighted_average_price,
    )


def _leg_is_long(direction: FundingDirection, venue: Venue) -> bool:
    if direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX:
        return venue is Venue.DYDX
    return venue is Venue.LIGHTER


def open_paper_position(
    *,
    report: CandidateEconomicsReport,
    current_books: PairedBookObservation,
    lighter_instrument: InstrumentSpec,
    dydx_instrument: InstrumentSpec,
    position_id: UUID,
    opening_book_cycle_id: UUID,
    opened_at: datetime,
) -> tuple[PaperPosition, JournalTransaction]:
    if report.decision is not EconomicsDecision.SHADOW_CANDIDATE or report.economics is None:
        raise PaperOpenRejected("source report is not a SHADOW_CANDIDATE")
    direction = report.direction
    if direction is None:
        raise PaperOpenRejected("source report has no frozen direction")
    base_quantity = report.economics.base_quantity
    lighter_levels, dydx_levels = _entry_levels(
        direction, current_books.lighter, current_books.dydx
    )
    try:
        lighter_walk = _walk_base(lighter_levels, base_quantity, lighter_instrument)
        dydx_walk = _walk_base(dydx_levels, base_quantity, dydx_instrument)
    except InsufficientDepthError as error:
        raise PaperOpenRejected("current book cannot fill the frozen base quantity") from error

    normalized_opened_at = normalize_utc_timestamp(opened_at)
    position = PaperPosition(
        schema_version=1,
        position_id=position_id,
        source_evaluation_id=report.evaluation_id,
        asset=report.asset,
        direction=direction,
        opened_at=normalized_opened_at,
        base_quantity=base_quantity,
        lighter_entry_notional_usd=lighter_walk.notional,
        dydx_entry_notional_usd=dydx_walk.notional,
        lighter_entry_price=lighter_walk.weighted_average_price,
        dydx_entry_price=dydx_walk.weighted_average_price,
        opening_book_cycle_id=opening_book_cycle_id,
        warning=PAPER_RESEARCH_WARNING,
    )
    postings: list[JournalPosting] = []
    for venue, notional in (
        (Venue.LIGHTER, lighter_walk.notional),
        (Venue.DYDX, dydx_walk.notional),
    ):
        account = f"paper:position:{venue.value}"
        if _leg_is_long(direction, venue):
            postings.append(
                JournalPosting(
                    account=account,
                    asset=report.asset.value,
                    debit=notional,
                    credit=Decimal(0),
                )
            )
            postings.append(
                JournalPosting(
                    account="paper:cash",
                    asset=report.asset.value,
                    debit=Decimal(0),
                    credit=notional,
                )
            )
        else:
            postings.append(
                JournalPosting(
                    account="paper:cash",
                    asset=report.asset.value,
                    debit=notional,
                    credit=Decimal(0),
                )
            )
            postings.append(
                JournalPosting(
                    account=account,
                    asset=report.asset.value,
                    debit=Decimal(0),
                    credit=notional,
                )
            )
    transaction = JournalTransaction(
        schema_version=1,
        transaction_id=position_id,
        occurred_at=normalized_opened_at,
        observed_at=normalized_opened_at,
        description=f"paper open {report.asset.value} {direction.value}",
        postings=tuple(postings),
        evidence_ids=(str(opening_book_cycle_id),),
    )
    return position, transaction


def close_paper_position(
    *,
    position: PaperPosition,
    current_books: PairedBookObservation,
    lighter_instrument: InstrumentSpec,
    dydx_instrument: InstrumentSpec,
    closing_book_cycle_id: UUID,
    closed_at: datetime,
    close_reason: PaperCloseReason,
    realized_funding_usd: Decimal,
) -> tuple[PaperPositionClosure, JournalTransaction]:
    lighter_levels, dydx_levels = _exit_levels(
        position.direction, current_books.lighter, current_books.dydx
    )
    try:
        lighter_walk = _walk_base(lighter_levels, position.base_quantity, lighter_instrument)
        dydx_walk = _walk_base(dydx_levels, position.base_quantity, dydx_instrument)
    except InsufficientDepthError as error:
        raise PaperOpenRejected("current book cannot fill the exit quantity") from error

    normalized_closed_at = normalize_utc_timestamp(closed_at)
    postings: list[JournalPosting] = []
    trading_pnl = Decimal(0)
    for venue, entry_notional, exit_notional in (
        (Venue.LIGHTER, position.lighter_entry_notional_usd, lighter_walk.notional),
        (Venue.DYDX, position.dydx_entry_notional_usd, dydx_walk.notional),
    ):
        account = f"paper:position:{venue.value}"
        is_long = _leg_is_long(position.direction, venue)
        # long leg profits when price rises (exit > entry); short leg profits when
        # price falls (entry > exit) — this is the one place direction-sensitive
        # sign enters the ledger; everything else below is direction-agnostic.
        signed_pnl = (
            (exit_notional - entry_notional) if is_long else (entry_notional - exit_notional)
        )
        trading_pnl += signed_pnl
        if is_long:
            postings.append(
                JournalPosting(
                    account="paper:cash",
                    asset=position.asset.value,
                    debit=exit_notional,
                    credit=Decimal(0),
                )
            )
            postings.append(
                JournalPosting(
                    account=account,
                    asset=position.asset.value,
                    debit=Decimal(0),
                    credit=entry_notional,
                )
            )
        else:
            postings.append(
                JournalPosting(
                    account=account,
                    asset=position.asset.value,
                    debit=entry_notional,
                    credit=Decimal(0),
                )
            )
            postings.append(
                JournalPosting(
                    account="paper:cash",
                    asset=position.asset.value,
                    debit=Decimal(0),
                    credit=exit_notional,
                )
            )
        pnl_debit = max(Decimal(0), -signed_pnl)
        pnl_credit = max(Decimal(0), signed_pnl)
        if pnl_debit > 0 or pnl_credit > 0:
            postings.append(
                JournalPosting(
                    account="paper:pnl:trading",
                    asset=position.asset.value,
                    debit=pnl_debit,
                    credit=pnl_credit,
                )
            )
    realized_pnl = trading_pnl + realized_funding_usd

    closure = PaperPositionClosure(
        schema_version=1,
        position_id=position.position_id,
        closed_at=normalized_closed_at,
        close_reason=close_reason,
        lighter_exit_notional_usd=lighter_walk.notional,
        dydx_exit_notional_usd=dydx_walk.notional,
        lighter_exit_price=lighter_walk.weighted_average_price,
        dydx_exit_price=dydx_walk.weighted_average_price,
        closing_book_cycle_id=closing_book_cycle_id,
        realized_funding_usd=realized_funding_usd,
        realized_pnl_usd=realized_pnl,
    )
    # Close reuses the same position_id but is a distinct immutable ledger event
    # from the open transaction, so it needs its own transaction_id — otherwise
    # persisting both to the same store collides on the open transaction's
    # identity (transaction_id is the store's dedup/conflict key). Same fix
    # shape as the funding-accrual transaction_id below.
    close_transaction_id = uuid5(position.position_id, f"close:{normalized_closed_at.isoformat()}")
    transaction = JournalTransaction(
        schema_version=1,
        transaction_id=close_transaction_id,
        occurred_at=normalized_closed_at,
        observed_at=normalized_closed_at,
        description=f"paper close {position.asset.value} {close_reason.value}",
        postings=tuple(postings),
        evidence_ids=(str(closing_book_cycle_id),),
    )
    return closure, transaction


def funding_accrual_transaction(
    *,
    position: PaperPosition,
    effective_at: datetime,
    lighter_rate: Decimal,
    dydx_rate: Decimal,
) -> JournalTransaction | None:
    if position.direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX:
        lighter_funding_usd = -position.lighter_entry_notional_usd * lighter_rate
        dydx_funding_usd = position.dydx_entry_notional_usd * dydx_rate
    else:
        lighter_funding_usd = position.lighter_entry_notional_usd * lighter_rate
        dydx_funding_usd = -position.dydx_entry_notional_usd * dydx_rate
    net = lighter_funding_usd + dydx_funding_usd
    if net == 0:
        # a zero-value posting is not a valid JournalPosting; there is simply no
        # funding event to record for this hour.
        return None
    normalized_effective_at = normalize_utc_timestamp(effective_at)
    cash_debit = net if net > 0 else Decimal(0)
    cash_credit = -net if net < 0 else Decimal(0)
    transaction_id = uuid5(position.position_id, normalized_effective_at.isoformat())
    return JournalTransaction(
        schema_version=1,
        transaction_id=transaction_id,
        occurred_at=normalized_effective_at,
        observed_at=normalized_effective_at,
        description=(
            f"paper funding accrual {position.asset.value} {normalized_effective_at.isoformat()}"
        ),
        postings=(
            JournalPosting(
                account="paper:cash",
                asset=position.asset.value,
                debit=cash_debit,
                credit=cash_credit,
            ),
            JournalPosting(
                account="paper:pnl:funding",
                asset=position.asset.value,
                debit=cash_credit,
                credit=cash_debit,
            ),
        ),
        evidence_ids=(f"{position.position_id}:{normalized_effective_at.isoformat()}",),
    )
