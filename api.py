"""JSON interface for the menu bar app.

    python3 organizer.py api COMMAND [ARGS...] [< request.json]

Each command prints exactly one JSON object on stdout; progress lines go to stderr as
"PROGRESS <text>". Changes to Music go through the same plans, verification, audit
log and undo as everywhere else, and only for ops the request explicitly lists.
"""
import contextlib
import json
import sys

import artist
import discover
import labels
import music_bridge
import plans
import reorg
import suggest
import ui


def progress(text):
    print(f"PROGRESS {text}", file=sys.stderr, flush=True)


def read_request():
    data = sys.stdin.read()
    return json.loads(data) if data.strip() else {}


def track_info(t):
    return {"id": t["persistentID"], "name": t["name"], "artist": t["artist"]}


# --- Context and plans -----------------------------------------------------------

def cmd_context(args):
    lib = ui.load_lib()
    ctx = music_bridge.current_context()
    tracks = {t["persistentID"]: t for t in lib["tracks"]}
    eff = labels.merged()
    out = {"scannedAt": lib.get("scannedAt"), "playlist": None,
           "selection": [dict(s, scanned=s["id"] in tracks) for s in ctx.get("selection", [])]}
    pl = ctx.get("playlist")
    if pl:
        known = next((p for p in lib["playlists"] if p["persistentID"] == pl["persistentID"]), None)
        ids = list(dict.fromkeys(known["trackIDs"])) if known else []
        editable = suggest.editable(pl)
        made_by_us = pl["persistentID"] in plans.created_playlists()
        labelled = sum(1 for i in ids if i in eff)
        out["playlist"] = {"id": pl["persistentID"], "name": pl["name"].strip(), "editable": editable,
                           "size": len(ids), "labelled": labelled,
                           "canSplit": editable and not made_by_us and labelled >= 20}
    return out


def cmd_playlists(args):
    """Your own playlists, biggest first, with whether they can be split."""
    lib = ui.load_lib()
    eff = labels.merged()
    made = plans.created_playlists()
    out = []
    for p in lib["playlists"]:
        if not suggest.editable(p) or p["persistentID"] in made:
            continue
        ids = list(dict.fromkeys(p["trackIDs"]))
        labelled = sum(1 for i in ids if i in eff)
        out.append({"id": p["persistentID"], "name": p["name"].strip(), "size": len(ids), "labelled": labelled,
                    "canSplit": labelled >= 20})
    out.sort(key=lambda x: (-x["canSplit"], -x["size"]))
    return {"playlists": out}


def op_view(op, plan):
    kind = op["op"]
    text = {"create_playlist": "Create", "add_tracks": "Add to", "remove_tracks": "Remove from",
            "delete_playlist": "Delete"}[kind] + " " + plans.target_label(op).replace("new playlist ", "")
    return {"n": op["n"], "op": kind, "status": op["status"], "approved": op["approved"], "text": text,
            "reason": op.get("reason"), "ref": op.get("ref") or (op.get("playlist") or {}).get("ref"),
            "playlist": (op.get("playlist") or {}).get("name") or op.get("name"),
            "name": op.get("name"), "nameOptions": op.get("nameOptions") or [],
            "bucket": op.get("bucket"), "count": len(op.get("tracks") or []),
            "tracks": [{"name": t["name"], "artist": t["artist"], "note": t.get("note")}
                       for t in (op.get("tracks") or [])[:60]],
            "result": (op.get("result") or {}).get("detail")}


def plan_view(plan):
    return {"id": plan["id"], "title": plan["title"], "createdAt": plan["createdAt"],
            "pending": sum(op["status"] == "pending" for op in plan["ops"]),
            "isUndo": plan["title"].startswith("Undo"), "waiting": plan.get("waiting", []),
            "leftOut": plan.get("leftOut", {}), "ops": [op_view(op, plan) for op in plan["ops"]]}


def cmd_plans(args):
    pending = [p for p in plans.all_plans() if any(op["status"] == "pending" for op in p["ops"])]
    pending.sort(key=lambda p: p["createdAt"], reverse=True)
    return {"plans": [plan_view(p) for p in pending]}


def cmd_plan(args):
    return {"plan": plan_view(plans.load_plan(args[0]))}


def apply_and_report(plan):
    with contextlib.redirect_stdout(sys.stderr):
        done, undo = plans.apply_plan(plan)
        progress("Re-reading your library")
        ui.rescan()
    return {"plan": plan["id"], "applied": sum(op["status"] == "applied" for op in done), "total": len(done),
            "results": [{"n": op["n"], "status": op["status"], "text": op_view(op, plan)["text"],
                         "detail": (op.get("result") or {}).get("detail")} for op in done],
            "undo": undo["id"] if undo else None, "waiting": plan.get("waiting", [])}


def cmd_apply(args):
    """Request: {"plan": id, "ops": [n, ...], "names": {"n": "Playlist name"}}"""
    req = read_request()
    plan = plans.load_plan(req["plan"])
    ui.approve_with_creates(plan, {int(n) for n in req.get("ops", [])})
    for n, name in (req.get("names") or {}).items():
        if name and name.strip():
            plans.rename(plan, int(n), name.strip())
    plan = plans.load_plan(plan["id"])
    if not any(op["approved"] and op["status"] == "pending" for op in plan["ops"]):
        return {"error": "Nothing selected to apply."}
    return apply_and_report(plan)


def cmd_undo(args):
    plan = plans.load_plan(args[0])
    ui.approve_with_creates(plan, {op["n"] for op in plan["ops"] if op["status"] == "pending"})
    return apply_and_report(plans.load_plan(plan["id"]))


# --- Suggestions ---------------------------------------------------------------------

def cmd_belong(args):
    lib = ui.load_lib()
    tracks = {t["persistentID"]: t for t in lib["tracks"]}
    progress("Comparing with your playlists")
    rows = ui.belong_suggestions(lib, args)
    songs = {}
    for tid, p, score, why in rows:
        songs.setdefault(tid, dict(track_info(tracks[tid]), suggestions=[]))["suggestions"].append(
            {"playlistId": p["persistentID"], "playlist": p["name"].strip(), "score": round(score, 3), "why": why})
    return {"songs": list(songs.values()), "unknown": [i for i in args if i not in tracks],
            "noMatch": [track_info(tracks[i]) for i in args if i in tracks and i not in songs]}


def cmd_add(args):
    """Request: {"picks": [[track id, playlist id], ...]}"""
    req = read_request()
    lib = ui.load_lib()
    by_id = {p["persistentID"]: p for p in lib["playlists"]}
    picks = [(tid, by_id[pid]) for tid, pid in req["picks"] if pid in by_id]
    if not picks:
        return {"error": "Nothing selected."}
    return apply_and_report(ui.add_plan(lib, picks, "Where do these belong", "chosen in the menu bar app"))


def cmd_split(args):
    lib = ui.load_lib()
    pl = next((p for p in lib["playlists"] if p["persistentID"] == args[0]), None)
    if not pl:
        return {"error": "That playlist isn't in the last scan yet. Try Refresh."}
    progress(f"Sorting {pl['name'].strip()} by vibe")
    ops, left = reorg.split_ops(lib, pl)
    if not ops:
        return {"error": "No labelled songs to split in this playlist."}
    plan = plans.new_plan(f"Split {pl['name'].strip()}", ops, lib)
    plan["leftOut"] = {pl["name"].strip(): left} if left else {}
    plans.save_plan(plan)
    return {"plan": plan_view(plan)}


ARTIST_CACHE = ui.DATA / "ui_artist.json"


def cmd_artist(args):
    lib = ui.load_lib()
    progress(f"Listening to {args[0]}'s catalogue")
    with contextlib.redirect_stdout(sys.stderr):
        result = artist.best_songs(lib, args[0], progress=progress)
    ARTIST_CACHE.write_text(json.dumps(result, ensure_ascii=False))
    return {"artist": result["artist"], "soundUsed": result["soundUsed"],
            "picks": [{"i": i, "name": p["name"], "artist": p["artist"], "owned": bool(p["libraryID"]),
                       "url": p["url"], "why": artist.why(p)} for i, p in enumerate(result["picks"])]}


def cmd_artist_create(args):
    """Request: {"keep": [pick index, ...]} for the last `artist` result."""
    req = read_request()
    result = json.loads(ARTIST_CACHE.read_text())
    result["picks"] = [p for i, p in enumerate(result["picks"]) if i in set(req.get("keep", []))]
    if not result["picks"]:
        return {"error": "Pick at least one song."}
    lib = ui.load_lib()
    ops, waiting = artist.plan_ops(lib, result)
    plan = plans.new_plan(f"Artist playlist: {result['artist']}", ops, lib)
    plan["waiting"] = waiting
    plans.save_plan(plan)
    return {"plan": plan_view(plan)}


def cmd_fill(args):
    plan = plans.load_plan(args[0])
    progress("Looking for the songs you added")
    with contextlib.redirect_stdout(sys.stderr):
        lib = ui.rescan()
    ops, still = artist.fill_ops(lib, plan)
    plan["waiting"] = still
    plans.save_plan(plan)
    if not ops:
        return {"error": "None of them are in your library yet. Add them with + in Music first.", "waiting": still}
    fill = plans.new_plan(f"Fill {plan['title']}", ops, lib)
    for op in fill["ops"]:
        op["approved"] = True
    plans.save_plan(fill)
    return dict(apply_and_report(fill), waiting=still)


def cmd_discover(args):
    lib = ui.load_lib()
    chosen = [t for t in lib["tracks"] if t["persistentID"] in set(args)]
    if not chosen:
        return {"error": "Select some songs in Music first."}
    progress("Checking what's charting near these songs")
    with contextlib.redirect_stdout(sys.stderr):
        result = discover.trending({**lib, "tracks": chosen}, seeds=min(10, len(chosen) * 2), limit=20,
                                   owned=lib["tracks"], progress=progress)
    discover.save(result)
    return {"picks": [{"name": p["name"], "artist": p["artist"], "url": p.get("url"),
                       "chart": (p.get("charts") or [None])[0], "fans": p.get("fans"),
                       "known": p.get("known"), "because": p.get("because") or []} for p in result["picks"]]}


# --- Move Song ---------------------------------------------------------------------

def cmd_move_options(args):
    lib = ui.load_lib()
    tracks = {t["persistentID"]: t for t in lib["tracks"]}
    made = ui.organizer_playlists(lib)
    by_pl = {p["persistentID"]: p for p in lib["playlists"]}
    groups = {}
    for tid in args:
        home = next((pid for pid in made if tid in by_pl[pid]["trackIDs"]), None)
        if home:
            groups.setdefault(home, []).append(tid)
    out = []
    for home, tids in groups.items():
        op, _ = made[home]
        out.append({"home": {"id": home, "name": by_pl[home]["name"]}, "source": op["source"],
                    "tracks": [track_info(tracks[t]) for t in tids if t in tracks],
                    "targets": [{"id": pid, "name": by_pl[pid]["name"], "group": o["group"]}
                                for pid, (o, _) in made.items() if o["source"] == op["source"] and pid != home]})
    return {"groups": out}


def cmd_move(args):
    """Request: {"home": playlist id, "target": playlist id or null, "tracks": [track id, ...]}"""
    req = read_request()
    lib = ui.load_lib()
    tracks = {t["persistentID"]: t for t in lib["tracks"]}
    made = ui.organizer_playlists(lib)
    by_pl = {p["persistentID"]: p for p in lib["playlists"]}
    home, target, tids = req["home"], req.get("target"), req["tracks"]
    ops = [{"op": "remove_tracks", "playlist": {"id": home, "name": by_pl[home]["name"]},
            "tracks": [plans.track_ref(tracks[t]) for t in tids], "reason": "moved in the menu bar app"}]
    if target:
        ops.append({"op": "add_tracks", "playlist": {"id": target, "name": by_pl[target]["name"]},
                    "tracks": [plans.track_ref(tracks[t]) for t in tids], "reason": "moved in the menu bar app"})
    plan = plans.new_plan("Move Song", ops, lib)
    for op in plan["ops"]:
        op["approved"] = True
    plans.save_plan(plan)
    manual = labels.load("manual")
    for t in tids:
        manual.setdefault(t, {})["group"] = made[target][0]["group"] if target else reorg.KEEP_OUT
    labels.save("manual", manual)
    return apply_and_report(plan)


def cmd_refresh(args):
    import metadata
    progress("Re-reading your library")
    with contextlib.redirect_stdout(sys.stderr):
        before = {t["persistentID"] for t in ui.load_lib()["tracks"]} if ui.INVENTORY.exists() else set()
        lib = ui.rescan()
        new = [t for t in lib["tracks"] if t["persistentID"] not in before]
        progress(f"{len(new)} new songs; fetching metadata")
        metadata.enrich_apple(lib, progress=progress)
        metadata.enrich_audio(lib, progress=progress)
        metadata.enrich_musicbrainz(lib, progress=progress)
    unlabelled = sum(1 for t in lib["tracks"] if t["persistentID"] not in labels.merged())
    return {"tracks": len(lib["tracks"]), "new": len(new), "unlabelled": unlabelled}


COMMANDS = {
    "context": cmd_context, "playlists": cmd_playlists, "plans": cmd_plans, "plan": cmd_plan, "apply": cmd_apply, "undo": cmd_undo,
    "belong": cmd_belong, "add": cmd_add, "split": cmd_split, "artist": cmd_artist,
    "artist-create": cmd_artist_create, "fill": cmd_fill, "discover": cmd_discover,
    "move-options": cmd_move_options, "move": cmd_move, "refresh": cmd_refresh,
}


def main(command, args):
    try:
        with contextlib.redirect_stdout(sys.stderr):  # nothing but the answer goes to stdout
            result = COMMANDS[command](args)
    except SystemExit as e:
        result = {"error": str(e)}
    except Exception as e:  # noqa: BLE001 - the app shows the message instead of a crash
        result = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(result, ensure_ascii=False))
