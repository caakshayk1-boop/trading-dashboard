"""
Auto-deploy pipeline: signals.db → Dhruvedge (terminal.askakshay.com)

Reads OPEN A/A+ signals from signals.db, writes:
  - ~/akk-terminal/public/signals.json   (Setups page)
  - ~/akk-terminal/public/portfolio.json (Portfolio page)
Then triggers Vercel production deploy.

Called automatically after every scanner run via scheduler.py.
"""
import json
import logging
import os
import subprocess
import sqlite3
from datetime import datetime, timezone, timedelta
import db

IST = timezone(timedelta(hours=5, minutes=30))

DHRUVEDGE_DIR = os.path.expanduser("~/akk-terminal")
PUBLIC_DIR    = os.path.join(DHRUVEDGE_DIR, "public")

CAPITAL = 500000


def _score_to_conviction(score: int) -> str:
    if score >= 80: return "A+"
    if score >= 65: return "A"
    if score >= 50: return "B"
    return "C"


def _nse_to_yahoo(symbol: str) -> str:
    """Best-effort NSE → Yahoo Finance symbol mapping."""
    overrides = {
        "M&M":          "M%26M.NS",
        "MCDOWELL-N":   "MCDOWELL-N.NS",
        "HDFC BANK":    "HDFCBANK.NS",
        "ICICI BANK":   "ICICIBANK.NS",
    }
    clean = symbol.strip().upper()
    return overrides.get(clean, f"{clean}.NS")


def _read_open_signals(min_score: int = 65) -> list:
    """Read OPEN A/A+ signals from Turso/signals.db (all_signals table)."""
    try:
        con = db.connect()
        con.row_factory = db.Row
        cur = con.execute(
            """SELECT * FROM all_signals
               WHERE status='OPEN' AND score >= ?
               ORDER BY score DESC, date DESC""",
            (min_score,)
        )
        rows = [dict(r) for r in cur.fetchall()]
        con.close()
        return rows
    except Exception as e:
        logging.error(f"DB read error: {e}")
        return []


def build_signals_json(rows: list) -> dict:
    """Build signals.json in the format Dhruvedge API expects."""
    now_ist = datetime.now(IST).strftime("%Y-%m-%d")
    return {
        "all_signals": rows,
        "signals":     [],
        "exported_at": now_ist,
    }


def build_portfolio_json(rows: list) -> dict:
    """
    Build portfolio.json from OPEN A/A+ signals.
    Each signal becomes a portfolio position.
    """
    now_ist = datetime.now(IST).isoformat()
    positions = []
    total_invested = 0

    for r in rows:
        score     = int(r.get("score") or 0)
        conviction = _score_to_conviction(score)
        symbol    = (r.get("symbol") or "").strip().upper()
        entry     = float(r.get("entry") or 0)
        sl        = float(r.get("sl") or r.get("sl2") or entry * 0.96)
        target    = float(r.get("target1") or entry * 1.05)
        target2   = float(r.get("target2") or target * 1.02)

        if entry <= 0 or symbol == "":
            continue

        # Qty: from metadata if available, else calculate from CAPITAL * 1% risk
        qty = 0
        try:
            meta = json.loads(r.get("metadata") or "{}")
            qty  = int(meta.get("qty") or 0)
        except Exception:
            pass
        if qty <= 0:
            risk_per_share = max(entry - sl, 0.01)
            qty = max(1, int((CAPITAL * 0.01) / risk_per_share))

        # Cap position at 20% of capital
        max_qty = int((CAPITAL * 0.20) / entry)
        qty = min(qty, max_qty)

        invested = entry * qty
        total_invested += invested

        # Thesis from metadata.reasons (obfuscated by scanner)
        thesis = ""
        try:
            meta   = json.loads(r.get("metadata") or "{}")
            reasons = meta.get("reasons", "")
            if isinstance(reasons, list):
                thesis = ". ".join(reasons[:3])
            elif isinstance(reasons, str):
                thesis = reasons[:200]
        except Exception:
            pass
        if not thesis:
            thesis = f"{r.get('signal_type','Setup')} · Score {score}/100"

        positions.append({
            "symbol":      symbol,
            "yahooSymbol": _nse_to_yahoo(symbol),
            "qty":         qty,
            "entryPrice":  round(entry, 2),
            "entryDate":   r.get("date") or datetime.now(IST).strftime("%Y-%m-%d"),
            "target":      round(target, 2),
            "target2":     round(target2, 2),
            "sl":          round(sl, 2),
            "conviction":  conviction,
            "setup":       r.get("signal_type") or r.get("setup_type") or "Swing",
            "thesis":      thesis,
        })

    cash = max(0, CAPITAL - total_invested)
    return {
        "capital":     CAPITAL,
        "cash":        round(cash, 2),
        "positions":   positions,
        "updatedAt":   now_ist,
    }


def write_files(signals_data: dict, portfolio_data: dict) -> bool:
    """Write both JSON files to akk-terminal/public/."""
    if not os.path.isdir(PUBLIC_DIR):
        logging.error(f"Dhruvedge public dir not found: {PUBLIC_DIR}")
        return False
    try:
        sig_path  = os.path.join(PUBLIC_DIR, "signals.json")
        port_path = os.path.join(PUBLIC_DIR, "portfolio.json")
        with open(sig_path, "w") as f:
            json.dump(signals_data, f, default=str, indent=2)
        with open(port_path, "w") as f:
            json.dump(portfolio_data, f, default=str, indent=2)
        logging.info(f"Wrote signals.json ({len(signals_data['all_signals'])} signals) + portfolio.json ({len(portfolio_data['positions'])} positions)")
        return True
    except Exception as e:
        logging.error(f"File write error: {e}")
        return False


def deploy_vercel() -> bool:
    """Trigger Vercel production deploy of Dhruvedge."""
    if not os.path.isdir(DHRUVEDGE_DIR):
        logging.error(f"Dhruvedge dir not found: {DHRUVEDGE_DIR}")
        return False
    try:
        result = subprocess.run(
            ["vercel", "--prod", "--yes"],
            cwd=DHRUVEDGE_DIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            logging.info("Vercel deploy success")
            return True
        else:
            logging.error(f"Vercel deploy failed: {result.stderr[:300]}")
            return False
    except subprocess.TimeoutExpired:
        logging.error("Vercel deploy timed out (120s)")
        return False
    except FileNotFoundError:
        logging.error("vercel CLI not found — install with: npm i -g vercel")
        return False
    except Exception as e:
        logging.error(f"Deploy error: {e}")
        return False


def run(min_score: int = 65, deploy: bool = True) -> bool:
    """
    Full pipeline: read DB → write JSON files → deploy.
    Returns True if all steps succeeded.
    """
    logging.info("=== Dhruvedge deploy pipeline started ===")

    rows = _read_open_signals(min_score=min_score)
    # Only A/A+ in portfolio (score ≥ 65), but keep all OPEN in signals view
    portfolio_rows = [r for r in rows if int(r.get("score") or 0) >= 65]

    signals_data   = build_signals_json(rows)
    portfolio_data = build_portfolio_json(portfolio_rows)

    ok = write_files(signals_data, portfolio_data)
    if not ok:
        return False

    if deploy:
        return deploy_vercel()
    return True


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    no_deploy = "--no-deploy" in sys.argv
    success = run(deploy=not no_deploy)
    sys.exit(0 if success else 1)
