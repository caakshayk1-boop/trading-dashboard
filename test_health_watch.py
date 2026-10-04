"""Offline checks for health_watch.current_editions. No network, no pytest."""
from datetime import datetime, timedelta, timezone

import health_watch as H

UTC = timezone.utc
ok = fail = 0


def check(name, cond):
    global ok, fail
    print(("  PASS  " if cond else "  FAIL  ") + name)
    ok, fail = ok + bool(cond), fail + (not cond)


at = lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC)  # noqa: E731

# The two runs that failed: a late 15:00 UTC slot, after midnight in Malaysia.
check("03:37 MYT on 3 Oct accepts the 2 Oct edition (run 37055286638)",
      "2026-10-02" in H.current_editions(at("2026-10-02T19:37:00")))
check("02:23 MYT on 4 Oct accepts the 3 Oct edition (run 37143993639)",
      "2026-10-03" in H.current_editions(at("2026-10-03T18:23:00")))
# Once the day's edition is due, yesterday's is stale again.
check("08:00 MYT rejects yesterday's edition",
      H.current_editions(at("2026-10-03T00:00:00")) == {"2026-10-03"})
check("07:29 MYT still accepts yesterday's, 07:30 does not",
      "2026-10-02" in H.current_editions(at("2026-10-02T23:29:00"))
      and "2026-10-02" not in H.current_editions(at("2026-10-02T23:30:00")))
# Two days old is never current, at any hour.
check("an edition two days old is stale at every hour",
      all("2026-10-01" not in H.current_editions(at("2026-10-02T16:00:00") + timedelta(hours=h)) for h in range(24)))
# Today's MYT edition is always current, including just after midnight MYT
# (the 12 Sep case: an edition dated the new MYT day before IST rolled over).
check("today's MYT edition is current at every hour",
      all((at("2026-10-02T16:00:00") + timedelta(hours=h)).astimezone(H.MYT).date().isoformat()
          in H.current_editions(at("2026-10-02T16:00:00") + timedelta(hours=h)) for h in range(24)))

print(f"\n{ok} passed · {fail} failed")
raise SystemExit(1 if fail else 0)
