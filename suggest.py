"""Suggestion generators. Each returns a list of plan ops; nothing here touches Music.

Only metadata from the scan is used (artist credits, genre). Every op carries a
reason that says which data it was based on.
"""
import re
import unicodedata
from collections import Counter, defaultdict

from plans import track_ref

ARTIST_SHARE = 0.8   # a playlist is an "artist playlist" if this share credits one artist
GENRE_SHARE = 0.6    # a playlist is a genre's home if this share of it is that genre
MIN_TRACKS = 3


def norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c)).replace("’", "'")
    s = re.sub(r"\s*[\(\[](feat|ft|with|from|remaster|deluxe)[^\)\]]*[\)\]]", "", s)
    s = re.sub(r"[^\w\s$]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def credit_names(t):
    """Artist names credited on a track, including features in the title."""
    names = re.split(r",\s*|\s+&\s+|\s+x\s+|\s+and\s+", t["artist"] or "")
    for m in re.finditer(r"[\(\[](?:feat\.?|ft\.?|with)\s+([^\)\]]+)[\)\]]", t["name"] or "", re.I):
        names += re.split(r",\s*|\s+&\s+", m.group(1))
    return [n.strip() for n in names if n.strip()]


def credited(t):
    return {norm(n) for n in credit_names(t)}


def song_key(t):
    """Same song regardless of edition: normalized title + first credited artist."""
    first = re.split(r",\s*|\s+&\s+", t["artist"] or "")[0]
    return norm(t["name"]), norm(first)


def editable(p):
    return not p["smart"] and p["specialKind"] == "none"


def playlist_artist(p, tracks, all_artists):
    ts = [tracks[i] for i in p["trackIDs"] if i in tracks]
    if not ts:
        name = norm(p["name"])
        return (name, "playlist is named after this artist") if name in all_artists else (None, None)
    if len(ts) < MIN_TRACKS:
        return None, None
    counts = Counter(a for t in ts for a in credited(t))
    artist, n = counts.most_common(1)[0]
    if n / len(ts) >= ARTIST_SHARE:
        return artist, f"{n} of {len(ts)} tracks credit this artist"
    return None, None


def best_edition(group):
    return max(group, key=lambda t: (t["inLibrary"], t["playedCount"] or 0))


def artist_gaps(lib):
    """Artist playlists missing songs by that artist that you already have."""
    tracks = {t["persistentID"]: t for t in lib["tracks"]}
    display = {norm(n): n for t in tracks.values() for n in credit_names(t)}
    all_artists = set(display)
    ops = []
    for p in filter(editable, lib["playlists"]):
        artist, why = playlist_artist(p, tracks, all_artists)
        if not artist:
            continue
        have = set(p["trackIDs"])
        have_songs = {song_key(tracks[i]) for i in have if i in tracks}
        cands = defaultdict(list)
        for t in tracks.values():
            if t["persistentID"] in have or t["disliked"] or artist not in credited(t):
                continue
            if song_key(t) not in have_songs:
                cands[song_key(t)].append(t)
        picks = sorted((best_edition(g) for g in cands.values()), key=lambda t: (t["album"] or "", t["name"]))
        if picks:
            name = display[artist]
            ops.append({
                "op": "add_tracks",
                "playlist": {"id": p["persistentID"], "name": p["name"]},
                "tracks": [track_ref(t) for t in picks],
                "reason": f"Artist playlist for {name} ({why}); these songs crediting "
                          f"{name} are in your library or other playlists but not here",
            })
    return ops


def genre_homes(lib):
    """Library tracks in no playlist, matched to the one playlist built around their genre."""
    tracks = {t["persistentID"]: t for t in lib["tracks"]}
    all_artists = {a for t in tracks.values() for a in credited(t)}
    playlists = [p for p in lib["playlists"] if editable(p)]
    in_any = {i for p in playlists for i in p["trackIDs"]}

    homes = {}  # genre -> (playlist, count, share)
    for p in playlists:
        if playlist_artist(p, tracks, all_artists)[0]:
            continue  # artist playlists aren't genre buckets
        ts = [tracks[i] for i in p["trackIDs"] if i in tracks]
        if len(ts) < 5:
            continue
        for g, n in Counter(t["genre"] for t in ts if t["genre"]).items():
            share = n / len(ts)
            if share >= GENRE_SHARE and n > homes.get(g, (None, 0))[1]:
                homes[g] = (p, n, share)

    orphans = defaultdict(lambda: defaultdict(list))  # genre -> song -> editions
    for t in tracks.values():
        if t["inLibrary"] and t["persistentID"] not in in_any and not t["disliked"] and t["genre"] in homes:
            orphans[t["genre"]][song_key(t)].append(t)

    ops = []
    for g, songs in sorted(orphans.items()):
        p, n, share = homes[g]
        have_songs = {song_key(tracks[i]) for i in p["trackIDs"] if i in tracks}
        ts = sorted((best_edition(e) for k, e in songs.items() if k not in have_songs),
                    key=lambda t: (t["artist"] or "", t["name"]))
        if not ts:
            continue
        ops.append({
            "op": "add_tracks",
            "playlist": {"id": p["persistentID"], "name": p["name"]},
            "tracks": [track_ref(t) for t in ts],
            "reason": f"These {g} tracks are in no playlist; {p['name'].strip()!r} is "
                      f"{share:.0%} {g} ({n} of its tracks)",
        })
    return ops


GENERATORS = {"artist-gaps": artist_gaps, "genre-homes": genre_homes}
