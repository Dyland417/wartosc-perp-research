"""Pure deterministic alignment of actual funding events to prior oracle observations."""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"'{field_name}' must be timezone-aware")
    return value.astimezone(UTC)


def _seconds(value: timedelta) -> Decimal:
    microseconds = (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds
    return Decimal(microseconds) / Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class OracleSourceProvenance:
    bucket: str
    object_key: str
    archive_sha256: str
    etag: str | None
    object_size: int
    last_modified: datetime | None
    retrieved_at: datetime | None
    source_row_number: int
    source_row_sha256: str
    schema_version: str
    source_revision: bool


def oracle_source_sort_key(source: OracleSourceProvenance) -> tuple[object, ...]:
    return (
        source.bucket,
        source.object_key,
        source.archive_sha256,
        source.source_row_number,
        source.source_row_sha256,
    )


@dataclass(frozen=True, slots=True)
class StoredFundingEvent:
    funding_id: int
    symbol: str
    event_time: datetime
    rate: Decimal
    interval_seconds: int
    is_predicted: bool
    received_at: datetime
    ingested_at: datetime
    ingestion_run_id: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_time", _utc(self.event_time, "event_time"))
        object.__setattr__(self, "received_at", _utc(self.received_at, "received_at"))
        object.__setattr__(self, "ingested_at", _utc(self.ingested_at, "ingested_at"))
        if not isinstance(self.rate, Decimal) or not self.rate.is_finite():
            raise TypeError("Funding rate must be a finite Decimal")


@dataclass(frozen=True, slots=True)
class StoredOracleObservation:
    observation_id: int
    symbol: str
    event_time: datetime
    oracle_price: Decimal
    is_conflicting: bool
    sources: tuple[OracleSourceProvenance, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_time", _utc(self.event_time, "event_time"))
        if (
            not isinstance(self.oracle_price, Decimal)
            or not self.oracle_price.is_finite()
            or self.oracle_price <= 0
        ):
            raise TypeError("Oracle price must be a positive finite Decimal")
        object.__setattr__(self, "sources", tuple(sorted(self.sources, key=oracle_source_sort_key)))


@dataclass(frozen=True, slots=True)
class FundingOracleAlignment:
    funding: StoredFundingEvent
    status: str
    reason: str | None
    oracle_event_time: datetime | None
    oracle_price: Decimal | None
    oracle_age_seconds: Decimal | None
    oracle_observation_ids: tuple[int, ...]
    conflicting_prices: tuple[Decimal, ...]
    oracle_sources: tuple[OracleSourceProvenance, ...]


@dataclass(frozen=True, slots=True)
class SymbolAlignmentCoverage:
    symbol: str
    requested_funding_events: int
    aligned_events: int
    unaligned_events: int
    stale_events: int
    missing_oracle_events: int
    conflicting_oracle_events: int
    coverage_percentage: Decimal


@dataclass(frozen=True, slots=True)
class FundingOracleDataset:
    exchange: str
    symbols: tuple[str, ...]
    start: datetime
    end: datetime
    max_oracle_age_seconds: Decimal
    alignments: tuple[FundingOracleAlignment, ...]
    coverage: tuple[SymbolAlignmentCoverage, ...]
    archive_provenance: tuple[OracleSourceProvenance, ...]
    malformed_archive_rows: int
    conflicting_observations: int
    source_revisions: int


def align_funding_to_oracles(
    *,
    exchange: str,
    symbols: list[str] | tuple[str, ...],
    start: datetime,
    end: datetime,
    max_oracle_age: timedelta,
    funding_events: list[StoredFundingEvent],
    oracle_observations: list[StoredOracleObservation],
    malformed_archive_rows: int = 0,
    source_revisions: int = 0,
) -> FundingOracleDataset:
    """Use only the latest oracle timestamp at or before each actual funding event."""

    start = _utc(start, "start")
    end = _utc(end, "end")
    if end <= start:
        raise ValueError("'end' must be after 'start'")
    if max_oracle_age <= timedelta(0):
        raise ValueError("'max_oracle_age' must be positive")
    normalized_symbols = tuple(sorted({symbol.strip() for symbol in symbols if symbol.strip()}))
    if not normalized_symbols:
        raise ValueError("At least one symbol is required")

    actual_funding = sorted(
        (
            item
            for item in funding_events
            if not item.is_predicted
            and item.symbol in normalized_symbols
            and start <= item.event_time < end
        ),
        key=lambda item: (item.symbol, item.event_time, item.funding_id),
    )
    oracle_groups: dict[str, dict[datetime, list[StoredOracleObservation]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for observation in oracle_observations:
        if observation.symbol in normalized_symbols and observation.event_time < end:
            oracle_groups[observation.symbol][observation.event_time].append(observation)

    ordered_times = {symbol: sorted(groups) for symbol, groups in oracle_groups.items()}
    alignments: list[FundingOracleAlignment] = []
    for funding in actual_funding:
        times = ordered_times.get(funding.symbol, [])
        index = bisect_right(times, funding.event_time) - 1
        if index < 0:
            alignments.append(
                FundingOracleAlignment(
                    funding=funding,
                    status="unaligned",
                    reason="missing_oracle",
                    oracle_event_time=None,
                    oracle_price=None,
                    oracle_age_seconds=None,
                    oracle_observation_ids=(),
                    conflicting_prices=(),
                    oracle_sources=(),
                )
            )
            continue
        oracle_time = times[index]
        candidates = oracle_groups[funding.symbol][oracle_time]
        observation_ids = tuple(sorted(candidate.observation_id for candidate in candidates))
        prices = tuple(sorted({candidate.oracle_price for candidate in candidates}))
        sources = tuple(
            sorted(
                {source for candidate in candidates for source in candidate.sources},
                key=oracle_source_sort_key,
            )
        )
        age = funding.event_time - oracle_time
        age_seconds = _seconds(age)
        if len(prices) != 1 or any(candidate.is_conflicting for candidate in candidates):
            alignments.append(
                FundingOracleAlignment(
                    funding=funding,
                    status="unaligned",
                    reason="conflicting_oracle",
                    oracle_event_time=oracle_time,
                    oracle_price=None,
                    oracle_age_seconds=age_seconds,
                    oracle_observation_ids=observation_ids,
                    conflicting_prices=prices,
                    oracle_sources=sources,
                )
            )
        elif age > max_oracle_age:
            alignments.append(
                FundingOracleAlignment(
                    funding=funding,
                    status="unaligned",
                    reason="stale_oracle",
                    oracle_event_time=oracle_time,
                    oracle_price=prices[0],
                    oracle_age_seconds=age_seconds,
                    oracle_observation_ids=observation_ids,
                    conflicting_prices=(),
                    oracle_sources=sources,
                )
            )
        else:
            alignments.append(
                FundingOracleAlignment(
                    funding=funding,
                    status="aligned",
                    reason=None,
                    oracle_event_time=oracle_time,
                    oracle_price=prices[0],
                    oracle_age_seconds=age_seconds,
                    oracle_observation_ids=observation_ids,
                    conflicting_prices=(),
                    oracle_sources=sources,
                )
            )

    coverage: list[SymbolAlignmentCoverage] = []
    for symbol in normalized_symbols:
        rows = [item for item in alignments if item.funding.symbol == symbol]
        aligned = sum(item.status == "aligned" for item in rows)
        stale = sum(item.reason == "stale_oracle" for item in rows)
        missing = sum(item.reason == "missing_oracle" for item in rows)
        conflicts = sum(item.reason == "conflicting_oracle" for item in rows)
        percentage = Decimal(aligned) * Decimal(100) / Decimal(len(rows)) if rows else Decimal(0)
        coverage.append(
            SymbolAlignmentCoverage(
                symbol,
                len(rows),
                aligned,
                len(rows) - aligned,
                stale,
                missing,
                conflicts,
                percentage,
            )
        )

    provenance = tuple(
        sorted(
            {source for observation in oracle_observations for source in observation.sources},
            key=oracle_source_sort_key,
        )
    )
    conflicting_observations = sum(item.is_conflicting for item in oracle_observations)
    return FundingOracleDataset(
        exchange=exchange,
        symbols=normalized_symbols,
        start=start,
        end=end,
        max_oracle_age_seconds=_seconds(max_oracle_age),
        alignments=tuple(alignments),
        coverage=tuple(coverage),
        archive_provenance=provenance,
        malformed_archive_rows=malformed_archive_rows,
        conflicting_observations=conflicting_observations,
        source_revisions=source_revisions,
    )
