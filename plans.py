"""Change plans: reviewable, individually approved operations on the Music library.

A plan is a JSON file in data/plans/. Each op starts unapproved and pending.
`apply` runs only approved, pending ops, re-reads the playlist from Music after
each one, and marks it applied only if the new state matches what was asked.
Every op is written to data/audit.jsonl, and an undo plan is generated for the
ops that actually changed something. Undo plans need approval like any other.
"""
import json
import re
from datetime import datetime
from pathlib import Path

import music_bridge

DATA = Path(__file__).parent / "data"
PLANS = DATA / "plans"
AUDIT = DATA / "audit.jsonl"
# Playlists this tool created. Only these may ever be deleted.
CREATED = DATA / "created_playlists.json"

OPS = ("create_playlist", "add_tracks", "remove_tracks", "delete_playlist")


def now():
    return datetime.now().isoformat(timespec="seconds")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def track_ref(t):
    return {"id": t["persistentID"], "name": t["name"], "artist": t["artist"]}


def new_plan(title, ops, inventory=None):
    stamp = now()
    for n, op in enumerate(ops, 1):
        assert op["op"] in OPS, op["op"]
        op.update(n=n, approved=False, status="pending", result=None)
    return {
        "id": f"{stamp.replace(':', '')}-{slug(title)}",
        "title": title,
        "createdAt": stamp,
        "inventoryScannedAt": inventory and inventory.get("scannedAt"),
        "ops": ops,
    }


def plan_path(plan):
    return PLANS / f"{plan['id']}.json"


def save_plan(plan):
    PLANS.mkdir(parents=True, exist_ok=True)
    path = plan_path(plan)
    path.write_text(json.dumps(plan, indent=1, ensure_ascii=False))
    return path


def load_plan(ref):
    """Load by path, full id, or unique id prefix/substring."""
    p = Path(ref)
    if not p.exists():
        matches = sorted(PLANS.glob(f"*{ref}*.json"))
        if len(matches) != 1:
            raise SystemExit(f"{len(matches)} plans match {ref!r}; use `plans` to list them")
        p = matches[0]
    return json.loads(p.read_text())


def all_plans():
    return [json.loads(p.read_text()) for p in sorted(PLANS.glob("*.json"))]


def target_label(op):
    pl = op.get("playlist") or {}
    if op["op"] == "create_playlist":
        return f"new playlist {op['name']!r}"
    if pl.get("ref"):
        return f"new playlist {pl.get('name')!r} (from op {pl['ref']})"
    return f"{(pl.get('name') or '').strip()!r} [{pl.get('id')}]"


def print_plan(plan, verbose=True):
    print(f"PLAN {plan['id']}")
    print(f"  {plan['title']}")
    if plan.get("inventoryScannedAt"):
        print(f"  based on scan from {plan['inventoryScannedAt']}")
    for op in plan["ops"]:
        mark = "x" if op["approved"] else " "
        verb = {"create_playlist": "CREATE", "add_tracks": "ADD to", "remove_tracks": "REMOVE from",
                "delete_playlist": "DELETE"}[op["op"]]
        count = f" ({len(op['tracks'])} tracks)" if op.get("tracks") else ""
        print(f"\n [{mark}] {op['n']:>2}. {verb} {target_label(op)}{count}   <{op['status']}>")
        if verbose and op.get("nameOptions") and op["status"] == "pending":
            print(f"        other names: {' / '.join(o for o in op['nameOptions'] if o != op['name'])}")
        if op.get("reason"):
            print(f"        why: {op['reason']}")
        if verbose:
            for i, t in enumerate(op.get("tracks") or [], 1):
                extra = f"   [{t['note']}]" if t.get("note") else ""
                print(f"        {i:>3}. {t['name']} - {t['artist']}{extra}")
        if op.get("result") and op["status"] != "pending":
            print(f"        result: {op['result'].get('detail')}")


def parse_ops(spec, plan):
    if spec == "all":
        return [op["n"] for op in plan["ops"]]
    ns = set()
    for part in spec.split(","):
        a, _, b = part.partition("-")
        ns.update(range(int(a), int(b or a) + 1))
    return sorted(ns)


def set_approval(plan, spec, value):
    by_n = {op["n"]: op for op in plan["ops"]}
    for n in parse_ops(spec, plan):
        if n not in by_n:
            raise SystemExit(f"plan has no op {n}")
        if by_n[n]["status"] != "pending":
            raise SystemExit(f"op {n} is already {by_n[n]['status']}")
        by_n[n]["approved"] = value
    save_plan(plan)


def rename(plan, n, name):
    """Rename a not-yet-created playlist, keeping the ops that add to it in step."""
    op = next((o for o in plan["ops"] if o["n"] == n), None)
    if not op or op["op"] != "create_playlist":
        raise SystemExit(f"op {n} doesn't create a playlist")
    if op["status"] != "pending":
        raise SystemExit(f"op {n} is already {op['status']}; rename the playlist in Music instead")
    op["name"] = name
    if op.get("ref"):
        import names
        names.remember(op["ref"], [name])
    for o in plan["ops"]:
        if (o.get("playlist") or {}).get("ref") == op.get("ref"):
            o["playlist"]["name"] = name
    save_plan(plan)


def drop_tracks(plan, n, spec):
    op = next((o for o in plan["ops"] if o["n"] == n), None)
    if not op or not op.get("tracks"):
        raise SystemExit(f"op {n} has no track list")
    if op["status"] != "pending":
        raise SystemExit(f"op {n} is already {op['status']}")
    drop = set(parse_ops(spec, {"ops": []}))
    op["tracks"] = [t for i, t in enumerate(op["tracks"], 1) if i not in drop]
    op["approved"] = False  # the op changed, so it needs a fresh look
    save_plan(plan)


# --- Applying ----------------------------------------------------------------

def created_playlists():
    return json.loads(CREATED.read_text()) if CREATED.exists() else {}


def remember_created(pid, name, plan_id):
    reg = created_playlists()
    reg[pid] = {"name": name, "plan": plan_id, "at": now()}
    CREATED.write_text(json.dumps(reg, indent=1, ensure_ascii=False))


def audit(entry):
    with AUDIT.open("a") as f:
        f.write(json.dumps({"at": now(), **entry}, ensure_ascii=False) + "\n")


def resolve_playlist(op, plan):
    pl = op["playlist"]
    if pl.get("id"):
        return pl["id"]
    src = next((o for o in plan["ops"] if o["op"] == "create_playlist" and o.get("ref") == pl.get("ref")), None)
    if not src or src["status"] != "applied":
        raise RuntimeError(f"depends on creating playlist {pl.get('name')!r}, which has not been applied")
    return src["result"]["playlistID"]


def check_editable(state):
    if state is None:
        raise RuntimeError("playlist no longer exists")
    if state["smart"] or state["specialKind"] != "none":
        raise RuntimeError(f"{state['name']!r} is not an editable user playlist")


def run_op(op, plan):
    """Perform one op and verify it. Returns (verified, result, undo_ops)."""
    kind = op["op"]

    if kind == "create_playlist":
        pid = music_bridge.create_playlist(op["name"])
        after = music_bridge.read_playlist(pid)
        ok = bool(after) and after["name"] == op["name"] and not after["trackIDs"]
        remember_created(pid, op["name"], plan["id"])
        undo = [{"op": "delete_playlist", "playlist": {"id": pid, "name": op["name"]},
                 "reason": f"undo: created by plan {plan['id']} op {op['n']}"}]
        return ok, {"playlistID": pid, "after": after,
                    "detail": f"created [{pid}]" if ok else f"created [{pid}] but state is {after}"}, undo

    pid = resolve_playlist(op, plan)
    before = music_bridge.read_playlist(pid)
    check_editable(before)
    pl = {"id": pid, "name": before["name"]}
    ids = [t["id"] for t in op.get("tracks") or []]

    if kind == "add_tracks":
        todo = [i for i in ids if i not in before["trackIDs"]]
        errors = music_bridge.add_tracks(pid, todo) if todo else {}
        after = music_bridge.read_playlist(pid)
        missing = [i for i in todo if i not in after["trackIDs"]]
        added = [i for i in todo if i in after["trackIDs"]]
        ok = not missing
        detail = f"added {len(added)}, {len(ids) - len(todo)} already present"
        if missing:
            detail += f", {len(missing)} NOT added: " + "; ".join(f"{i}: {errors.get(i) or 'not in playlist after add'}" for i in missing)
        undo_tracks = [t for t in op["tracks"] if t["id"] in added]
        undo = [{"op": "remove_tracks", "playlist": pl, "tracks": undo_tracks,
                 "reason": f"undo: added by plan {plan['id']} op {op['n']}"}] if undo_tracks else []

    elif kind == "remove_tracks":
        todo = [i for i in ids if i in before["trackIDs"]]
        errors = music_bridge.remove_tracks(pid, todo) if todo else {}
        after = music_bridge.read_playlist(pid)
        still = [i for i in todo if i in after["trackIDs"]]
        removed = [i for i in todo if i not in after["trackIDs"]]
        ok = not still
        detail = f"removed {len(removed)}, {len(ids) - len(todo)} were not in the playlist"
        if still:
            detail += f", {len(still)} STILL present: " + "; ".join(f"{i}: {errors.get(i) or 'still in playlist'}" for i in still)
        undo_tracks = [t for t in op["tracks"] if t["id"] in removed]
        undo = [{"op": "add_tracks", "playlist": pl, "tracks": undo_tracks,
                 "reason": f"undo: removed by plan {plan['id']} op {op['n']} (re-added at the end, original order not kept)"}] if undo_tracks else []

    elif kind == "delete_playlist":
        if pid not in created_playlists():
            raise RuntimeError("refusing to delete a playlist this tool did not create")
        music_bridge.delete_playlist(pid)
        after = music_bridge.read_playlist(pid)
        ok = after is None
        detail = "deleted" if ok else "playlist still exists"
        tracks = [{"id": i, "name": "", "artist": ""} for i in before["trackIDs"]]
        undo = [{"op": "create_playlist", "name": before["name"], "ref": "restored",
                 "reason": f"undo: deleted by plan {plan['id']} op {op['n']}"}]
        if tracks:
            undo.append({"op": "add_tracks", "playlist": {"ref": "restored", "name": before["name"]},
                         "tracks": tracks, "reason": "undo: restore the deleted playlist's tracks"})

    return ok, {"playlistID": pid, "before": before["trackIDs"], "after": after and after["trackIDs"],
                "detail": detail}, undo


def apply_plan(plan):
    """Apply approved pending ops in order, stopping at the first failure."""
    todo = [op for op in plan["ops"] if op["approved"] and op["status"] == "pending"]
    undo_ops = []
    for op in todo:
        print(f"  op {op['n']}: {op['op']} {target_label(op)} ...", end=" ", flush=True)
        try:
            ok, result, undo = run_op(op, plan)
        except Exception as e:  # noqa: BLE001 - record any failure, never claim success
            ok, result, undo = False, {"detail": f"error: {e}"}, []
        op["status"] = "applied" if ok else "failed"
        op["result"] = result
        op["appliedAt"] = now()
        undo_ops = undo + undo_ops  # undo runs in reverse order
        save_plan(plan)
        audit({"plan": plan["id"], "n": op["n"], "op": op["op"], "verified": ok, **result,
               "tracks": [t["id"] for t in op.get("tracks") or []]})
        print(("OK - " if ok else "FAILED - ") + result["detail"])
        if not ok:
            print("  Stopping. Later ops were not attempted.")
            break

    undo_plan = None
    if undo_ops:
        # Tracks in a playlist that the undo deletes don't need removing first.
        deleted = {o["playlist"]["id"] for o in undo_ops if o["op"] == "delete_playlist"}
        undo_ops = [o for o in undo_ops if o["op"] != "remove_tracks" or o["playlist"].get("id") not in deleted]
        undo_plan = new_plan(f"Undo {plan['id']}", undo_ops)
        save_plan(undo_plan)
    return todo, undo_plan
