#!/usr/bin/env python3
"""
test_entitlements.py — the access-control rules, as rules.

Every case is a way a subscription product leaks revenue or locks out a paying
customer. They are written before any store account exists precisely so the
rules can be argued about now, not inferred from three webhook handlers later.

The two that matter most:
  * a refund or chargeback on ANY platform ends premium on ALL of them
  * anything unrecognised DENIES

Offline. No network, no store, no pytest.

Usage:
    python3 edge/test_entitlements.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from edge import entitlements as e

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def later(**kw):
    return (NOW + timedelta(**kw)).isoformat()


def earlier(**kw):
    return (NOW - timedelta(**kw)).isoformat()


def sub(platform=e.APPLE, status=e.ACTIVE, expires=None):
    return {"platform": platform, "status": status,
            "expires_at": later(days=30) if expires is None else expires}


CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


# ── Granting ─────────────────────────────────────────────────────────────────

@check("an active subscription inside its period grants premium")
def _():
    assert e.resolve([sub()], NOW)["premium"] is True


@check("a trial grants premium")
def _():
    assert e.resolve([sub(status=e.TRIAL)], NOW)["premium"] is True


@check("cancelled but not yet expired keeps access — the month was paid for")
def _():
    r = e.resolve([sub(status=e.CANCELLED)], NOW)
    assert r["premium"] is True
    assert r["will_renew"] is False


@check("a failed card in grace period does NOT lock the user out")
def _():
    # Locking someone out the hour their card expires is how a subscription
    # loses a customer it was about to keep. The store is still retrying.
    r = e.resolve([sub(status=e.GRACE_PERIOD, expires=earlier(days=2))], NOW)
    assert r["premium"] is True and r["in_grace"] is True


@check("payment failed with time still on the clock keeps access until expiry")
def _():
    assert e.resolve([sub(status=e.PAYMENT_FAILED)], NOW)["premium"] is True


@check("payment failed past expiry denies")
def _():
    assert e.resolve([sub(status=e.PAYMENT_FAILED, expires=earlier(days=1))], NOW)["premium"] is False


# ── Denying ──────────────────────────────────────────────────────────────────

@check("no subscriptions at all denies")
def _():
    r = e.resolve([], NOW)
    assert r["premium"] is False and r["status"] == "NONE"


@check("an expired subscription denies")
def _():
    assert e.resolve([sub(status=e.EXPIRED, expires=earlier(days=1))], NOW)["premium"] is False


@check("an active status past its expiry denies — the status is not the clock")
def _():
    # A stale ACTIVE row is the normal state between a lapse and the webhook
    # that records it. The expiry is the truth, not the label.
    assert e.resolve([sub(status=e.ACTIVE, expires=earlier(hours=1))], NOW)["premium"] is False


@check("an unknown status denies — fail closed")
def _():
    assert e.resolve([sub(status="SOMETHING_NEW")], NOW)["premium"] is False


@check("a missing expiry denies")
def _():
    assert e.resolve([sub(expires=None if False else "")], NOW)["premium"] is False


@check("an unreadable expiry denies rather than being read as forever")
def _():
    assert e.resolve([sub(expires="whenever")], NOW)["premium"] is False


@check("garbage in the list is ignored, not crashed on")
def _():
    assert e.resolve(["nonsense", None, 42, sub()], NOW)["premium"] is True
    assert e.resolve(["nonsense", None], NOW)["premium"] is False


@check("None instead of a list denies")
def _():
    assert e.resolve(None, NOW)["premium"] is False


# ── The exploits ─────────────────────────────────────────────────────────────

@check("a refund on iOS kills premium even with an ACTIVE web subscription")
def _():
    # Otherwise "buy on iOS, refund on iOS, keep the web access" is a working
    # exploit, and it is the first one anyone finds.
    r = e.resolve([sub(platform=e.APPLE, status=e.REFUNDED),
                   sub(platform=e.WEB, status=e.ACTIVE)], NOW)
    assert r["premium"] is False
    assert "REFUNDED" in r["reason"]


@check("a revoked entitlement beats every active one, whatever the order")
def _():
    for order in ([sub(status=e.REVOKED), sub()], [sub(), sub(status=e.REVOKED)]):
        assert e.resolve(order, NOW)["premium"] is False


@check("a client-supplied premium flag cannot grant access")
def _():
    # The record shape has no 'premium' key by design. If one is injected it
    # must be inert — the server decides, the client reports.
    hostile = {"platform": e.WEB, "status": e.EXPIRED,
               "expires_at": earlier(days=5), "premium": True, "entitled": True}
    assert e.resolve([hostile], NOW)["premium"] is False


@check("an unrecognised platform still obeys the status rules")
def _():
    # A typo'd platform must not become a back door in either direction.
    assert e.resolve([{"platform": "paypal?", "status": e.ACTIVE,
                       "expires_at": later(days=5)}], NOW)["premium"] is True
    assert e.resolve([{"platform": "paypal?", "status": e.REVOKED}], NOW)["premium"] is False


# ── Multiple platforms ───────────────────────────────────────────────────────

@check("holding web and iOS resolves to ONE entitlement, furthest expiry")
def _():
    r = e.resolve([sub(platform=e.WEB, expires=later(days=5)),
                   sub(platform=e.APPLE, expires=later(days=40))], NOW)
    assert r["premium"] is True and r["source"] == e.APPLE
    assert r["subscription_count"] == 2


@check("an expired old subscription never drags down a current one")
def _():
    r = e.resolve([sub(platform=e.WEB, status=e.EXPIRED, expires=earlier(days=90)),
                   sub(platform=e.GOOGLE, expires=later(days=10))], NOW)
    assert r["premium"] is True and r["source"] == e.GOOGLE


@check("a comp entitlement works — App Review needs one")
def _():
    # Apple reviewers cannot buy a real subscription. Without this the app is
    # rejected for being unreviewable, which is a common first rejection.
    assert e.resolve([sub(platform=e.COMP, expires=later(days=365))], NOW)["premium"] is True


@check("grace only wins when nothing else grants")
def _():
    r = e.resolve([sub(status=e.GRACE_PERIOD, platform=e.APPLE, expires=earlier(days=1)),
                   sub(status=e.ACTIVE, platform=e.WEB, expires=later(days=20))], NOW)
    assert r["source"] == e.WEB and r["in_grace"] is False


# ── Shape and clock ──────────────────────────────────────────────────────────

@check("naive timestamps are read as UTC, not local time")
def _():
    naive = (NOW + timedelta(days=1)).replace(tzinfo=None).isoformat()
    assert e.resolve([sub(expires=naive)], NOW)["premium"] is True


@check("the response shape is the same whether granted or denied")
def _():
    keys = {"premium", "status", "source", "expires_at", "in_grace",
            "will_renew", "checked_at", "reason", "subscription_count"}
    assert set(e.resolve([sub()], NOW)) == keys
    assert set(e.resolve([], NOW)) == keys


@check("resolve never mutates the records it was handed")
def _():
    recs = [sub(), sub(platform=e.WEB, status=e.CANCELLED)]
    before = [dict(r) for r in recs]
    e.resolve(recs, NOW)
    assert recs == before


def main() -> int:
    passed = failed = 0
    for name, fn in CHECKS:
        try:
            fn()
        except AssertionError as ex:
            print(f"  FAIL  {name}  ({ex})"); failed += 1
        except Exception as ex:
            print(f"  ERROR {name}  ({type(ex).__name__}: {ex})"); failed += 1
        else:
            print(f"  PASS  {name}"); passed += 1
    print(f"\n{passed} passed · {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
