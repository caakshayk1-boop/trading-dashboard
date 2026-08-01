"""
signals/quality.py — the gate every signal must clear before it is published.

Why this exists
---------------
Measured over 476 closed trades the system wins 36.8% of the time. A trade at
R:R 1.5 needs a 40% win rate just to break even, so an R:R-1.5 signal is a
losing trade at this hit rate no matter how clean the chart looks. On
2026-08-01, 18 of 23 published signals were below 2.0R and one was 0.5R — a
setup needing a 66.7% win rate.

The breakout scan, which produced 21 of those 23, had no R:R floor at all: it
sorted by R:R and published everything. config.MIN_RR = 2.0 already existed and
was imported by scan_all(), which then ignored it in favour of a hardcoded 1.5.

Break-even win rate is 1/(1+R). That single identity is why min_rr is the one
gate that is never relaxed:

    R:R 1.0 → 50.0%      R:R 2.0 → 33.3%
    R:R 1.5 → 40.0%      R:R 2.5 → 28.6%

Fail-open vs fail-closed
------------------------
Price gates (R:R, liquidity) fail CLOSED — the data is always present, so a
failure is a real rejection. Fundamental gates fail OPEN into an UNVERIFIED
grade when Yahoo has no data for a symbol, because a Yahoo outage must not
quietly produce a zero-signal day that looks like "nothing qualified today".
Callers that want only fully-verified signals filter on grade.
"""

# CI runs 3.11 but local dev is on 3.9, where `float | None` in a default-arg
# annotation is evaluated at import time and raises. Deferring annotations
# keeps one source working on both.
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

__all__ = ["GateConfig", "QualityResult", "DEFAULT", "qualify", "rr_of",
           "breakeven_win_rate"]


@dataclass(frozen=True)
class GateConfig:
    # ── price gates: always enforced ─────────────────────────────────────────
    min_rr: float = 2.0                 # break-even 33.3% vs measured 36.8%
    min_turnover_cr: float = 25.0       # 20d avg traded value, ₹ crore
    min_price: float = 50.0

    # ── fundamental gates: enforced when data exists ─────────────────────────
    min_market_cap_cr: float = 5000.0
    max_pe: float = 80.0
    min_roe: float = 0.15               # 15%
    min_revenue_growth: float = 0.10    # 10% yoy (Yahoo gives yoy, not 3y CAGR)
    max_debt_to_equity: float = 1.5     # skipped for banks/NBFCs/real estate
    require_positive_pat: bool = True
    earnings_blackout_days: int = 5     # no entry this close to results

    # Grade A additionally wants both growth *and* return on equity, not either.
    grade_a_min_rr: float = 2.5


DEFAULT = GateConfig()


@dataclass
class QualityResult:
    passed: bool
    grade: str                                    # "A" | "B" | "UNVERIFIED" | "REJECT"
    rr: float
    rejections: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)

    @property
    def reason(self) -> str:
        return "; ".join(self.rejections) if self.rejections else "all gates passed"

    def as_metadata(self) -> dict:
        """Compact form for the all_signals.metadata JSON blob."""
        return {
            "grade": self.grade,
            "rr": round(self.rr, 2),
            "breakeven_wr": round(breakeven_win_rate(self.rr) * 100, 1),
            "warnings": self.warnings,
            "checks": self.checks,
        }


def rr_of(entry: float, sl: float, target: float) -> float:
    """Reward-to-risk against the FIRST target. T1 is what actually gets hit."""
    risk = entry - sl
    if risk <= 0:
        return 0.0
    return (target - entry) / risk


def breakeven_win_rate(rr: float) -> float:
    return 1.0 / (1.0 + rr) if rr > 0 else 1.0


def _parse_date(v):
    if not v:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        return datetime.fromisoformat(str(v)[:10]).date()
    except ValueError:
        return None


def qualify(symbol: str, entry: float, sl: float, target1: float,
            avg_turnover_cr: float | None = None,
            fund: dict | None = None,
            cfg: GateConfig = DEFAULT,
            today: date | None = None) -> QualityResult:
    """Run every gate. Returns the verdict — never raises, never fetches."""
    today = today or date.today()
    rejections: list[str] = []
    warnings: list[str] = []
    checks: dict = {}

    # ── 1. Risk/reward — the gate that matters most ──────────────────────────
    rr = rr_of(entry, sl, target1)
    checks["rr"] = round(rr, 2)
    checks["breakeven_wr_pct"] = round(breakeven_win_rate(rr) * 100, 1)
    if rr < cfg.min_rr:
        rejections.append(
            f"R:R {rr:.2f} < {cfg.min_rr} (needs "
            f"{breakeven_win_rate(rr) * 100:.0f}% win rate to break even)"
        )

    # ── 2. Price floor ───────────────────────────────────────────────────────
    checks["price"] = round(entry, 2)
    if entry < cfg.min_price:
        rejections.append(f"price ₹{entry:.2f} < ₹{cfg.min_price:.0f}")

    # ── 3. Liquidity — can this position actually be exited ──────────────────
    if avg_turnover_cr is not None:
        checks["turnover_cr"] = round(avg_turnover_cr, 1)
        if avg_turnover_cr < cfg.min_turnover_cr:
            rejections.append(
                f"20d turnover ₹{avg_turnover_cr:.1f}cr < ₹{cfg.min_turnover_cr:.0f}cr")
    else:
        warnings.append("turnover unknown")

    # ── 4. Fundamentals — fail open into UNVERIFIED when Yahoo has nothing ───
    verified = False
    if not fund:
        warnings.append("fundamentals unavailable")
    else:
        verified = True

        mcap = fund.get("market_cap_cr")
        if mcap is not None:
            checks["market_cap_cr"] = round(mcap)
            if mcap < cfg.min_market_cap_cr:
                rejections.append(
                    f"market cap ₹{mcap:,.0f}cr < ₹{cfg.min_market_cap_cr:,.0f}cr")
        else:
            warnings.append("market cap unknown")

        pat = fund.get("net_income")
        if pat is not None:
            checks["pat_positive"] = pat > 0
            if cfg.require_positive_pat and pat <= 0:
                rejections.append("trailing PAT is negative")
        else:
            warnings.append("PAT unknown")

        pe = fund.get("pe")
        if pe is not None:
            checks["pe"] = round(pe, 1)
            if pe > cfg.max_pe:
                rejections.append(f"PE {pe:.0f}x > {cfg.max_pe:.0f}x")
            elif pe <= 0:
                rejections.append("PE not meaningful (loss-making)")
        else:
            warnings.append("PE unknown")

        # Debt is meaningless for lenders — skip the gate, record why.
        dte = fund.get("debt_to_equity")
        if fund.get("sector") in {"Financial Services", "Financials", "Real Estate"}:
            checks["debt_to_equity"] = "n/a (financials)"
        elif dte is not None:
            checks["debt_to_equity"] = round(dte, 2)
            if dte > cfg.max_debt_to_equity:
                rejections.append(f"D/E {dte:.2f} > {cfg.max_debt_to_equity}")
        else:
            warnings.append("D/E unknown")

        # Growth OR quality — a compounder with flat revenue but 20% ROE is
        # still a business worth owning, and so is a 25%-grower reinvesting
        # hard enough to hold ROE down. Requiring both rejects most of the
        # market; requiring neither is not a gate at all.
        roe = fund.get("roe")
        rev = fund.get("revenue_growth")
        checks["roe"] = round(roe, 3) if roe is not None else None
        checks["revenue_growth"] = round(rev, 3) if rev is not None else None
        roe_ok = roe is not None and roe >= cfg.min_roe
        rev_ok = rev is not None and rev >= cfg.min_revenue_growth
        if roe is None and rev is None:
            warnings.append("growth and ROE both unknown")
        elif not (roe_ok or rev_ok):
            rejections.append(
                f"neither ROE ≥{cfg.min_roe:.0%} ({_pct(roe)}) nor "
                f"revenue growth ≥{cfg.min_revenue_growth:.0%} ({_pct(rev)})")
        checks["growth_ok"] = roe_ok or rev_ok

        # ── 5. Earnings blackout ─────────────────────────────────────────────
        nxt = _parse_date(fund.get("next_earnings"))
        if nxt:
            days = (nxt - today).days
            checks["days_to_earnings"] = days
            if 0 <= days <= cfg.earnings_blackout_days:
                rejections.append(f"results in {days}d — inside blackout")
        else:
            warnings.append("earnings date unknown")

    # ── Verdict ──────────────────────────────────────────────────────────────
    passed = not rejections
    if not passed:
        grade = "REJECT"
    elif not verified:
        grade = "UNVERIFIED"
    elif rr >= cfg.grade_a_min_rr and checks.get("growth_ok"):
        grade = "A"
    else:
        grade = "B"

    return QualityResult(passed=passed, grade=grade, rr=rr,
                         rejections=rejections, warnings=warnings, checks=checks)


def _pct(v):
    return "n/a" if v is None else f"{v:.0%}"
