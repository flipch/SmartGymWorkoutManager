"""Record real Speediance responses for fixture-shaping.  python -m harness.record --days 180 --detail 3

Writes raw JSON under a PRIVATE directory outside the repo (default ~/.config/speediance-spike/recordings,
mode 0700/0600). Never commit these: they contain personal data. Tests use synthetic fixtures.
Budget: one login (if no cached token) + one history read + at most --detail detail reads, paced.
Stops at the first error.
"""
import argparse
import json
import os
import pathlib
import sys
from datetime import timedelta

from . import localsecrets
from .errors import SourceDown
from .localday import today
from .speediance import DETAIL_ENDPOINT, SpeedianceNano


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--detail", type=int, default=3, help="max per-session detail reads (default 3)")
    parser.add_argument("--out", default=str(localsecrets.CONFIG_DIR / "recordings"))
    args = parser.parse_args(argv)
    if not 0 <= args.detail <= 5:
        parser.error("--detail must be 0..5 (keep the live-API budget small)")

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    adapter = SpeedianceNano()
    end = today()
    try:
        records = adapter.history(end - timedelta(days=args.days), end)
        localsecrets.write_json_private(out / "history.json", {"records": records})
        wanted = sorted((r for r in records if r.get("type") in DETAIL_ENDPOINT),
                        key=lambda r: r.get("startTimestamp") or 0, reverse=True)[: args.detail]
        for rec in wanted:
            body = adapter.detail(rec["trainingId"], rec["type"])
            localsecrets.write_json_private(out / f"detail-{rec['trainingId']}.json", {"record": rec, "detail": body})
    except (SourceDown, PermissionError, FileNotFoundError) as err:
        print(f"STOP: {err}", file=sys.stderr)
        return 1
    print(json.dumps({"records": len(records), "details_saved": len(wanted), "logins": adapter.logins,
                      "out": str(out)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
