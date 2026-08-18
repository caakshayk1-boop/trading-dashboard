#!/usr/bin/env python3
"""
edge/entitlements.py — who is allowed to see paid content, and why.

The one rule this module exists to enforce:

    The app asks the SERVER what a user is entitled to.
    It never decides locally, and it never trusts the client.

Every payment route — Stripe on the web, StoreKit on iOS, Play Billing on
Android — ends here. The rest of the product asks one question,
`resolve(...)`, and gets one answer. Nothing downstream knows or cares which
store the money came through, which is what stops "is this user paid?" from
being reimplemented three times with three different bugs.

WHY A PURE FUNCTION

`resolve()` takes server-side subscription records and a clock, and returns an
entitlement. No database, no network, no store SDK. That makes every rule below
testable offline, before a single cent of real money or a single store account
exists — and it means the rules can be reviewed as rules rather than inferred
from three webhook handlers.

FAIL CLOSED, ALWAYS

An unknown status, a malformed record, a missing expiry, an empty list: all
deny. A bug in this file must lock a paying customer out (recoverable, and they
will tell you within minutes) rather than let a non-paying one in (silent, and
it is how a subscription product quietly stops being one).

WHAT THIS MODULE DELIBERATELY DOES NOT DO

It does not verify receipts. Apple and Google receipt validation is a network
call against a store, and mixing it in here would make the rules untestable and
put a store outage inside an access-control decision. The validator writes
records; this reads them.
"""
from __future__ import annotations

from datetime import datetime, timezone

# ── Subscription lifecycle ───────────────────────────────────────────────────
# One vocabulary across all three stores. Apple, Google and Stripe each use
# different words for the same five or six things; translating at the webhook
# boundary means the access rules are written once instead of per store.
TRIAL = "TRIAL"                    # introductory period, full access
ACTIVE = "ACTIVE"                  # paid and current
GRACE_PERIOD = "GRACE_PERIOD"      # renewal failed, store is retrying, keep access
PAYMENT_FAILED = "PAYMENT_FAILED"  # retries exhausted, access ends at expiry
CANCELLED = "CANCELLED"            # will not renew — but the paid period is theirs
EXPIRED = "EXPIRED"                # period ended
REFUNDED = "REFUNDED"              # money returned
REVOKED = "REVOKED"                # access withdrawn (chargeback, fraud, abuse)

# Statuses that can grant access AT ALL. Everything not named here denies, so a
# new store status added tomorrow denies until someone deliberately maps it —
# the safe direction for an unknown.
GRANTING = frozenset({TRIAL, ACTIVE, GRACE_PERIOD, PAYMENT_FAILED, CANCELLED})

# Statuses that KILL access no matter what else the user holds. A refund or a
# chargeback on any platform ends premium immediately, even if an unrelated
# ACTIVE row exists — otherwise "refund on iOS, keep the free ride on web" is a
# working exploit.
POISON = frozenset({REFUNDED, REVOKED})

# GRACE_PERIOD keeps access WITHOUT a valid expiry because that is the whole
# point: the store is retrying a card and the period has already lapsed. Every
# other granting status must still be inside its paid period.
IGNORES_EXPIRY = frozenset({GRACE_PERIOD})

WEB, APPLE, GOOGLE, COMP = "stripe", "apple", "google", "comp"
PLATFORMS = frozenset({WEB, APPLE, GOOGLE, COMP})


def _utc(ts) -> datetime | None:
    """UTC datetime, or None when unreadable. Unreadable never grants access."""
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def resolve(subscriptions, now: datetime | None = None) -> dict:
    """The user's entitlement, from every subscription record they hold.

    `subscriptions` is a list of server-side records, each at minimum:

        {"platform": "apple", "status": "ACTIVE",
         "expires_at": "2026-09-18T00:00:00Z"}

    A user can legitimately hold several — they subscribed on the web, then
    reinstalled and bought again through the App Store, or a comp entitlement
    was granted for App Review. The most generous GRANTING record wins, except
    that any POISON record vetoes everything.

    Returns a dict the API can serialise straight to `GET /v1/me/entitlements`.
    """
    now = now or datetime.now(timezone.utc)
    records = [r for r in (subscriptions or []) if isinstance(r, dict)]

    # A refund or chargeback anywhere ends premium everywhere, first and
    # unconditionally. Checked BEFORE the granting pass so no ordering or
    # expiry subtlety can let a poisoned account keep access.
    for r in records:
        if str(r.get("status", "")).upper() in POISON:
            return _deny(now, reason=f"{str(r.get('status')).upper()} on {r.get('platform')}",
                         records=records)

    best = None
    for r in records:
        status = str(r.get("status", "")).upper()
        if status not in GRANTING:
            continue
        expires = _utc(r.get("expires_at"))
        if status in IGNORES_EXPIRY:
            pass          # the store is retrying; a lapsed period is expected
        elif expires is None or expires <= now:
            continue      # no readable expiry, or the paid period is over
        # "Most generous" = furthest expiry. A grace-period row has no usable
        # expiry, so it only wins when nothing else grants.
        rank = (expires or now).timestamp() if status not in IGNORES_EXPIRY else now.timestamp()
        if best is None or rank > best[0]:
            best = (rank, r, status, expires)

    if best is None:
        return _deny(now, reason="no active subscription", records=records)

    _rank, rec, status, expires = best
    return {
        "premium": True,
        "status": status,
        "source": rec.get("platform"),
        "expires_at": expires.isoformat() if expires else None,
        "in_grace": status in IGNORES_EXPIRY,
        "will_renew": status in (TRIAL, ACTIVE, GRACE_PERIOD),
        "checked_at": now.isoformat(),
        "reason": None,
        "subscription_count": len(records),
    }


def _deny(now: datetime, *, reason: str, records) -> dict:
    return {
        "premium": False,
        "status": EXPIRED if records else "NONE",
        "source": None,
        "expires_at": None,
        "in_grace": False,
        "will_renew": False,
        "checked_at": now.isoformat(),
        "reason": reason,
        "subscription_count": len(records),
    }
