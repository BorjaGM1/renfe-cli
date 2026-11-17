"""Refresh the station list shipped with the CLI from Renfe's live API.

    uv run python scripts/update_stations.py

Writes src/renfe/data/stations.json and prints what changed, in Markdown for the PR description. Refuses
an empty list, or one much shorter than the current one (Renfe answering something other than its list),
unless --force.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from renfe import config, stations
from renfe.client import RsvClient
from renfe.errors import RenfeError

BUNDLE = Path(__file__).resolve().parents[1] / "src" / "renfe" / stations.BUNDLE
MIN_SHARE = 0.9  # a new list under 90% of the current one's length is refused without --force


def changes(old: list[stations.Station], new: list[stations.Station]) -> list[str]:
    before, after = {s.code: s for s in old}, {s.code: s for s in new}
    lines = [f"- Added: {s.name} ({s.code})" for code, s in sorted(after.items()) if code not in before]
    lines += [f"- Removed: {s.name} ({s.code})" for code, s in sorted(before.items()) if code not in after]
    lines += [f"- Renamed: {before[code].name} → {s.name} ({code})"
              for code, s in sorted(after.items()) if code in before and before[code].name != s.name]
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--force", action="store_true", help="write the list even if it is much shorter")
    args = parser.parse_args()
    try:
        old = stations.parse(json.loads(BUNDLE.read_text(encoding="utf-8"))["stations"])
    except FileNotFoundError:
        old = []
    try:
        with RsvClient() as client:
            raw = client.get(config.STATIONS_PATH, label="stations")
        new = stations.parse(raw)
    except RenfeError as err:
        print(f"Couldn't get Renfe's station list: {err.message}", file=sys.stderr)
        return 1
    if old and len(new) < MIN_SHARE * len(old) and not args.force:
        print(f"Renfe returned {len(new)} stations against {len(old)} shipped; not writing it "
              "(run again later, or pass --force if the list really shrank).", file=sys.stderr)
        return 1
    BUNDLE.parent.mkdir(parents=True, exist_ok=True)
    BUNDLE.write_text(stations.bundle_text(raw, date.today()), encoding="utf-8")
    lines = changes(old, new)
    print(f"{len(new)} stations ({len(new) - len(old):+d}).")
    print("\n".join(lines) if lines else "No station added, removed or renamed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
