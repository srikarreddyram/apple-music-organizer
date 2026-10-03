"""Split big mixed playlists into tighter vibe playlists, as a reviewable plan.

Every track of the source playlist lands in exactly one new playlist, in the
source's order. The source playlist itself is never touched. Buckets come from
the effective labels (manual overrides first, then AI-inferred), so fixing a
label with a manual override and re-running `split` moves the track.
"""
from collections import OrderedDict

import labels
from plans import track_ref
from suggest import song_key

FEELS = {"romantic", "heartbreak", "melancholic", "introspective", "nostalgic"}
BUCKETS = ["Gym", "Party", "Cruise", "Feels", "Late Night"]
# Where a too-small bucket goes instead, in order of preference.
NEAREST = {
    "Gym": ["Party", "Cruise"], "Party": ["Gym", "Cruise"], "Cruise": ["Party", "Feels", "Gym"],
    "Feels": ["Late Night", "Cruise"], "Late Night": ["Feels", "Cruise"],
}
ABOUT = {
    "Gym": "energy 5, or energy 4 hype/aggressive",
    "Party": "high-energy feel-good, euphoric or party tracks",
    "Cruise": "mid-energy, confident or laid-back",
    "Feels": "mid-energy romantic, heartbreak, nostalgic or introspective",
    "Late Night": "low energy, or mellow late-night/wind-down tracks",
}


def bucket(label):
    e, m, c = label["energy"], label["mood"], set(label["contexts"])
    if e >= 5 or (e >= 4 and m in {"hype", "aggressive"}):
        return "Party" if "party" in c and "workout" not in c else "Gym"
    if e <= 2 or (e == 3 and m in FEELS | {"dreamy", "chill"} and c & {"late-night", "wind-down"}):
        return "Late Night"
    if e >= 4 and (m in {"feel-good", "euphoric"} or "party" in c):
        return "Party"
    if m in FEELS:
        return "Feels"
    return "Cruise"


def note(label):
    conf = "" if label.get("confidence") == "high" else f" ({label.get('confidence')} confidence)"
    src = " [manual]" if label.get("source", "").startswith("manual") else ""
    return f"energy {label['energy']} · {label['mood']} · {label['style']}{conf}{src}"


def split_ops(lib, playlist, min_size=10, name_format="{source} · {bucket}"):
    """Plan ops creating one playlist per vibe bucket of `playlist`, plus unlabelled leftovers."""
    tracks = {t["persistentID"]: t for t in lib["tracks"]}
    eff = labels.merged()
    groups = OrderedDict((b, []) for b in BUCKETS)
    unlabelled, seen = [], set()
    for pid in dict.fromkeys(playlist["trackIDs"]):  # keep order, drop repeated entries
        if pid in tracks:
            key = song_key(tracks[pid])
            if key in seen:
                continue  # another edition of a song already placed
            seen.add(key)
        if pid in eff:
            groups[bucket(eff[pid])].append(pid)
        elif pid in tracks:
            unlabelled.append(pid)

    # Fold buckets that are too small into their nearest neighbour, smallest first.
    for b in sorted(groups, key=lambda b: len(groups[b])):
        if 0 < len(groups[b]) < min_size:
            target = next((n for n in NEAREST[b] if len(groups[n]) >= min_size), None)
            if target:
                groups[target] += groups[b]
                groups[b] = []
    order = {}
    for i, pid in enumerate(playlist["trackIDs"]):
        order.setdefault(pid, i)  # first position counts when a song is listed twice

    source = playlist["name"].strip()
    ops = []
    for b, ids in groups.items():
        if not ids:
            continue
        ids.sort(key=order.get)
        name = name_format.format(source=source, bucket=b)
        ref = f"{source}-{b}"
        ops.append({"op": "create_playlist", "name": name, "ref": ref,
                    "reason": f"Split of {source!r}: {ABOUT[b]}"})
        ops.append({"op": "add_tracks", "playlist": {"ref": ref, "name": name},
                    "tracks": [{**track_ref(tracks[i]), "note": note(eff[i])} for i in ids],
                    "reason": f"{len(ids)} of {len(order)} tracks from {source!r}; the original stays as it is"})
    if unlabelled:
        ops.append({"op": "create_playlist", "name": f"{source} · Unsorted", "ref": f"{source}-unsorted",
                    "reason": "tracks with no labels yet"})
        ops.append({"op": "add_tracks", "playlist": {"ref": f"{source}-unsorted", "name": f"{source} · Unsorted"},
                    "tracks": [track_ref(tracks[i]) for i in unlabelled],
                    "reason": "label these, then re-run split"})
    return ops
