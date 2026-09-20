"""
rp_keys_dump.py — dump EVERY Steam Rich Presence key Wardogs publishes.

The agent stores every key it sees, so this is no longer the only way to find
out what Wardogs publishes — but it is still the fastest, because it prints the
full set live rather than after a round trip through the server. Run it during
a match to find out whether loadout cost or cash balance is exposed directly:
if it is, ROI stops being a derived quantity and becomes a measured one.

Usage:
    python rp_keys_dump.py              # poll forever, print keys as they change
    python rp_keys_dump.py --once       # single snapshot
"""

import argparse
import time

from profitdog.agent.adapters import richpresence as rp_module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Single snapshot, then exit")
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()

    rp = rp_module.RichPresence()
    if not rp.init():
        print("Steam init failed — is Steam running?")
        return

    print("Polling. Start a match; keys appear as Wardogs sets them. Ctrl+C to stop.\n")
    seen_keys = set()
    last = {}
    try:
        while True:
            data = rp.read()

            new = set(data) - seen_keys
            if new:
                seen_keys |= new
                print(f"  ++ NEW KEY(S): {', '.join(sorted(new))}")

            # Only reprint when something actually changed, so a long match
            # doesn't bury the interesting transitions in repeated output.
            if data != last:
                stamp = time.strftime("%H:%M:%S")
                print(f"[{stamp}] " + ("(empty — menu)" if not data else ""))
                for k in sorted(data):
                    print(f"    {k} = {data[k]!r}")
                last = data

            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        print(f"\nAll keys seen this run: {sorted(seen_keys)}")
        rp.shutdown()


if __name__ == "__main__":
    main()
