"""Split big mixed playlists into tighter vibe playlists, as a reviewable plan.

Every track of the source playlist lands in exactly one new playlist, in the
source's order. The source playlist itself is never touched. Buckets come from
the effective labels (manual overrides first, then AI-inferred), so fixing a
label with a manual override and re-running `split` moves the track.
"""
from collections import Counter, OrderedDict

import labels
import names
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


# Style families that sit fine next to each other in one vibe playlist.
COMPATIBLE = {
    "rap": {"rnb", "electronic", "latin", "pop"}, "rnb": {"rap", "pop", "latin"},
    "pop": {"rnb", "rock", "electronic", "latin", "rap"},
    "rock": {"pop"}, "electronic": {"pop", "rap"}, "latin": {"rap", "pop", "rnb"},
    "indian-film": {"indian-other"}, "indian-other": {"indian-film"}, "roots": set(), "other": set(),
}
FAMILY_LABEL = {"roots": "Folk & Country", "rock": "Rock", "rap": "Rap", "rnb": "R&B", "pop": "Pop",
                "electronic": "Electronic", "latin": "Latin", "indian-film": "Film", "indian-other": "Indie",
                "other": "Wildcards"}


def coherent_parts(ids, eff, max_spread=2):
    """A pulled-out group must also hang together: a wide energy spread splits it loud vs quiet."""
    energies = [eff[i]["energy"] for i in ids]
    if max(energies) - min(energies) <= max_spread:
        return [ids]
    return [[i for i in ids if eff[i]["energy"] >= 4], [i for i in ids if eff[i]["energy"] <= 3]]


def split_ops(lib, playlist, min_size=10, used=None, min_family=4, rare=0.15, dominant=0.5):
    """Plan ops splitting `playlist` into vibe playlists. Returns (ops, left_out).

    Songs whose style family is rare in the playlist and doesn't blend with its main sound
    (John Denver in a pop playlist) aren't scattered across the vibe groups: `min_family` or
    more of a kind get their own playlist, fewer stay only in the original (`left_out`).
    `used`: names already taken (other playlists, earlier splits in the same plan); extended in place.
    """
    used = used if used is not None else {p["name"].strip() for p in lib["playlists"]}
    tracks = {t["persistentID"]: t for t in lib["tracks"]}
    eff = labels.merged()
    labelled, unlabelled, seen = [], [], set()
    for pid in dict.fromkeys(playlist["trackIDs"]):  # keep order, drop repeated entries
        if pid in tracks:
            key = song_key(tracks[pid])
            if key in seen:
                continue  # another edition of a song already placed
            seen.add(key)
        if pid in eff:
            labelled.append(pid)
        elif pid in tracks:
            unlabelled.append(pid)

    fam = {pid: labels.FAMILY.get(eff[pid]["style"], "other") for pid in labelled}
    counts = Counter(fam.values())
    main, main_n = counts.most_common(1)[0] if counts else (None, 0)
    # Only a playlist with one clear sound has misfits; a deliberately mixed one doesn't.
    misfit = {f for f, n in counts.items()
              if f != main and f not in COMPATIBLE.get(main, set()) and n / len(labelled) < rare} \
        if labelled and main_n / len(labelled) >= dominant else set()

    groups = OrderedDict((b, []) for b in BUCKETS)
    family_groups, left_out = OrderedDict(), []
    for pid in labelled:
        if fam[pid] in misfit:
            family_groups.setdefault(fam[pid], []).append(pid)
        else:
            groups[bucket(eff[pid])].append(pid)
    parts = OrderedDict()
    for f, ids in family_groups.items():
        for k, part in enumerate(coherent_parts(ids, eff)):
            if len(part) >= min_family:
                parts[f if k == 0 else f"{f}-{k}"] = (f, part)
            else:
                left_out += part

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

    def add_group(ref, ids, name_bucket, label, about):
        ids.sort(key=order.get)
        prof = names.profile(ids, tracks, eff)
        opts = names.options(prof, name_bucket, avoid=used, seed=ref, preferred=names.preferred(ref))
        name = opts[0] if opts else f"{source} · {label}"
        used.add(name)
        ops.append({"op": "create_playlist", "name": name, "ref": ref, "bucket": name_bucket, "nameProfile": prof,
                    "nameOptions": opts, "reason": f"{label} split of {source!r}: {about}"})
        ops.append({"op": "add_tracks", "playlist": {"ref": ref, "name": name},
                    "tracks": [{**track_ref(tracks[i]), "note": note(eff[i])} for i in ids],
                    "reason": f"{len(ids)} of {len(order)} tracks from {source!r}; the original stays as it is"})

    for b, ids in groups.items():
        if ids:
            add_group(f"{source}-{b}", ids, b, b, ABOUT[b])
    for key, (f, ids) in parts.items():
        vibe = Counter(bucket(eff[i]) for i in ids).most_common(1)[0][0]
        add_group(f"{source}-{key}", ids, vibe, FAMILY_LABEL.get(f, f),
                  f"{FAMILY_LABEL.get(f, f).lower()} songs that don't blend with the rest")
    if unlabelled:
        ops.append({"op": "create_playlist", "name": f"{source} · Unsorted", "ref": f"{source}-unsorted",
                    "reason": "tracks with no labels yet"})
        ops.append({"op": "add_tracks", "playlist": {"ref": f"{source}-unsorted", "name": f"{source} · Unsorted"},
                    "tracks": [track_ref(tracks[i]) for i in unlabelled],
                    "reason": "label these, then re-run split"})
    return ops, [track_ref(tracks[i]) for i in sorted(left_out, key=order.get)]
