"""Last.fm crowd tags per song ("chill", "hype", "sad", "workout"...): listeners' opinions,
independent of the AI labels. Needs a free API key in .env as LASTFM_API_KEY=...

Stored in data/metadata/lastfm.json keyed by track id: the song's top tags with their
weights (0-100), or the artist's top tags when the song itself has none.
"""
import os
import urllib.parse
from pathlib import Path

import metadata
from discover import FetchError, get_json
from suggest import credit_names

ROOT = Path(__file__).parent
DAY = 86400


def api_key():
    if os.environ.get("LASTFM_API_KEY"):
        return os.environ["LASTFM_API_KEY"].strip()
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("LASTFM_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def call(method, key, **params):
    q = urllib.parse.urlencode({"method": method, "api_key": key, "format": "json", "autocorrect": 1, **params})
    body = get_json(f"https://ws.audioscrobbler.com/2.0/?{q}", 30 * DAY)
    if "error" in body:
        raise FetchError(f"Last.fm: {body.get('message')}")
    return body


def tags_of(body, key="toptags"):
    tags = (body.get(key) or {}).get("tag") or []
    tags = [tags] if isinstance(tags, dict) else tags
    return [(t["name"].lower(), int(t.get("count", 0))) for t in tags if int(t.get("count", 0)) > 0][:15]


def enrich(lib, progress=print, save_every=25):
    key = api_key()
    if not key:
        raise SystemExit("No Last.fm key. Put LASTFM_API_KEY=your_key in the .env file in the project folder.")
    data = metadata.load("lastfm")
    todo = [t for t in lib["tracks"] if t["persistentID"] not in data]
    for i, t in enumerate(todo, 1):
        artist = credit_names(t)[0] if credit_names(t) else t["artist"]
        progress(f"last.fm {i}/{len(todo)}: {t['name']} - {artist}")
        try:
            tags = tags_of(call("track.getTopTags", key, artist=artist, track=metadata.strip_title(t["name"])))
            source = "track"
            if not tags:
                tags = tags_of(call("artist.getTopTags", key, artist=artist))
                source = "artist"
            data[t["persistentID"]] = {"tags": tags, "from": source}
        except FetchError as e:
            if "Invalid API key" in str(e):
                raise SystemExit("Last.fm says the API key is invalid; check LASTFM_API_KEY in .env.")
            data[t["persistentID"]] = {"tags": [], "from": None, "error": str(e)[:120]}
        if i % save_every == 0:
            metadata.store("lastfm", data)
    metadata.store("lastfm", data)
    return data
