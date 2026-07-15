import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from wartosc_perp_research.research.funding import (
    FundingObservation,
    FundingStudy,
    analyze_funding_study,
)
from wartosc_perp_research.research.funding_report import (
    ReportOutputError,
    funding_study_to_dict,
    render_funding_markdown,
    write_funding_report,
)


def _study() -> FundingStudy:
    start = datetime(2026, 1, 31, 23, tzinfo=UTC)
    return analyze_funding_study(
        exchange="hyperliquid",
        symbols=["BTC"],
        start=start,
        end=start + timedelta(hours=3),
        observations=[
            FundingObservation("BTC", start, Decimal("0.001"), 3600),
            FundingObservation("BTC", start + timedelta(hours=2), Decimal("-0.002"), 3600),
        ],
    )


def test_machine_readable_report_has_explicit_methodology() -> None:
    payload = funding_study_to_dict(_study())
    instrument = payload["instruments"][0]

    assert payload["schema_version"] == 1
    assert payload["timestamp_source"] == "exchange_event_time"
    assert payload["grid_alignment_tolerance_seconds"] == 1
    assert payload["annualization_method"].endswith("no_compounding")
    assert payload["standard_deviation_method"] == "population"
    assert payload["funding_sign_convention"] == {
        "positive_rate": "long_pays_short_receives",
        "negative_rate": "short_pays_long_receives",
        "cash_flow_sign": "positive_received_negative_paid",
    }
    assert instrument["observation_count"] == 2
    assert instrument["missing_expected_observation_count"] == 1
    assert instrument["mean_hourly_rate"] == "-0.0005"
    assert instrument["cumulative_signed_funding_rate"] == "-0.001"
    assert instrument["long_net_funding_cash_flow"] == "0.001"
    assert instrument["short_net_funding_cash_flow"] == "-0.001"
    assert [item["bucket"] for item in instrument["results_by_month"]] == [
        "2026-01",
        "2026-02",
    ]


def test_markdown_is_explanatory_and_not_presented_as_backtest() -> None:
    markdown = render_funding_markdown(_study())

    assert "It is not a backtest" in markdown
    assert "Missing expected timestamps" in markdown
    assert "Annualized funding is a simple extrapolation" in markdown
    assert "original exchange timestamps are preserved" in markdown
    assert "fees, slippage, liquidity, liquidation" in markdown
    assert "Results by month" in markdown
    assert "Results by UTC hour" in markdown
    assert "positive means longs pay and shorts receive" in markdown
    assert "Long net funding cash flow (+ received / - paid)" in markdown
    assert "× 8,760 (365 × 24)" in markdown
    assert "**DATA WARNING:**" in markdown


def test_report_writes_are_byte_reproducible(tmp_path: Path) -> None:
    study = _study()

    first = write_funding_report(study, tmp_path)
    first_json = first.json_path.read_bytes()
    first_markdown = first.markdown_path.read_bytes()
    second = write_funding_report(study, tmp_path)

    assert second.json_path.read_bytes() == first_json
    assert second.markdown_path.read_bytes() == first_markdown
    assert json.loads(first_json)["study_type"] == "observed_funding_rate_descriptive_analysis"
    assert not list(tmp_path.glob("*.tmp"))


def test_report_refuses_changed_overwrite_without_explicit_permission(tmp_path: Path) -> None:
    first = write_funding_report(_study(), tmp_path)
    original = first.json_path.read_bytes()
    start = datetime(2026, 1, 31, 23, tzinfo=UTC)
    changed = analyze_funding_study(
        exchange="hyperliquid",
        symbols=["BTC"],
        start=start,
        end=start + timedelta(hours=3),
        observations=[FundingObservation("BTC", start, Decimal("0.009"), 3600)],
    )

    with pytest.raises(ReportOutputError, match="--overwrite"):
        write_funding_report(changed, tmp_path)
    assert first.json_path.read_bytes() == original

    write_funding_report(changed, tmp_path, overwrite=True)
    assert first.json_path.read_bytes() != original


def test_report_rejects_output_path_that_is_a_file(tmp_path: Path) -> None:
    output = tmp_path / "not-a-directory"
    output.write_text("occupied", encoding="utf-8")

    with pytest.raises(ReportOutputError, match="not a directory"):
        write_funding_report(_study(), output)


def test_results_do_not_depend_on_callers_decimal_context() -> None:
    expected = render_funding_markdown(_study())
    with localcontext() as context:
        context.prec = 6
        observed = render_funding_markdown(_study())

    assert observed == expected
