"""Best songs by one artist that fit your taste, as a playlist plan.

  popularity  Apple's catalog lists an artist's songs roughly by popularity;
              that order is used as the popularity score.
  sound fit   Each candidate's 30-second preview is measured (see metadata.analyze)
              and compared with the songs you play most: close neighbours in
              loudness, brightness, bass, pulse and pace score high. Needs the
              library audio analysis (`enrich audio`); skipped without it.
  genre fit   How much of your listening is in the song's catalog genre.

Songs already in your library or playlists go straight into the new playlist.
Catalog songs you don't have yet can't be added by script (Apple only allows
that through MusicKit with a paid developer account), so they're listed with
Apple Music links; once you add them in Music, `fill` puts them in the playlist.
"""
import math
import urllib.parse
from datetime import datetime, timezone

import numpy as np

import discover
import metadata
from plans import track_ref
from suggest import credited, norm, song_key

FEATURES = ["loudnessDb", "dynamicsDb", "brightnessHz", "bassShare", "beatStrength", "onsetRate", "noisiness",
            "tempo", "highShare", "fluxMean"]


def fold_tempo(t):
    """Fold tempo into 80-160 BPM so half/double-time estimates compare equal."""
    while t and t < 80:
        t *= 2
    while t >= 160:
        t /= 2
    return t


def vector(f):
    """Feature vector for comparisons, or None for an analysis from an older version."""
    if any(k not in f for k in FEATURES):
        return None
    v = [f[k] for k in FEATURES]
    v[FEATURES.index("brightnessHz")] = math.log(max(v[FEATURES.index("brightnessHz")], 1))
    v[FEATURES.index("tempo")] = fold_tempo(v[FEATURES.index("tempo")])
    return v


class SoundTaste:
    """Your listening in audio-feature space: songs weighted by how much you play them."""

    def __init__(self, lib, now):
        audio = metadata.load("audio")
        rows = [(t, audio[t["persistentID"]]) for t in lib["tracks"]
                if vector(audio.get(t["persistentID"], {})) and not t["disliked"]]
        self.ready = len(rows) >= 50
        if not self.ready:
            return
        x = np.array([vector(f) for _, f in rows], dtype=float)
        self.mean, self.std = x.mean(axis=0), x.std(axis=0) + 1e-9
        self.x = (x - self.mean) / self.std
        self.w = np.array([discover.track_weight(t, now) for t, _ in rows])

    def fit(self, features, k=12):
        """0..1: how close a song sounds to the songs you play most."""
        vec = vector(features)
        if vec is None:
            return None
        v = (np.array(vec) - self.mean) / self.std
        d = np.sqrt(((self.x - v) ** 2).sum(axis=1))
        near = np.argsort(d)[:k]
        return float((self.w[near] * np.exp(-d[near] ** 2 / 8)).sum() / self.w[near].sum())


def find_artist(name, country):
    """Exact name match first; otherwise Apple's top artist result."""
    q = urllib.parse.quote(name)
    res = discover.get_json(f"https://itunes.apple.com/search?term={q}&entity=musicArtist&country={country}&limit=5",
                            30 * discover.DAY).get("results", [])
    return next((a for a in res if norm(a["artistName"]) == norm(name)), res[0] if res else None)


def catalog_songs(artist_id, country):
    url = f"https://itunes.apple.com/lookup?id={artist_id}&entity=song&limit=200&country={country}"
    songs, seen = [], set()
    for r in discover.get_json(url, discover.DAY).get("results", []):
        if r.get("wrapperType") != "track" or r.get("kind") != "song":
            continue
        key = song_key({"name": r["trackName"], "artist": r["artistName"]})
        if key in seen:
            continue  # other editions of a song already listed
        seen.add(key)
        songs.append(r)
    return songs


def best_songs(lib, name, country="in", size=20, analyse=40, progress=print):
    now = datetime.now(timezone.utc)
    artist = find_artist(name, country)
    if not artist:
        raise SystemExit(f"No artist called {name!r} in the {country.upper()} Apple Music catalog.")
    songs = catalog_songs(artist["artistId"], country)
    if not songs:
        raise SystemExit(f"Apple returned no songs for {artist['artistName']}.")
    progress(f"{artist['artistName']}: {len(songs)} songs in the catalog")

    taste = discover.taste_profile(lib, now)
    sound = SoundTaste(lib, now)
    owned = {}
    for t in lib["tracks"]:
        owned.setdefault(song_key(t), t)

    picks = []
    for rank, s in enumerate(songs[:analyse]):
        cand = {"name": s["trackName"], "artist": s["artistName"], "album": s.get("collectionName"),
                "genre": s.get("primaryGenreName"), "url": (s.get("trackViewUrl") or "").split("&uo")[0],
                "trackId": s["trackId"], "popularity": 1 - rank / len(songs)}
        mine = owned.get(song_key(cand))
        cand["libraryID"] = mine and mine["persistentID"]
        cand["genreFit"] = taste["genres"].get(cand["genre"], 0) / max(taste["genres"].values() or [1])
        cand["soundFit"] = None
        if sound.ready and s.get("previewUrl"):
            progress(f"listening {rank + 1}/{min(analyse, len(songs))}: {cand['name']}")
            try:
                cand["soundFit"] = sound.fit(metadata.analyze(metadata.decode(s["previewUrl"], s["trackId"])))
            except Exception as e:  # noqa: BLE001 - a broken preview just means no sound score
                progress(f"  preview failed: {e}")
        if mine:  # songs you keep and play get a nudge: you already chose them
            cand["played"] = mine["playedCount"] or 0
        picks.append(cand)

    # Your own songs by this artist are candidates even when they're not in Apple's top list.
    listed = {song_key(p) for p in picks}
    apple, audio = metadata.load("apple"), metadata.load("audio")
    for t in lib["tracks"]:
        if norm(artist["artistName"]) not in credited(t) or song_key(t) in listed or t["disliked"]:
            continue
        listed.add(song_key(t))
        rank = next((i for i, s in enumerate(songs) if song_key({"name": s["trackName"], "artist": s["artistName"]})
                     == song_key(t)), None)
        f = audio.get(t["persistentID"], {})
        picks.append({
            "name": t["name"], "artist": t["artist"], "album": t["album"], "genre": t["genre"],
            "url": apple.get(t["persistentID"], {}).get("url", ""), "trackId": apple.get(t["persistentID"], {}).get("trackId"),
            "popularity": 1 - rank / len(songs) if rank is not None else 0.5, "libraryID": t["persistentID"],
            "genreFit": taste["genres"].get(t["genre"], 0) / max(taste["genres"].values() or [1]),
            "soundFit": sound.fit(f) if sound.ready and f else None,
            "played": t["playedCount"] or 0,
        })

    fits = [p["soundFit"] for p in picks if p["soundFit"] is not None]
    lo, hi = (min(fits), max(fits)) if fits else (0, 1)
    for p in picks:
        sound_part = (p["soundFit"] - lo) / (hi - lo + 1e-9) if p["soundFit"] is not None else None
        p["soundScore"] = sound_part
        parts = [(0.45, p["popularity"]), (0.15, p["genreFit"])]
        if sound_part is not None:
            parts.append((0.4, sound_part))
        score = sum(w * v for w, v in parts) / sum(w for w, _ in parts)
        if p.get("played"):
            score += 0.1 * min(1, math.log1p(p["played"]) / 3)
        p["score"] = round(score, 4)
    picks.sort(key=lambda p: -p["score"])
    # Songs you already keep are the best taste signal: they get up to half the playlist,
    # the strongest catalog songs fill the rest.
    mine = [p for p in picks if p["libraryID"]][:size // 2]
    rest = [p for p in picks if not p["libraryID"]][:size - len(mine)]
    chosen = sorted(mine + rest, key=lambda p: -p["score"])
    return {"artist": artist["artistName"], "artistId": artist["artistId"], "country": country,
            "soundUsed": sound.ready, "picks": chosen}


def plan_ops(lib, result, name=None):
    """create + add ops for owned songs; the rest are returned as `waiting` for later."""
    tracks = {t["persistentID"]: t for t in lib["tracks"]}
    title = name or f"{result['artist']} · For You"
    have = [p for p in result["picks"] if p["libraryID"]]
    waiting = [{k: p[k] for k in ("name", "artist", "album", "url", "trackId")} for p in result["picks"]
               if not p["libraryID"]]
    ops = [{"op": "create_playlist", "name": title, "ref": "artist",
            "reason": f"best {result['artist']} songs for your taste"}]
    if have:
        ops.append({"op": "add_tracks", "playlist": {"ref": "artist", "name": title},
                    "tracks": [{**track_ref(tracks[p["libraryID"]]), "note": why(p)} for p in have],
                    "reason": "already in your library or playlists"})
    return ops, waiting


def why(p):
    bits = [f"popularity {p['popularity']:.2f}"]
    if p.get("soundScore") is not None:
        bits.append(f"sound fit {p['soundScore']:.2f}")
    bits.append(f"genre fit {p['genreFit']:.2f}")
    return ", ".join(bits)


def fill_ops(lib, plan):
    """Add-ops for `waiting` songs of an artist plan that have since appeared in the library."""
    create = next((o for o in plan["ops"] if o["op"] == "create_playlist" and o["status"] == "applied"), None)
    if not create or not plan.get("waiting"):
        return [], plan.get("waiting", [])
    by_key = {}
    for t in lib["tracks"]:
        by_key.setdefault(song_key(t), t)
    found, still = [], []
    for w in plan["waiting"]:
        t = by_key.get(song_key(w))
        (found if t else still).append(t or w)
    ops = []
    if found:
        pid = create["result"]["playlistID"]
        ops.append({"op": "add_tracks", "playlist": {"id": pid, "name": create["name"]},
                    "tracks": [track_ref(t) for t in found],
                    "reason": f"added to your library since {plan['id']}"})
    return ops, still
