#!/usr/bin/env python3
"""Apple Music Library Organizer: local-first, read-only unless you approve a change plan.

Usage:
  python3 organizer.py scan                  Snapshot the library into data/inventory.json
  python3 organizer.py report                Summarize playlists, duplicates and overlap
  python3 organizer.py artist "NAME"         Plan a playlist of that artist's best songs for your taste
  python3 organizer.py fill                  Add songs you've since added to the library to artist playlists
  python3 organizer.py split PLAYLIST...     Plan splitting big mixed playlists into vibe playlists
  python3 organizer.py check-labels          Compare energy labels with the measured audio
  python3 organizer.py evaluate              Benchmark playlist fit against your own playlists
  python3 organizer.py label TRACK ...       Override a track's mood/energy/context/language/style
  python3 organizer.py suggest [NAME...]     Write suggested change plans (artist-gaps, genre-homes)
  python3 organizer.py draft ...             Write a hand-made change plan
  python3 organizer.py plans                 List change plans and their status
  python3 organizer.py review PLAN           Show every op and track in a plan
  python3 organizer.py approve PLAN 1,3-5    Approve ops (or "all"); `reject` undoes approval
  python3 organizer.py names PLAN [--reroll N]  Name options for new playlists; rename PLAN OP "Name"
  python3 organizer.py drop PLAN OP 2,7      Remove tracks from an op before approving it
  python3 organizer.py apply PLAN            Apply approved ops to Music, verify, log, write undo plan
  python3 organizer.py enrich [SOURCE...]    Add metadata: apple, audio, musicbrainz (default), lastfm (needs key)
  python3 organizer.py install-app           Build and start the menu bar app (♫ icon)
  python3 organizer.py install-scripts       Add the organizer's actions to Music's Scripts menu
  python3 organizer.py discover [--fresh]    Charting songs (or new releases) from lesser-known artists near your taste

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

import artist
import discover
import labels
import metadata
import music_bridge
import plans
import reorg
import suggest

DATA = Path(__file__).parent / "data"
INVENTORY = DATA / "inventory.json"


def scan():
    """Read the library from Music, save it as the inventory plus a snapshot, and return it."""
    DATA.mkdir(exist_ok=True)
    lib = music_bridge.read_library()
    lib["scannedAt"] = datetime.now().isoformat(timespec="seconds")
    text = json.dumps(lib, indent=1, ensure_ascii=False)
    INVENTORY.write_text(text)
    snap = DATA / "snapshots" / f"inventory-{lib['scannedAt'].replace(':', '')}.json"
    snap.parent.mkdir(exist_ok=True)
    snap.write_text(text)
    return lib


def cmd_scan(args):
    print("Reading library from Music (read-only)...")
    lib = scan()
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


def cmd_artist(args):
    lib = load_inventory()
    result = artist.best_songs(lib, args.name, country=args.country, size=args.size,
                               progress=lambda m: print(f"  {m}", file=sys.stderr))
    if not result["soundUsed"]:
        print("(sound fit skipped: run `enrich audio` first so your own songs are measured)")
    print(f"\n{result['artist']}: best {len(result['picks'])} for you\n")
    for i, p in enumerate(result["picks"], 1):
        mark = "in library" if p["libraryID"] else "add in Music"
        print(f"{i:>3}. {p['name']}  [{mark}]  ({artist.why(p)})")
        if not p["libraryID"]:
            print(f"       {p['url']}")
    ops, waiting = artist.plan_ops(lib, result, args.playlist_name)
    plan = plans.new_plan(f"Artist playlist: {result['artist']}", ops, lib)
    plan["waiting"] = waiting
    plans.save_plan(plan)
    print(f"\nPlan {plan['id']}: creates the playlist with the {len(result['picks']) - len(waiting)} songs you have.")
    if waiting:
        print(f"Add the other {len(waiting)} in Music (+), then run `scan` and `fill` to put them in the playlist.")


def cmd_fill(args):
    lib = load_inventory()
    made = 0
    for plan in plans.all_plans():
        ops, still = artist.fill_ops(lib, plan)
        if ops:
            fill = plans.new_plan(f"Fill {plan['title']}", ops, lib)
            plans.save_plan(fill)
            plan["waiting"] = still
            plans.save_plan(plan)
            made += 1
            print(f"{fill['id']}: {len(ops[0]['tracks'])} songs ready to add ({len(still)} still not in library)")
    if not made:
        print("Nothing new to fill. (Add songs in Music, then run `scan` first.)")


def cmd_split(args):
    lib = load_inventory()
    ops, left_out, used = [], {}, {p["name"].strip() for p in lib["playlists"]}
    for ref in args.playlists:
        p = find_playlist(lib, ref)
        more, left = reorg.split_ops(lib, p, min_size=args.min_size, used=used)
        ops += more
        if left:
            left_out[p["name"].strip()] = left
    sources = ", ".join(find_playlist(lib, r)["name"].strip() for r in args.playlists)
    plan = plans.new_plan(f"Split {sources}", ops, lib)
    plan["leftOut"] = left_out
    plans.save_plan(plan)
    plans.print_plan(plan, verbose=args.verbose)
    for source, left in left_out.items():
        print(f"\nStay only in {source!r} (too few of their kind to form a playlist):")
        for t in left:
            print(f"    {t['name']} - {t['artist']}")
    print(f"\nSaved {plan['id']}. See every track with: python3 organizer.py review {plan['id']}")


def cmd_check_labels(args):
    import calibrate
    report, weights, flagged = calibrate.check(load_inventory(), threshold=args.threshold)
    print(f"Energy from sound alone, 5-fold cross-validated on {report['trained_on']} trusted labels:")
    print(f"  mean error {report['cv_mae']:.2f} levels (guessing the average: {report['baseline_mae']:.2f}), "
          f"{report['within_1']:.0%} within one level, correlation {report['cv_corr']:.2f}")
    print("  weights: " + ", ".join(f"{k} {v:+.2f}" for k, v in weights.items()))
    print(f"\n{len(flagged)} low-confidence labels the audio disagrees with by {args.threshold}+ levels:")
    for t, lab, pred in flagged:
        print(f"  {t['persistentID']}  {t['name'][:38]:38} {t['artist'][:24]:24} label {lab['energy']}  sounds {pred:.1f}")
    if flagged:
        print("\nFix with: python3 organizer.py label TRACK_ID --energy N")
    crowd = calibrate.crowd_check(load_inventory())
    if crowd.get("corr") is not None:
        print(f"\nLast.fm listeners: {crowd['songs_with_mood_tags']} songs have calm/energetic tags; "
              f"agreement with energy labels: correlation {crowd['corr']:.2f}")
        print("  crowd energy (-1 calm .. +1 energetic) by label: " +
              ", ".join(f"{e}: {m:+.2f} ({n})" for e, (m, n) in crowd["by_energy"].items()))
        if crowd["disagree"]:
            print(f"  {len(crowd['disagree'])} songs where listeners clearly disagree:")
            for t, lab, c in crowd["disagree"][:20]:
                print(f"    {t['persistentID']}  {t['name'][:36]:36} label {lab['energy']}  crowd {c:+.2f}")


def cmd_evaluate(args):
    import evaluate
    lib = load_inventory()
    evaluate.report(evaluate.run(lib))
    splits = [p for p in plans.all_plans() if p["title"].startswith("Split ")]
    if splits:
        plan = splits[-1]
        print(f"\nSplit cohesion, measured sound only ({plan['id']}):")
        print("  x1.0 = no more alike than a random group of the same size from the same playlist")
        for source, r in evaluate.split_cohesion(lib, plan).items():
            print(f"  {source}: x{r['weighted']:.2f}")
            for name, n, x in r["groups"]:
                print(f"      {name[:36]:36} {n:3} songs  x{x:.2f}")
        import calibrate
        crowd = calibrate.crowd_check(lib, plan)
        if crowd.get("groups"):
            print("\nWhat Last.fm listeners call each new playlist (-1 calm .. +1 energetic):")
            for name, (group, vals) in crowd["groups"].items():
                if vals:
                    print(f"      {name[:36]:36} {group:11} {sum(vals) / len(vals):+.2f}  ({len(vals)} tagged songs)")


def cmd_label(args):
    lib = load_inventory()
    tracks = {t["persistentID"]: t for t in lib["tracks"]}
    if args.track not in tracks:
        sys.exit(f"unknown track ID {args.track}")
    manual = labels.load("manual")
    current = labels.merged().get(args.track, {})
    change = {k: v for k, v in (("mood", args.mood), ("energy", args.energy), ("language", args.language),
                                ("style", args.style), ("group", args.group)) if v is not None}
    if args.contexts is not None:
        change["contexts"] = [c for c in args.contexts.split(",") if c]
    errors = labels.validate({**current, **change, "confidence": "high"}) if change else []
    if errors:
        sys.exit("invalid: " + "; ".join(errors))
    if change:
        manual[args.track] = {**manual.get(args.track, {}), **change}
        labels.save("manual", manual)
    t = tracks[args.track]
    print(f"{t['name']} - {t['artist']}: {json.dumps(labels.merged().get(args.track, {}), ensure_ascii=False)}")


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


def cmd_rename(args):
    plan = plans.load_plan(args.plan)
    plans.rename(plan, args.op, args.name)
    plans.print_plan(plan, verbose=False)


def cmd_names(args):
    import names
    plan = plans.load_plan(args.plan)
    taken = {o["name"] for o in plan["ops"] if o["op"] == "create_playlist"}
    for op in plan["ops"]:
        if op["op"] != "create_playlist" or not op.get("nameProfile") or op["status"] != "pending":
            continue
        if args.reroll:
            op["nameOptions"] = names.options(op["nameProfile"], op["bucket"], avoid=taken - {op["name"]},
                                              seed=op["ref"], reroll=args.reroll)
        print(f"op {op['n']}: {op['name']}")
        for o in op["nameOptions"]:
            print(f"    {o}")
    plans.save_plan(plan)
    print(f"\nPick one with: python3 organizer.py rename {plan['id']} OP \"Name\"")


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


def cmd_enrich(args):
    lib = load_inventory()
    progress = lambda msg: print(f"  {msg}", file=sys.stderr, flush=True)
    sources = args.sources or ["apple", "audio", "musicbrainz"]
    if "apple" in sources:
        data = metadata.enrich_apple(lib, country=args.country, progress=progress, retry_misses=args.retry_misses)
        hits = sum(1 for v in data.values() if not v.get("miss"))
        print(f"apple: {hits} of {len(data)} tracks matched in the catalog")
    if "audio" in sources:
        data = metadata.enrich_audio(lib, progress=progress, reanalyze=args.reanalyze)
        ok = sum(1 for v in data.values() if "error" not in v)
        print(f"audio: {ok} of {len(data)} previews analysed")
    if "musicbrainz" in sources:
        data = metadata.enrich_musicbrainz(lib, progress=progress)
        hits = sum(1 for v in data.values() if not v.get("miss"))
        print(f"musicbrainz: {hits} of {len(data)} artists found")
    if "lastfm" in sources:
        import lastfm
        data = lastfm.enrich(lib, progress=progress)
        own = sum(1 for v in data.values() if v.get("from") == "track")
        print(f"last.fm: {own} of {len(data)} songs have their own listener tags (others use the artist's)")


# Music's Scripts menu only runs scripts, so its entry opens the app's panel over Music.
MENU_SCRIPT_NAME = "✨ Organizer"
OLD_MENU_SCRIPTS = ["Organizer – Where Do These Belong", "Organizer – Discover From Selection",
                    "Organizer – Artist Playlist…", "Organizer – Move Song…", "Organizer – Review & Apply Changes",
                    "Organizer – Refresh Scan & Metadata"]
SCRIPT_DIRS = [Path.home() / "Library/Music/Scripts",                 # Music's own Scripts menu
               Path.home() / "Library/Scripts/Applications/Music"]    # system Script menu, when Music is in front


def cmd_api(args):
    import api
    api.main(args.command, args.args)


def cmd_ui(args):
    import ui
    ui.main(args.action, args.ids)


def cmd_install_scripts(args):
    """Put one "✨ Organizer" item in Music's Scripts menu that pops open the app's panel."""
    import subprocess
    src = 'open location "musicorganizer://show"'
    for d in SCRIPT_DIRS:
        d.mkdir(parents=True, exist_ok=True)
        for old in OLD_MENU_SCRIPTS:  # the earlier dialog-based items this tool installed
            (d / f"{old}.scpt").unlink(missing_ok=True)
        out = d / f"{MENU_SCRIPT_NAME}.scpt"
        proc = subprocess.run(["osacompile", "-o", str(out), "-e", src], capture_output=True, text=True)
        if proc.returncode:
            sys.exit(f"could not compile the menu script: {proc.stderr}")
        print(f"installed {out}")
    print("\nIn Music, open the Scripts menu (scroll icon between Window and Help) and choose ✨ Organizer.")


APP_DIR = Path.home() / "Applications" / "Music Organizer.app"


def cmd_install_app(args):
    """Build the SwiftUI menu bar app, install it in ~/Applications and start it."""
    import plistlib
    import shutil
    import subprocess
    root = Path(__file__).parent.resolve()
    build = root / "build"
    build.mkdir(exist_ok=True)
    print("Building the menu bar app (about a minute)...")
    proc = subprocess.run(["swiftc", "-parse-as-library", "-swift-version", "5", "-target", "arm64-apple-macosx14.0",
                           "-O", str(root / "app" / "MusicOrganizer.swift"), "-o", str(build / "MusicOrganizer")],
                          capture_output=True, text=True)
    if proc.returncode:
        sys.exit("Build failed:\n" + proc.stderr[-3000:])
    subprocess.run(["pkill", "-x", "MusicOrganizer"], capture_output=True)
    if APP_DIR.exists():
        shutil.rmtree(APP_DIR)
    (APP_DIR / "Contents" / "MacOS").mkdir(parents=True)
    shutil.copy2(build / "MusicOrganizer", APP_DIR / "Contents" / "MacOS" / "MusicOrganizer")
    with open(APP_DIR / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({
            "CFBundleName": "Music Organizer", "CFBundleDisplayName": "Music Organizer",
            "CFBundleIdentifier": "com.srikarreddyram.musicorganizer", "CFBundleExecutable": "MusicOrganizer",
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1",
            "LSMinimumSystemVersion": "14.0", "LSUIElement": True,  # menu bar only, no Dock icon
            "NSAppleEventsUsageDescription": "Music Organizer reads your library and, when you press Apply, "
                                             "creates playlists and adds songs in Music.",
            "OrganizerProject": str(root), "OrganizerPython": sys.executable,
            "CFBundleURLTypes": [{"CFBundleURLName": "Music Organizer", "CFBundleURLSchemes": ["musicorganizer"]}],
        }, f)
    subprocess.run(["codesign", "--force", "--sign", "-", str(APP_DIR)], capture_output=True)
    subprocess.run(["/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
                    "/Support/lsregister", "-f", str(APP_DIR)], capture_output=True)  # register the URL scheme
    subprocess.run(["open", str(APP_DIR)])
    cmd_install_scripts(args)
    print(f"\nInstalled {APP_DIR} and started it. In Music: Scripts menu (scroll icon) → ✨ Organizer.")


def cmd_discover(args):
    lib = load_inventory()
    progress = lambda msg: print(f"  {msg}", file=sys.stderr)
    if args.fresh:
        result = discover.fresh(lib, country=args.country.split(",")[0], days=args.days, seeds=args.seeds,
                                max_ratio=args.max_ratio, limit=args.limit, progress=progress)
    else:
        result = discover.trending(lib, countries=tuple(args.country.split(",")), genres=args.genres,
                                   seeds=args.seeds, max_ratio=args.max_ratio, limit=args.limit,
                                   progress=progress)
    path = discover.save(result)
    discover.print_result(result)
    print(f"\nSaved to {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan").set_defaults(fn=cmd_scan)
    r = sub.add_parser("report")
    r.add_argument("--limit", type=int, default=25)
    r.set_defaults(fn=cmd_report)

    ar = sub.add_parser("artist", help="plan a playlist of an artist's best songs for your taste")
    ar.add_argument("name")
    ar.add_argument("--size", type=int, default=20)
    ar.add_argument("--country", default="in", help="Apple Music storefront")
    ar.add_argument("--playlist-name", help='default: "<Artist> · For You"')
    ar.set_defaults(fn=cmd_artist)

    sub.add_parser("fill", help="add newly added songs to artist playlists").set_defaults(fn=cmd_fill)

    sp = sub.add_parser("split", help="plan vibe-based splits of big playlists")
    sp.add_argument("playlists", nargs="+", metavar="PLAYLIST")
    sp.add_argument("--min-size", type=int, default=10, help="merge smaller groups into their nearest neighbour")
    sp.add_argument("--verbose", action="store_true", help="list every track")
    sp.set_defaults(fn=cmd_split)

    cl = sub.add_parser("check-labels", help="compare energy labels with measured audio")
    cl.add_argument("--threshold", type=float, default=1.25)
    cl.set_defaults(fn=cmd_check_labels)

    sub.add_parser("evaluate", help="benchmark playlist fit against your own playlists").set_defaults(fn=cmd_evaluate)

    lb = sub.add_parser("label", help="override labels for one track (shows them if no options)")
    lb.add_argument("track", metavar="TRACK_ID")
    lb.add_argument("--mood", choices=labels.MOODS)
    lb.add_argument("--energy", type=int, choices=range(1, 6))
    lb.add_argument("--contexts", help="comma separated: " + ",".join(labels.CONTEXTS))
    lb.add_argument("--language", choices=labels.LANGUAGES)
    lb.add_argument("--style", choices=labels.STYLES)
    lb.add_argument("--group", choices=reorg.BUCKETS + list(labels.FAMILIES) + [reorg.KEEP_OUT],
                    help="force the split group (or keep-out: leave it only in the original)")
    lb.set_defaults(fn=cmd_label)

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

    rn = sub.add_parser("rename", help="rename a playlist a plan will create")
    rn.add_argument("plan")
    rn.add_argument("op", type=int)
    rn.add_argument("name")
    rn.set_defaults(fn=cmd_rename)

    nm = sub.add_parser("names", help="show (or --reroll) name options for a plan's new playlists")
    nm.add_argument("plan")
    nm.add_argument("--reroll", type=int, default=0, help="any number for a fresh set")
    nm.set_defaults(fn=cmd_names)

    dr = sub.add_parser("drop")
    dr.add_argument("plan")
    dr.add_argument("op", type=int)
    dr.add_argument("tracks", help='track numbers within the op, like "2,7-9"')
    dr.set_defaults(fn=cmd_drop)

    ap_ = sub.add_parser("apply")
    ap_.add_argument("plan")
    ap_.add_argument("--confirm", metavar="PLAN_ID", help="non-interactive confirmation; must equal the plan id")
    ap_.set_defaults(fn=cmd_apply)
    a = sub.add_parser("api", help="JSON interface for the menu bar app")
    a.add_argument("command")
    a.add_argument("args", nargs="*")
    a.set_defaults(fn=cmd_api)

    u = sub.add_parser("ui", help="dialog flows used by the Music Scripts menu")
    u.add_argument("action", choices=["review", "belong", "discover", "refresh", "artist", "move"])  # old dialogs
    u.add_argument("ids", nargs="*", metavar="TRACK_ID")
    u.set_defaults(fn=cmd_ui)

    sub.add_parser("install-scripts", help="add actions to Music's Scripts menu").set_defaults(fn=cmd_install_scripts)
    sub.add_parser("install-app", help="build and start the menu bar app").set_defaults(fn=cmd_install_app)

    en = sub.add_parser("enrich", help="add metadata from Apple's catalog and preview audio (uses the internet)")
    en.add_argument("sources", nargs="*", choices=["apple", "audio", "musicbrainz", "lastfm"], metavar="SOURCE")
    en.add_argument("--country", default="in", help="Apple storefront for catalog matching")
    en.add_argument("--retry-misses", action="store_true", help="try catalog matching again for unmatched tracks")
    en.add_argument("--reanalyze", action="store_true", help="re-measure all previews (after analysis changes)")
    en.set_defaults(fn=cmd_enrich)

    dc = sub.add_parser("discover", help="songs near your taste from lesser-known artists (uses the internet)")
    dc.add_argument("--fresh", action="store_true", help="recent releases instead of what's charting now")
    dc.add_argument("--country", default="in,us", help="Apple storefronts, comma separated (default: in,us)")
    dc.add_argument("--genres", type=int, default=8, help="how many of your top genres to pull charts for")
    dc.add_argument("--days", type=int, default=90, help="--fresh: how recent a release must be")
    dc.add_argument("--max-ratio", type=float, default=2.0,
                    help="skip artists with more than this many times the fans of your usual artists")
    dc.add_argument("--seeds", type=int, default=30, help="how many of your top artists to start from")
    dc.add_argument("--limit", type=int, default=30)
    dc.set_defaults(fn=cmd_discover)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
