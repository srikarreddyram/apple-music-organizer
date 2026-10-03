#!/usr/bin/env python3
"""Apple Music Library Organizer: local-first, read-only unless you approve a change plan.

Usage:
  python3 organizer.py scan                  Snapshot the library into data/inventory.json
  python3 organizer.py report                Summarize playlists, duplicates and overlap
  python3 organizer.py suggest [NAME...]     Write suggested change plans (artist-gaps, genre-homes)
  python3 organizer.py draft ...             Write a hand-made change plan
  python3 organizer.py plans                 List change plans and their status
  python3 organizer.py review PLAN           Show every op and track in a plan
  python3 organizer.py approve PLAN 1,3-5    Approve ops (or "all"); `reject` undoes approval
  python3 organizer.py drop PLAN OP 2,7      Remove tracks from an op before approving it
  python3 organizer.py apply PLAN            Apply approved ops to Music, verify, log, write undo plan

Nothing changes in Music except through `apply`, which only runs approved ops
and asks for its own confirmation.
"""
import argparse
import itertools
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import music_bridge
import plans
import suggest

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


def editable(p):
    return not p["smart"] and p["specialKind"] == "none"


def cmd_report(args):
    lib = load_inventory()
    tracks = {t["persistentID"]: t for t in lib["tracks"]}
    playlists = [p for p in lib["playlists"] if editable(p)]

    in_lib = sum(t.get("inLibrary", True) for t in tracks.values())
    print(f"Scanned {lib['scannedAt']}: {in_lib} library tracks, {len(tracks) - in_lib} more only in "
          f"playlists, {len(playlists)} editable playlists\n")

    # Playlists
    print("PLAYLISTS")
    for p in sorted(playlists, key=lambda p: -len(p["trackIDs"])):
        dupes = len(p["trackIDs"]) - len(set(p["trackIDs"]))
        extra = f"  ({dupes} repeated entries)" if dupes else ""
        print(f"  {len(p['trackIDs']):4}  {p['name'].strip()}{extra}")

    # Tracks in no playlist
    in_any = set(itertools.chain.from_iterable(p["trackIDs"] for p in playlists))
    orphans = [t for pid, t in tracks.items() if pid not in in_any and t.get("inLibrary", True)]
    print(f"\nLIBRARY TRACKS IN NO PLAYLIST: {len(orphans)}")

    # Duplicate candidates: same normalized title + primary artist, different IDs
    groups = defaultdict(list)
    for t in tracks.values():
        groups[suggest.song_key(t)].append(t)
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


def cmd_suggest(args):
    lib = load_inventory()
    names = args.names or list(suggest.GENERATORS)
    for name in names:
        if name not in suggest.GENERATORS:
            sys.exit(f"unknown suggestion {name!r}; choose from {', '.join(suggest.GENERATORS)}")
        ops = suggest.GENERATORS[name](lib)
        if not ops:
            print(f"{name}: nothing to suggest")
            continue
        plan = plans.new_plan(f"Suggested: {name}", ops, lib)
        plans.save_plan(plan)
        n = sum(len(o.get("tracks") or []) for o in ops)
        print(f"{name}: {len(ops)} ops, {n} tracks -> {plan['id']}")
    print("\nReview with: python3 organizer.py review PLAN")


def find_playlist(lib, ref):
    hits = [p for p in lib["playlists"] if p["persistentID"] == ref or p["name"].strip() == ref.strip()]
    if len(hits) != 1:
        sys.exit(f"{len(hits)} playlists match {ref!r}")
    return hits[0]


def cmd_draft(args):
    lib = load_inventory()
    tracks = {t["persistentID"]: t for t in lib["tracks"]}
    missing = [i for i in args.add + args.remove if i not in tracks]
    if missing:
        sys.exit(f"unknown track IDs: {', '.join(missing)}")
    if bool(args.new_playlist) == bool(args.playlist):
        sys.exit("give exactly one of --new-playlist NAME or --playlist NAME_OR_ID")
    ops = []
    if args.new_playlist:
        if args.remove:
            sys.exit("--remove makes no sense for a new playlist")
        ops.append({"op": "create_playlist", "name": args.new_playlist, "ref": "new", "reason": args.reason})
        target = {"ref": "new", "name": args.new_playlist}
    else:
        p = find_playlist(lib, args.playlist)
        if not editable(p):
            sys.exit(f"{p['name']!r} is a smart or special playlist and can't be edited")
        target = {"id": p["persistentID"], "name": p["name"]}
    if args.add:
        ops.append({"op": "add_tracks", "playlist": target, "reason": args.reason,
                    "tracks": [plans.track_ref(tracks[i]) for i in args.add]})
    if args.remove:
        ops.append({"op": "remove_tracks", "playlist": target, "reason": args.reason,
                    "tracks": [plans.track_ref(tracks[i]) for i in args.remove]})
    if not ops:
        sys.exit("nothing to do")
    plan = plans.new_plan(args.title or "Draft", ops, lib)
    plans.save_plan(plan)
    plans.print_plan(plan)


def cmd_plans(args):
    for plan in plans.all_plans():
        st = defaultdict(int)
        for op in plan["ops"]:
            st[op["status"] if op["status"] != "pending" else ("approved" if op["approved"] else "pending")] += 1
        summary = ", ".join(f"{n} {k}" for k, n in sorted(st.items()))
        print(f"{plan['id']}\n    {plan['title']}: {summary}")


def cmd_review(args):
    plans.print_plan(plans.load_plan(args.plan), verbose=not args.brief)


def cmd_approve(args, value=True):
    plan = plans.load_plan(args.plan)
    plans.set_approval(plan, args.ops, value)
    plans.print_plan(plan, verbose=False)


def cmd_drop(args):
    plan = plans.load_plan(args.plan)
    plans.drop_tracks(plan, args.op, args.tracks)
    plans.print_plan(plan)


def cmd_apply(args):
    plan = plans.load_plan(args.plan)
    todo = [op for op in plan["ops"] if op["approved"] and op["status"] == "pending"]
    if not todo:
        sys.exit("No approved pending ops in this plan. Approve some with: organizer.py approve PLAN OPS")
    print("About to change your Music library:")
    for op in todo:
        count = f" ({len(op['tracks'])} tracks)" if op.get("tracks") else ""
        print(f"  {op['n']}. {op['op']} {plans.target_label(op)}{count}")
    if args.confirm != plan["id"]:
        if not sys.stdin.isatty():
            sys.exit(f"\nNot applied. Confirm interactively, or pass --confirm {plan['id']}")
        if input(f"\nType the plan id to apply ({plan['id']}): ").strip() != plan["id"]:
            sys.exit("Not applied.")
    done, undo = plans.apply_plan(plan)
    ok = sum(op["status"] == "applied" for op in done)
    print(f"\n{ok} of {len(done)} ops applied and verified. Audit log: {plans.AUDIT}")
    if undo:
        print(f"Undo plan (needs approval like any other): {undo['id']}")
    print("Refreshing inventory...")
    cmd_scan(args)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan").set_defaults(fn=cmd_scan)
    r = sub.add_parser("report")
    r.add_argument("--limit", type=int, default=25)
    r.set_defaults(fn=cmd_report)

    sg = sub.add_parser("suggest")
    sg.add_argument("names", nargs="*", help=", ".join(suggest.GENERATORS))
    sg.set_defaults(fn=cmd_suggest)

    d = sub.add_parser("draft", help="hand-made plan")
    d.add_argument("--new-playlist", metavar="NAME")
    d.add_argument("--playlist", metavar="NAME_OR_ID")
    d.add_argument("--add", nargs="+", default=[], metavar="TRACK_ID")
    d.add_argument("--remove", nargs="+", default=[], metavar="TRACK_ID")
    d.add_argument("--title")
    d.add_argument("--reason", default="requested by hand")
    d.set_defaults(fn=cmd_draft)

    sub.add_parser("plans").set_defaults(fn=cmd_plans)

    rv = sub.add_parser("review")
    rv.add_argument("plan")
    rv.add_argument("--brief", action="store_true", help="hide track lists")
    rv.set_defaults(fn=cmd_review)

    for name, value in (("approve", True), ("reject", False)):
        a = sub.add_parser(name)
        a.add_argument("plan")
        a.add_argument("ops", help='op numbers like "1,3-5" or "all"')
        a.set_defaults(fn=lambda args, v=value: cmd_approve(args, v))

    dr = sub.add_parser("drop")
    dr.add_argument("plan")
    dr.add_argument("op", type=int)
    dr.add_argument("tracks", help='track numbers within the op, like "2,7-9"')
    dr.set_defaults(fn=cmd_drop)

    ap_ = sub.add_parser("apply")
    ap_.add_argument("plan")
    ap_.add_argument("--confirm", metavar="PLAN_ID", help="non-interactive confirmation; must equal the plan id")
    ap_.set_defaults(fn=cmd_apply)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
