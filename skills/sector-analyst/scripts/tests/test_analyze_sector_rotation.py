"""Unit tests for analyze_sector_rotation.py."""

import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest
from analyze_sector_rotation import (
    CYCLICAL_SECTORS,
    DEFENSIVE_SECTORS,
    SectorData,
    analyze_groups,
    analyze_trends,
    check_freshness,
    estimate_cycle_phase,
    fetch_csv,
    format_human,
    format_json,
    identify_overbought_oversold,
    parse_sector_rows,
    rank_sectors,
)
from helpers import (
    ALL_SECTORS,
    make_early_cycle_scenario,
    make_full_sector_set,
    make_late_cycle_scenario,
    make_recession_scenario,
    make_sector_row,
)


# ---------------------------------------------------------------------------
# TestParseSectorRows
# ---------------------------------------------------------------------------
class TestParseSectorRows:
    """Tests for CSV row parsing into SectorData."""

    def test_basic_parse(self):
        rows = [make_sector_row("Technology", ratio=0.25, trend="Up", slope=0.008)]
        result = parse_sector_rows(rows)
        assert len(result) == 1
        assert result[0].sector == "Technology"
        assert result[0].ratio == pytest.approx(0.25)
        assert result[0].trend == "Up"
        assert result[0].slope == pytest.approx(0.008)

    def test_full_set_parses_all_11(self):
        rows = make_full_sector_set()
        result = parse_sector_rows(rows)
        assert len(result) == 11

    def test_missing_ratio_skips_row(self):
        row = make_sector_row("Technology")
        row["Ratio"] = ""
        result = parse_sector_rows([row])
        assert len(result) == 0

    def test_non_numeric_ratio_skips_row(self):
        row = make_sector_row("Technology")
        row["Ratio"] = "N/A"
        result = parse_sector_rows([row])
        assert len(result) == 0

    def test_missing_sector_skips_row(self):
        row = make_sector_row("Technology")
        row["Sector"] = ""
        result = parse_sector_rows([row])
        assert len(result) == 0

    def test_missing_optional_fields_use_defaults(self):
        row = {"Sector": "Technology", "Ratio": "0.30"}
        result = parse_sector_rows([row])
        assert len(result) == 1
        assert result[0].ma_10 is None
        assert result[0].slope is None
        assert result[0].status == ""

    def test_empty_list_returns_empty(self):
        assert parse_sector_rows([]) == []


# ---------------------------------------------------------------------------
# TestRankSectors
# ---------------------------------------------------------------------------
class TestRankSectors:
    """Tests for ratio-descending ranking."""

    def test_rank_order(self):
        sectors = [
            SectorData("A", 0.10, None, "Up", None, ""),
            SectorData("B", 0.30, None, "Up", None, ""),
            SectorData("C", 0.20, None, "Up", None, ""),
        ]
        ranked = rank_sectors(sectors)
        assert ranked[0]["sector"] == "B"
        assert ranked[1]["sector"] == "C"
        assert ranked[2]["sector"] == "A"
        assert ranked[0]["rank"] == 1
        assert ranked[2]["rank"] == 3

    def test_rank_includes_ratio_pct(self):
        sectors = [SectorData("A", 0.25, None, "Up", None, "")]
        ranked = rank_sectors(sectors)
        assert ranked[0]["ratio_pct"] == pytest.approx(25.0)

    def test_empty_input(self):
        assert rank_sectors([]) == []


# ---------------------------------------------------------------------------
# TestAnalyzeGroups
# ---------------------------------------------------------------------------
class TestAnalyzeGroups:
    """Tests for cyclical/defensive group analysis and risk regime."""

    def test_balanced_scenario(self):
        sectors = parse_sector_rows(make_full_sector_set(ratio=0.20))
        result = analyze_groups(sectors)
        assert result["cyclical_avg"] == pytest.approx(0.20)
        assert result["defensive_avg"] == pytest.approx(0.20)
        assert result["difference"] == pytest.approx(0.0)
        assert result["regime"] == "balanced"

    def test_risk_on_scenario(self):
        rows = []
        for s in ALL_SECTORS:
            if s in CYCLICAL_SECTORS:
                rows.append(make_sector_row(s, ratio=0.35))
            else:
                rows.append(make_sector_row(s, ratio=0.10))
        sectors = parse_sector_rows(rows)
        result = analyze_groups(sectors)
        assert result["cyclical_avg"] > result["defensive_avg"]
        assert result["regime"] in ("risk-on", "strong risk-on")

    def test_risk_off_scenario(self):
        rows = []
        for s in ALL_SECTORS:
            if s in DEFENSIVE_SECTORS:
                rows.append(make_sector_row(s, ratio=0.35))
            else:
                rows.append(make_sector_row(s, ratio=0.05))
        sectors = parse_sector_rows(rows)
        result = analyze_groups(sectors)
        assert result["defensive_avg"] > result["cyclical_avg"]
        assert result["regime"] in ("defensive tilt", "strong risk-off")

    def test_commodity_avg_calculated(self):
        sectors = parse_sector_rows(make_full_sector_set(ratio=0.20))
        result = analyze_groups(sectors)
        assert result["commodity_avg"] == pytest.approx(0.20)

    def test_late_cycle_flag(self):
        rows = make_late_cycle_scenario()
        sectors = parse_sector_rows(rows)
        result = analyze_groups(sectors)
        assert result["late_cycle_flag"] is True

    def test_score_range_0_to_100(self):
        for ratio in [0.01, 0.10, 0.20, 0.30, 0.45]:
            rows = make_full_sector_set(ratio=ratio)
            sectors = parse_sector_rows(rows)
            result = analyze_groups(sectors)
            assert 0 <= result["score"] <= 100

    def test_divergence_flag_high_spread(self):
        """High spread within cyclical group triggers divergence."""
        rows = make_full_sector_set(ratio=0.20)
        # Make Technology very high and Industrials very low
        for r in rows:
            if r["Sector"] == "Technology":
                r["Ratio"] = "0.45"
            elif r["Sector"] == "Industrials":
                r["Ratio"] = "0.05"
        sectors = parse_sector_rows(rows)
        result = analyze_groups(sectors)
        assert result["divergence_flag"] is True


# ---------------------------------------------------------------------------
# TestOverboughtOversold
# ---------------------------------------------------------------------------
class TestOverboughtOversold:
    """Tests for overbought/oversold identification."""

    def test_overbought_threshold(self):
        sectors = [SectorData("A", 0.40, None, "Up", None, "")]
        ob, os_ = identify_overbought_oversold(sectors)
        assert len(ob) == 1
        assert ob[0]["sector"] == "A"

    def test_oversold_threshold(self):
        sectors = [SectorData("A", 0.05, None, "Down", None, "")]
        ob, os_ = identify_overbought_oversold(sectors)
        assert len(os_) == 1
        assert os_[0]["sector"] == "A"

    def test_normal_range_no_flags(self):
        sectors = [SectorData("A", 0.20, None, "Up", None, "")]
        ob, os_ = identify_overbought_oversold(sectors)
        assert len(ob) == 0
        assert len(os_) == 0

    def test_boundary_values(self):
        # Exactly at threshold
        sectors = [
            SectorData("A", 0.37, None, "Up", None, ""),
            SectorData("B", 0.097, None, "Down", None, ""),
        ]
        ob, os_ = identify_overbought_oversold(sectors)
        # At threshold = not triggered (must exceed)
        assert len(ob) == 0
        assert len(os_) == 0


# ---------------------------------------------------------------------------
# TestAnalyzeTrends
# ---------------------------------------------------------------------------
class TestAnalyzeTrends:
    """Tests for trend analysis."""

    def test_all_up(self):
        sectors = parse_sector_rows(make_full_sector_set(trend="Up"))
        result = analyze_trends(sectors)
        assert result["uptrend_count"] == 11
        assert result["downtrend_count"] == 0

    def test_mixed_trends(self):
        rows = make_early_cycle_scenario()
        sectors = parse_sector_rows(rows)
        result = analyze_trends(sectors)
        assert result["uptrend_count"] > 0
        assert result["downtrend_count"] > 0
        assert result["uptrend_count"] + result["downtrend_count"] == 11


# ---------------------------------------------------------------------------
# TestEstimateCyclePhase
# ---------------------------------------------------------------------------
class TestEstimateCyclePhase:
    """Tests for market cycle phase estimation."""

    def test_early_cycle(self):
        sectors = parse_sector_rows(make_early_cycle_scenario())
        result = estimate_cycle_phase(sectors)
        assert result["phase"] == "early"

    def test_late_cycle(self):
        sectors = parse_sector_rows(make_late_cycle_scenario())
        result = estimate_cycle_phase(sectors)
        assert result["phase"] == "late"

    def test_recession(self):
        sectors = parse_sector_rows(make_recession_scenario())
        result = estimate_cycle_phase(sectors)
        assert result["phase"] == "recession"

    def test_confidence_high_when_clear(self):
        sectors = parse_sector_rows(make_early_cycle_scenario())
        result = estimate_cycle_phase(sectors)
        assert result["confidence"] in ("high", "moderate")

    def test_scores_dict_has_all_phases(self):
        sectors = parse_sector_rows(make_full_sector_set())
        result = estimate_cycle_phase(sectors)
        for phase in ("early", "mid", "late", "recession"):
            assert phase in result["scores"]

    def test_evidence_list_not_empty(self):
        sectors = parse_sector_rows(make_early_cycle_scenario())
        result = estimate_cycle_phase(sectors)
        assert len(result["evidence"]) > 0


# ---------------------------------------------------------------------------
# TestFormatHuman
# ---------------------------------------------------------------------------
class TestFormatHuman:
    """Tests for human-readable output formatting."""

    def test_contains_required_sections(self):
        sectors = parse_sector_rows(make_full_sector_set())
        ranking = rank_sectors(sectors)
        groups = analyze_groups(sectors)
        ob, os_ = identify_overbought_oversold(sectors)
        trends = analyze_trends(sectors)
        cycle = estimate_cycle_phase(sectors)
        output = format_human(ranking, groups, ob, os_, trends, cycle, None)
        assert "Sector Ranking" in output
        assert "Risk Regime" in output
        assert "Cycle Phase" in output

    def test_freshness_warning_included(self):
        sectors = parse_sector_rows(make_full_sector_set())
        ranking = rank_sectors(sectors)
        groups = analyze_groups(sectors)
        ob, os_ = identify_overbought_oversold(sectors)
        trends = analyze_trends(sectors)
        cycle = estimate_cycle_phase(sectors)
        freshness = {"date": "2025-01-01", "is_fresh": False, "warning": "Data is 60 days old"}
        output = format_human(ranking, groups, ob, os_, trends, cycle, freshness)
        assert "WARNING" in output or "Data is 60 days old" in output


# ---------------------------------------------------------------------------
# TestFormatJson
# ---------------------------------------------------------------------------
class TestFormatJson:
    """Tests for JSON output formatting."""

    def test_valid_json(self):
        sectors = parse_sector_rows(make_full_sector_set())
        ranking = rank_sectors(sectors)
        groups = analyze_groups(sectors)
        ob, os_ = identify_overbought_oversold(sectors)
        trends = analyze_trends(sectors)
        cycle = estimate_cycle_phase(sectors)
        output = format_json(ranking, groups, ob, os_, trends, cycle, None)
        data = json.loads(output)
        assert "ranking" in data
        assert "groups" in data
        assert "cycle_phase" in data

    def test_json_has_meta_section(self):
        sectors = parse_sector_rows(make_full_sector_set())
        ranking = rank_sectors(sectors)
        groups = analyze_groups(sectors)
        ob, os_ = identify_overbought_oversold(sectors)
        trends = analyze_trends(sectors)
        cycle = estimate_cycle_phase(sectors)
        output = format_json(ranking, groups, ob, os_, trends, cycle, None)
        data = json.loads(output)
        assert "meta" in data
        assert "generated_at" in data["meta"]


# ---------------------------------------------------------------------------
# TestFetchCsv
# ---------------------------------------------------------------------------
class TestFetchCsv:
    """Tests for CSV fetching with mocked urllib."""

    @patch("analyze_sector_rotation.urllib.request.urlopen")
    def test_fetch_csv_success(self, mock_urlopen):
        csv_content = (
            "Sector,Ratio,MA_10,Trend,Slope,Status\nTechnology,0.25,0.24,Up,0.005,Above MA\n"
        )
        mock_response = MagicMock()
        mock_response.read.return_value = csv_content.encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        rows = fetch_csv("https://example.com/data.csv")
        assert len(rows) == 1
        assert rows[0]["Sector"] == "Technology"

    @patch("analyze_sector_rotation.urllib.request.urlopen")
    def test_fetch_csv_error(self, mock_urlopen):
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("connection failed")
        with pytest.raises(SystemExit):
            fetch_csv("https://example.com/data.csv")

    @patch("analyze_sector_rotation.urllib.request.urlopen")
    def test_fetch_csv_empty_response(self, mock_urlopen):
        csv_content = "Sector,Ratio,MA_10,Trend,Slope,Status\n"
        mock_response = MagicMock()
        mock_response.read.return_value = csv_content.encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        rows = fetch_csv("https://example.com/data.csv")
        assert len(rows) == 0


# ---------------------------------------------------------------------------
# TestCheckFreshness
# ---------------------------------------------------------------------------
class TestCheckFreshness:
    """Tests for data freshness checking."""

    @patch("analyze_sector_rotation.fetch_csv")
    def test_fresh_data(self, mock_fetch):
        today = date.today()
        yesterday = today - timedelta(days=1)
        mock_fetch.return_value = [
            {"date": str(today - timedelta(days=10))},
            {"date": str(yesterday)},  # Latest
            {"date": str(today - timedelta(days=5))},
        ]
        result = check_freshness("https://example.com/ts.csv")
        assert result["date"] == str(yesterday)
        assert result["is_fresh"] is True
        assert result["warning"] is None

    @patch("analyze_sector_rotation.fetch_csv")
    def test_stale_data(self, mock_fetch):
        old_date = date.today() - timedelta(days=10)
        mock_fetch.return_value = [
            {"date": str(old_date - timedelta(days=5))},
            {"date": str(old_date)},
        ]
        result = check_freshness("https://example.com/ts.csv")
        assert result["is_fresh"] is False
        assert result["warning"] is not None

    @patch("analyze_sector_rotation.fetch_csv")
    def test_fetch_failure_returns_none(self, mock_fetch):
        mock_fetch.side_effect = SystemExit(1)
        result = check_freshness("https://example.com/ts.csv")
        assert result is None

    @patch("analyze_sector_rotation.fetch_csv")
    def test_order_independent(self, mock_fetch):
        """Max date is found regardless of row order."""
        today = date.today()
        mock_fetch.return_value = [
            {"date": str(today - timedelta(days=3))},
            {"date": str(today)},
            {"date": str(today - timedelta(days=7))},
        ]
        result = check_freshness("https://example.com/ts.csv")
        assert result["date"] == str(today)

    @patch("analyze_sector_rotation.fetch_csv")
    def test_empty_timeseries(self, mock_fetch):
        mock_fetch.return_value = []
        result = check_freshness("https://example.com/ts.csv")
        assert result is None
