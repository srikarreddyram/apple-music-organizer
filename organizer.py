#!/usr/bin/env python3
"""Apple Music Library Organizer: local-first, read-only unless you approve a change plan.

Usage:
  python3 organizer.py scan      Snapshot the library into data/inventory.json
  python3 organizer.py report    Summarize playlists, duplicates and overlap
"""
import argparse
import itertools
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import music_bridge

DATA = Path(__file__).parent / "data"
INVENTORY = DATA / "inventory.json"


def cmd_scan(args):
    DATA.mkdir(exist_ok=True)
    print("Reading library from Music (read-only)...")
    lib = music_bridge.read_library()
    lib["scannedAt"] = datetime.now().isoformat(timespec="seconds")
    text = json.dumps(lib, indent=1, ensure_ascii=False)
    INVENTORY.write_text(text)
    snap = DATA / "snapshots" / f"inventory-{lib['scannedAt'].replace(':', '')}.json"
    snap.parent.mkdir(exist_ok=True)
    snap.write_text(text)
    print(f"{len(lib['tracks'])} tracks, {len(lib['playlists'])} playlists -> {INVENTORY}")


def load_inventory():
    if not INVENTORY.exists():
        sys.exit("No inventory yet. Run: python3 organizer.py scan")
    return json.loads(INVENTORY.read_text())


def normalize(s):
    s = (s or "").lower()
    s = re.sub(r"\s*[\(\[](feat|ft|with|from|remaster|deluxe)[^\)\]]*[\)\]]", "", s)
    s = re.sub(r"[^\w\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def editable(p):
    return not p["smart"] and p["specialKind"] == "none"


def cmd_report(args):
    lib = load_inventory()
    tracks = {t["persistentID"]: t for t in lib["tracks"]}
    playlists = [p for p in lib["playlists"] if editable(p)]

    print(f"Scanned {lib['scannedAt']}: {len(tracks)} tracks, "
          f"{len(playlists)} editable playlists\n")

    # Playlists
    print("PLAYLISTS")
    for p in sorted(playlists, key=lambda p: -len(p["trackIDs"])):
        dupes = len(p["trackIDs"]) - len(set(p["trackIDs"]))
        extra = f"  ({dupes} repeated entries)" if dupes else ""
        print(f"  {len(p['trackIDs']):4}  {p['name'].strip()}{extra}")

    # Tracks in no playlist
    in_any = set(itertools.chain.from_iterable(p["trackIDs"] for p in playlists))
    orphans = [t for pid, t in tracks.items() if pid not in in_any]
    print(f"\nTRACKS IN NO PLAYLIST: {len(orphans)}")

    # Duplicate candidates: same normalized title + primary artist, different IDs
    groups = defaultdict(list)
    for t in tracks.values():
        artist = normalize(t["artist"]).split(" and ")[0].split(" x ")[0]
        groups[(normalize(t["name"]), artist)].append(t)
    dupes = [g for g in groups.values() if len(g) > 1]
    print(f"\nDUPLICATE CANDIDATES: {len(dupes)}")
    for g in dupes[: args.limit]:
        print(f"  {g[0]['name']} - {g[0]['artist']}")
        for t in g:
            print(f"      [{t['persistentID']}] album: {t['album']}")

    # Overlap between playlists
    sets = {p["name"].strip(): set(p["trackIDs"]) for p in playlists}
    pairs = []
    for a, b in itertools.combinations(sets, 2):
        shared = len(sets[a] & sets[b])
        if shared:
            smaller = min(len(sets[a]), len(sets[b]))
            pairs.append((shared / smaller, shared, a, b))
    pairs.sort(reverse=True)
    print("\nPLAYLIST OVERLAP (shared tracks / size of smaller playlist)")
    for frac, shared, a, b in pairs[: args.limit]:
        print(f"  {frac:5.0%}  {shared:3} shared  {a}  <->  {b}")

    # Genres
    genres = defaultdict(int)
    for t in tracks.values():
        genres[t["genre"] or "(none)"] += 1
    print("\nGENRES")
    for g, n in sorted(genres.items(), key=lambda x: -x[1])[: args.limit]:
        print(f"  {n:4}  {g}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan").set_defaults(fn=cmd_scan)
    r = sub.add_parser("report")
    r.add_argument("--limit", type=int, default=25)
    r.set_defaults(fn=cmd_report)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
