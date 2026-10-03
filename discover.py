"""Find songs you don't have yet that fit your taste, from lesser-known artists.

Read-only: nothing here touches the Music library.

Two modes:
  trending (default)  Songs charting right now, old or new: Apple's daily
                      per-genre top songs for your storefronts plus the weekly
                      most-listened recordings on ListenBrainz.
  fresh               Releases from the last N days by artists related to yours.

Scoring = taste x heat x nicheness:
  taste     Artists you play (weighted by plays, favourites, recent listening),
            artists Deezer lists as related to them, and your genre mix.
  heat      Chart position, number of charts, ListenBrainz weekly listens, and
            chart climb since the last run (needs at least one earlier run).
  niche     Artist's Deezer fans relative to the artists you usually play in
            the same genre. Relative, because Deezer isn't available in India
            and Indian artists' absolute counts there are far too low.

Network: sends artist names from your library to api.deezer.com and
itunes.apple.com, and fetches public ListenBrainz stats. No account, key or
personal ID is sent. Responses are cached in data/cache/, chart snapshots are
kept in data/charts/ to measure climbs between runs.

Apple's chart feeds are iTunes Store charts, not Apple Music streaming charts:
the streaming charts need an Apple Developer token.
"""
import hashlib
import json
import math
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from suggest import credit_names, norm, song_key

DATA = Path(__file__).parent / "data"
CACHE = DATA / "cache" / "http"
CHARTS = DATA / "charts"
OUT = DATA / "discover"

HOUR, DAY = 3600, 86400
_last_call = defaultdict(float)
MIN_INTERVAL = {"itunes.apple.com": 3.2, "api.deezer.com": 0.15, "api.listenbrainz.org": 1.0,
                "musicbrainz.org": 1.1, "ws.audioscrobbler.com": 0.25}


class FetchError(RuntimeError):
    pass


def _cache_file(url):
    return CACHE / f"{hashlib.sha1(url.encode()).hexdigest()}.json"


def get_json(url, ttl):
    """GET a JSON URL with per-host pacing, retries and an on-disk cache (one file per URL)."""
    path = _cache_file(url)
    if path.exists() and time.time() - path.stat().st_mtime < ttl:
        try:
            return json.loads(path.read_text())
        except ValueError:
            pass  # partial write from an interrupted run; fetch again
    host = urllib.parse.urlparse(url).hostname
    for attempt in range(4):
        wait = MIN_INTERVAL.get(host, 0.5) - (time.time() - _last_call[host])
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": "apple-music-organizer/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.loads(r.read().decode())
            break
        except urllib.error.HTTPError as e:
            if e.code not in (403, 429, 500, 502, 503) or attempt == 3:
                raise FetchError(f"{url}: HTTP {e.code}")
            time.sleep(15 * (attempt + 1))  # Apple answers 403 when rate limited
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            if attempt == 3:
                raise FetchError(f"{url}: {e}")
            time.sleep(5)
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(body))
    tmp.replace(path)
    return body


def quiet(progress, fn, *args, default=None):
    try:
        return fn(*args)
    except FetchError as e:
        progress(f"  skipped: {e}")
        return default


# --- Taste -------------------------------------------------------------------

def track_weight(t, now):
    plays = t["playedCount"] or 0
    w = 1 + math.log1p(plays) + (2 if t["favorited"] else 0)
    if t["playedDate"]:
        age = (now - datetime.fromisoformat(t["playedDate"].replace("Z", "+00:00"))).days
        w += max(0, 1 - age / 90)  # listened to in the last three months
    return w


def taste_profile(lib, now):
    """Artists and genres you like, weighted by how you listen. Disliked songs don't count."""
    artists, genres = {}, Counter()
    for t in lib["tracks"]:
        if t["disliked"]:
            continue
        w = track_weight(t, now)
        if t["genre"]:
            genres[t["genre"]] += w
        for name in credit_names(t):
            key = norm(name)
            if not key or key == "various artists":
                continue
            a = artists.setdefault(key, {"name": name, "weight": 0.0, "plays": 0, "tracks": 0,
                                         "genres": Counter()})
            a["weight"] += w
            a["plays"] += t["playedCount"] or 0
            a["tracks"] += 1
            if t["genre"]:
                a["genres"][t["genre"]] += w
    total = sum(genres.values()) or 1
    return {"artists": artists, "genres": {g: w / total for g, w in genres.most_common()}}


def top_artists(taste, n):
    return sorted(taste["artists"].values(), key=lambda a: -a["weight"])[:n]


# --- Deezer: related artists and fan counts ------------------------------------

def deezer_artist(name):
    q = urllib.parse.quote(name)
    res = get_json(f"https://api.deezer.com/search/artist?q={q}&limit=5", 30 * DAY).get("data", [])
    return next((a for a in res if norm(a["name"]) == norm(name)), None)


def related_graph(taste, seeds, progress):
    """Related artists of your top artists, scored 0..1, plus your artists' fan counts."""
    top = top_artists(taste, seeds)
    max_w = top[0]["weight"] if top else 1
    related, seed_fans = {}, {}
    for i, seed in enumerate(top, 1):
        progress(f"related artists {i}/{len(top)}: {seed['name']}")
        d = quiet(progress, deezer_artist, seed["name"])
        if not d:
            continue
        seed_fans[norm(seed["name"])] = d.get("nb_fan") or 0
        url = f"https://api.deezer.com/artist/{d['id']}/related?limit=20"
        rel = (quiet(progress, get_json, url, 14 * DAY, default={}) or {}).get("data", [])
        for pos, r in enumerate(rel):
            key = norm(r["name"])
            if key in taste["artists"]:
                continue
            c = related.setdefault(key, {"name": r["name"], "fans": r.get("nb_fan") or 0,
                                         "score": 0.0, "because": []})
            c["score"] += (seed["weight"] / max_w) * (1 - pos / max(len(rel), 1))
            c["because"].append(seed["name"])
    top_score = max((c["score"] for c in related.values()), default=1)
    for c in related.values():
        c["score"] /= top_score
    return related, seed_fans


def fan_baselines(taste, seed_fans):
    """Median Deezer fans of the artists you play, per genre and overall."""
    by_genre = defaultdict(list)
    for key, fans in seed_fans.items():
        if fans:
            by_genre[taste["artists"][key]["genres"].most_common(1)[0][0]].append(fans)
    overall = statistics.median([f for f in seed_fans.values() if f] or [100_000])
    return {g: statistics.median(v) for g, v in by_genre.items() if len(v) >= 2}, overall


def niche_ratio(fans, genre, baselines):
    per_genre, overall = baselines
    return fans / per_genre.get(genre, overall)


# --- Trending sources --------------------------------------------------------

def genre_ids():
    """Apple genre name -> id for every music genre and subgenre."""
    tree = get_json("https://itunes.apple.com/WebObjects/MZStoreServices.woa/ws/genres?id=34", 30 * DAY)["34"]
    ids = {}

    def walk(g):
        ids.setdefault(g["name"], g["id"])
        for s in (g.get("subgenres") or {}).values():
            walk(s)
    walk(tree)
    for g in ("1262", "1266"):  # Indian and Regional Indian list their subgenres separately
        sub = get_json(f"https://itunes.apple.com/WebObjects/MZStoreServices.woa/ws/genres?id={g}", 30 * DAY)
        walk(sub[g])
    return ids


def apple_chart(country, genre_id):
    url = f"https://itunes.apple.com/{country}/rss/topsongs/limit=100/genre={genre_id}/json"
    entries = get_json(url, 12 * HOUR).get("feed", {}).get("entry", [])
    entries = [entries] if isinstance(entries, dict) else entries
    songs = []
    for pos, e in enumerate(entries, 1):
        links = e.get("link", [])
        links = [links] if isinstance(links, dict) else links
        href = next((l["attributes"]["href"] for l in links if l["attributes"].get("type") == "text/html"), "")
        songs.append({
            "name": e["im:name"]["label"], "artist": e["im:artist"]["label"],
            "released": e.get("im:releaseDate", {}).get("label", "")[:10],
            "genre": e.get("category", {}).get("attributes", {}).get("label"),
            "url": href.replace("&uo=2", "").replace("?uo=2", ""), "pos": pos, "of": len(entries),
        })
    return songs


def snapshot_climbs(feed, songs):
    """Save today's positions; return {song key: places climbed since the last earlier snapshot}."""
    CHARTS.mkdir(parents=True, exist_ok=True)
    today = datetime.now().date().isoformat()
    older = sorted(p for p in CHARTS.glob(f"{feed}--*.json") if p.stem.split("--")[1] < today)
    (CHARTS / f"{feed}--{today}.json").write_text(json.dumps(
        {"|".join(song_key(s)): s["pos"] for s in songs}, ensure_ascii=False))
    if not older:
        return None
    prev = json.loads(older[-1].read_text())
    return {k: prev[k] - s["pos"] if k in prev else "new"
            for s in songs for k in ["|".join(song_key(s))]}


def listenbrainz_week():
    url = "https://api.listenbrainz.org/1/stats/sitewide/recordings?range=week&count=1000"
    recs = get_json(url, 12 * HOUR).get("payload", {}).get("recordings", [])
    return [{"name": r["track_name"], "artist": r["artist_name"], "listens": r["listen_count"]} for r in recs]


def apple_song(name, artist, country):
    q = urllib.parse.quote(f"{artist} {name}")
    res = get_json(f"https://itunes.apple.com/search?term={q}&entity=song&country={country}&limit=5",
                   30 * DAY).get("results", [])
    want = song_key({"name": name, "artist": artist})
    return next((r for r in res if song_key({"name": r["trackName"], "artist": r["artistName"]}) == want), None)


# --- Trending mode -----------------------------------------------------------

def trending(lib, countries=("in", "us"), genres=8, seeds=30, max_ratio=2.0, limit=30, progress=print,
             owned=None):
    """`lib` defines the taste; `owned` (default: lib's tracks) is what never gets suggested."""
    now = datetime.now(timezone.utc)
    taste = taste_profile(lib, now)
    related, seed_fans = related_graph(taste, seeds, progress)
    baselines = fan_baselines(taste, seed_fans)
    owned = {song_key(t) for t in (owned or lib["tracks"])}
    max_w = top_artists(taste, 1)[0]["weight"]

    # Gather chart songs, merging the same song across charts.
    pool = {}
    ids = quiet(progress, genre_ids, default={}) or {}
    feeds = [(c, g) for g in list(taste["genres"])[:genres] if g in ids for c in countries]
    for i, (country, g) in enumerate(feeds, 1):
        progress(f"charts {i}/{len(feeds)}: {g} ({country.upper()})")
        songs = quiet(progress, apple_chart, country, ids[g], default=[]) or []
        climbs = snapshot_climbs(f"{country}-{ids[g]}", songs) if songs else None
        for s in songs:
            k = song_key(s)
            p = pool.setdefault(k, {**s, "charts": [], "listens": 0, "climb": None})
            p["charts"].append(f"{g} {country.upper()} #{s['pos']}")
            p["heat"] = max(p.get("heat", 0), 1 - (s["pos"] - 1) / s["of"])
            c = climbs and climbs.get("|".join(k))
            if c is not None and (p["climb"] is None or c == "new" or (p["climb"] != "new" and c > p["climb"])):
                p["climb"] = c
    progress("ListenBrainz weekly listens")
    lb = quiet(progress, listenbrainz_week, default=[]) or []
    max_listens = max((r["listens"] for r in lb), default=1)
    for r in lb:
        p = pool.setdefault(song_key(r), {**r, "charts": [], "listens": 0, "climb": None, "heat": 0,
                                          "url": "", "released": "", "genre": None})
        p["listens"] = r["listens"]

    # Score taste first (offline), so only plausible picks cost network lookups.
    scored = []
    for k, p in pool.items():
        if k in owned:
            continue
        names = credit_names(p)
        known = max((taste["artists"][norm(n)]["weight"] / max_w for n in names if norm(n) in taste["artists"]),
                    default=0)
        rel = max((related[norm(n)]["score"] for n in names if norm(n) in related), default=0)
        genre_fit = taste["genres"].get(p["genre"], 0)
        t = max(0.8 * known, rel) + 2 * genre_fit
        if t < 0.15:
            continue
        p["taste"] = round(t, 3)
        p["known"] = known > 0
        p["because"] = (related[norm(n)]["because"][:3] for n in names if norm(n) in related)
        p["because"] = next(p["because"], [])
        scored.append(p)
    scored.sort(key=lambda p: -p["taste"])

    picks = []
    for i, p in enumerate(scored[:limit * 4], 1):
        primary = credit_names(p)[0]
        rel = related.get(norm(primary))
        fans = rel["fans"] if rel else None
        if fans is None:
            progress(f"fan counts {i}: {primary}")
            d = quiet(progress, deezer_artist, primary)
            fans = d.get("nb_fan") if d else None
        if fans:
            ratio = niche_ratio(fans, p["genre"], baselines)
            if ratio > max_ratio:
                continue
            niche = 1 / (1 + ratio)
        else:
            ratio, niche = None, 0.4  # unknown on Deezer: probably small, but unproven
        lb_heat = math.log1p(p["listens"]) / math.log1p(max_listens) if p["listens"] else 0
        climb = 0.3 if p["climb"] == "new" else (min(p["climb"], 30) / 100 if isinstance(p["climb"], int) and p["climb"] > 0 else 0)
        heat = p["heat"] + 0.15 * (len(p["charts"]) - 1) + lb_heat + climb
        picks.append({**p, "fans": fans, "nicheRatio": ratio and round(ratio, 3),
                      "heatScore": round(heat, 3),
                      "score": round(p["taste"] * heat * niche * (0.8 if p["known"] else 1), 4)})
    picks.sort(key=lambda p: -p["score"])
    picks = vary(picks, limit)

    for p in picks:  # ListenBrainz-only songs need an Apple Music link
        if not p["url"]:
            hit = quiet(progress, apple_song, p["name"], p["artist"], countries[0])
            if hit:
                p.update(url=hit["trackViewUrl"].split("&uo")[0].split("?uo")[0],
                         released=hit.get("releaseDate", "")[:10], genre=hit.get("primaryGenreName"))
    return {
        "mode": "trending", "generatedAt": now.isoformat(timespec="seconds"),
        "inventoryScannedAt": lib.get("scannedAt"),
        "params": {"countries": list(countries), "genres": [g for _, g in feeds][::len(countries)],
                   "seeds": seeds, "maxRatio": max_ratio},
        "firstRun": all(p["climb"] is None for p in pool.values()),
        "picks": picks,
    }


def vary(picks, limit, per_artist=2):
    count, out = Counter(), []
    for p in picks:
        a = norm(credit_names(p)[0])
        if count[a] < per_artist:
            count[a] += 1
            out.append(p)
    return out[:limit]


# --- Fresh mode ----------------------------------------------------------------

def apple_artist(name, country):
    q = urllib.parse.quote(name)
    res = get_json(f"https://itunes.apple.com/search?term={q}&entity=musicArtist&country={country}&limit=5",
                   30 * DAY).get("results", [])
    return next((a for a in res if norm(a["artistName"]) == norm(name)), None)


def recent_releases(artist_id, country, days, now):
    url = f"https://itunes.apple.com/lookup?id={artist_id}&entity=album&sort=recent&limit=10&country={country}"
    out = []
    for r in get_json(url, DAY).get("results", []):
        if r.get("wrapperType") != "collection" or not r.get("releaseDate"):
            continue
        released = datetime.fromisoformat(r["releaseDate"].replace("Z", "+00:00"))
        age = (now - released).days
        if 0 <= age <= days:
            out.append({
                "name": r["collectionName"], "artist": r["artistName"],
                "released": released.date().isoformat(), "ageDays": age,
                "tracks": r.get("trackCount"), "genre": r.get("primaryGenreName"),
                "url": r.get("collectionViewUrl", "").split("?")[0],
            })
    return out


def fresh(lib, country="in", days=90, seeds=30, artists=40, max_ratio=2.0, limit=30, progress=print):
    now = datetime.now(timezone.utc)
    taste = taste_profile(lib, now)
    related, seed_fans = related_graph(taste, seeds, progress)
    baselines = fan_baselines(taste, seed_fans)
    owned = {song_key(t) for t in lib["tracks"]} | {(norm(t["album"]), norm(t["artist"])) for t in lib["tracks"]}

    cands = [c for c in related.values() if c["fans"] >= 1_000]  # tiny counts are usually name clashes
    cands.sort(key=lambda c: -c["score"])
    picks, quiet_artists = [], []
    for i, c in enumerate(cands[:artists], 1):
        progress(f"new releases {i}/{min(artists, len(cands))}: {c['name']}")
        a = quiet(progress, apple_artist, c["name"], country)
        rels = a and quiet(progress, recent_releases, a["artistId"], country, days, now, default=[])
        if not rels:
            quiet_artists.append(c["name"])
            continue
        for r in rels:
            if len(credit_names(r)) > 4 and (r["tracks"] or 0) > 8:
                continue  # label compilations like "Bollywood New Love Hits"
            title = norm(r["name"].replace(" - Single", "").replace(" - EP", ""))
            if (title, norm(r["artist"])) in owned:
                continue
            ratio = niche_ratio(c["fans"], r["genre"], baselines)
            if ratio > max_ratio:
                continue
            recency = 1 - r["ageDays"] / (days * 1.5)
            picks.append({**r, "fans": c["fans"], "nicheRatio": round(ratio, 3), "because": c["because"][:3],
                          "known": False, "charts": [],
                          "score": round(c["score"] * recency / (1 + ratio), 4)})
    picks.sort(key=lambda p: -p["score"])
    return {
        "mode": "fresh", "generatedAt": now.isoformat(timespec="seconds"),
        "inventoryScannedAt": lib.get("scannedAt"),
        "params": {"country": country, "days": days, "seeds": seeds, "artists": artists, "maxRatio": max_ratio},
        "quietArtists": quiet_artists,
        "picks": vary(picks, limit),
    }


# --- Output ------------------------------------------------------------------

def save(result):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{result['mode']}-{result['generatedAt'][:19].replace(':', '')}.json"
    path.write_text(json.dumps(result, indent=1, ensure_ascii=False))
    return path


def print_result(result):
    if result["mode"] == "trending":
        p = result["params"]
        print(f"\nCHARTING NOW, NEAR YOUR TASTE, LESS KNOWN THAN YOUR USUAL ARTISTS\n"
              f"  charts: {', '.join(p['genres'])} in {', '.join(c.upper() for c in p['countries'])}"
              f" + ListenBrainz weekly listens")
        if result["firstRun"]:
            print("  (first run: chart climbs show up from the next day's run)")
    else:
        print(f"\nNEW RELEASES FROM ARTISTS RELATED TO YOURS (last {result['params']['days']} days)")
    print()
    if not result["picks"]:
        print("  Nothing found. Try a higher --max-ratio.")
    for i, r in enumerate(result["picks"], 1):
        bits = [r.get("released") or "?", r.get("genre") or "?"]
        if r.get("fans") is not None:
            bits.append(f"{r['fans']:,} Deezer fans ({r['nicheRatio']:.2f}x your usual)")
        bits.append("artist you already play" if r["known"] else
                    f"related to {', '.join(r['because'])}" if r.get("because") else "fits your genres")
        print(f"{i:>3}. {r['name']} - {r['artist']}")
        print(f"     {' · '.join(bits)}")
        heat = list(r.get("charts", []))[:3]
        if r.get("listens"):
            heat.append(f"{r['listens']:,} ListenBrainz listens this week")
        if r.get("climb") == "new":
            heat.append("new on chart")
        elif isinstance(r.get("climb"), int) and r["climb"] > 0:
            heat.append(f"up {r['climb']}")
        if heat:
            print(f"     {' · '.join(heat)}")
        print(f"     {r.get('url') or '(no Apple Music link found)'}")
    if result.get("quietArtists"):
        print(f"\nAlso related to your artists, nothing new in this window: {', '.join(result['quietArtists'][:15])}")
