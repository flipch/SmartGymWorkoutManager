"""One-time WHOOP consent, and a read check.

  python -m harness.whoop_auth            # opens the consent page; click Allow once; tokens land in a mode-0600 file
  python -m harness.whoop_auth --check    # reads the newest recovery + sleep and prints them (no tokens)

Exit codes: 0 ok, 1 usage/config error, 2 WHOOP unavailable or rejected us.
"""
import argparse
import sys
import webbrowser
from datetime import datetime, timezone

from . import whoop_client as wc
from .errors import SourceDown
from .whoop import TokenStore


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="harness.whoop_auth", description=__doc__.splitlines()[0])
    p.add_argument("--creds", default=str(wc.DEFAULT_CREDS), help="app id/secret file (mode 600)")
    p.add_argument("--tokens", default=str(wc.DEFAULT_TOKENS), help="where the tokens are stored (mode 600)")
    p.add_argument("--port", type=int, default=8000, help="loopback port; must match the registered redirect URI")
    p.add_argument("--timeout", type=float, default=180, help="seconds to wait for the consent click")
    p.add_argument("--check", action="store_true", help="read recovery + sleep instead of authorising")
    a = p.parse_args(argv)
    try:
        creds = wc.load_app_credentials(a.creds)
    except (OSError, ValueError) as err:
        print(f"error: {type(err).__name__}: {err}", file=sys.stderr)
        return 1
    if not 1 <= a.port <= 65535:
        print("error: --port must be 1-65535", file=sys.stderr)
        return 1
    store = TokenStore(a.tokens)
    try:
        if a.check:
            readings, notes = wc.WhoopClient(creds, store).fetch_readings()
            now = datetime.now(timezone.utc)
            for r in readings:
                print(f"{r.kind}: {r.value:g} (measured {r.as_of.isoformat()}, {(now - r.as_of).total_seconds() / 3600:.1f} h ago)")
            for n in notes:
                print(f"note: {n}")
            return 0
        print(f"Opening WHOOP consent. If the browser does not open, visit the URL printed below.\n"
              f"Redirect must be {wc.redirect_uri_for(a.port)} (registered in the WHOOP app); scopes: {', '.join(wc.SCOPES)}.")

        def open_url(url):
            print(url)
            webbrowser.open(url)

        summary = wc.run_consent(creds, store, open_url=open_url, port=a.port, timeout_s=a.timeout)
        print(f"authorised: scopes {summary['scope']}; token expires in {summary['expires_in']} s (refresh is automatic)")
        return 0
    except OSError as err:  # e.g. the port is busy
        print(f"error: {type(err).__name__}: {err}", file=sys.stderr)
        return 1
    except SourceDown as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
