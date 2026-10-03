"""Track metadata beyond what Music stores, each source kept separate with its provenance.

  data/metadata/apple.json   Apple catalog match (iTunes Search API): catalog IDs,
                             explicitness, catalog genre, 30-second preview URL.
  data/metadata/musicbrainz.json   Per artist (keyed by normalized name): type, country,
                             gender, active since, description and crowd tags.
  data/metadata/audio.json   Measured from the public 30-second preview: tempo, key,
                             loudness, energy, brightness, beat strength, bass weight.
                             Previews are short and from one part of the song, so
                             treat these as estimates, never as Music's own values.

Read-only with respect to the Music library. Uses NumPy/SciPy (already installed)
and macOS's built-in afconvert to decode previews; nothing is installed.
Both stores are resumable: rerunning skips tracks that are already done.
"""
import json
import re
import subprocess
import tempfile
import warnings
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from discover import FetchError, get_json
from suggest import credit_names, norm, song_key

DATA = Path(__file__).parent / "data"
META = DATA / "metadata"
PREVIEWS = DATA / "cache" / "previews"
DAY = 86400


def load(name):
    p = META / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def store(name, data):
    META.mkdir(parents=True, exist_ok=True)
    (META / f"{name}.json").write_text(json.dumps(data, indent=0, ensure_ascii=False))


# --- Apple catalog match ---------------------------------------------------------

def strip_title(name):
    """Title without (feat. ...)/(with ...) so search isn't thrown off by credits."""
    return re.sub(r"\s*[\(\[](feat|ft|with)[^\)\]]*[\)\]]", "", name or "", flags=re.I).strip()


def match_apple(t, country):
    """Best catalog match for a track, or None. Same song is required; album and length decide editions."""
    artist = credit_names(t)[0] if credit_names(t) else (t["artist"] or "")
    want = song_key(t)
    best, best_score = None, 0
    for term in (f"{artist} {strip_title(t['name'])}", f"{strip_title(t['name'])} {t['album'] or ''}"):
        url = ("https://itunes.apple.com/search?" +
               urllib.parse.urlencode({"term": term, "entity": "song", "country": country, "limit": 15}))
        for r in get_json(url, 90 * DAY).get("results", []):
            if song_key({"name": r.get("trackName"), "artist": r.get("artistName")}) != want:
                continue
            score = 1
            if norm(r.get("collectionName")) == norm(t["album"]):
                score += 2
            if t["duration"] and r.get("trackTimeMillis") and abs(r["trackTimeMillis"] / 1000 - t["duration"]) < 3:
                score += 2
            if r.get("trackExplicitness") == "explicit":
                score += 0.5  # library copies are usually the explicit edition when one exists
            if score > best_score:
                best, best_score = r, score
        if best_score >= 3:
            break
    if not best:
        return None
    return {
        "trackId": best["trackId"], "artistId": best.get("artistId"), "collectionId": best.get("collectionId"),
        "name": best["trackName"], "artist": best["artistName"], "album": best.get("collectionName"),
        # Explicitness of the matched catalog edition, which may differ from your copy.
        "genre": best.get("primaryGenreName"), "explicit": best.get("trackExplicitness") == "explicit",
        "released": (best.get("releaseDate") or "")[:10], "durationMs": best.get("trackTimeMillis"),
        "previewUrl": best.get("previewUrl"), "url": (best.get("trackViewUrl") or "").split("&uo")[0],
        "matchScore": best_score,  # 5.5 = same song, album and length; 1 = same song only
    }


def enrich_apple(lib, country="in", progress=print, save_every=10):
    data = load("apple")
    todo = [t for t in lib["tracks"] if t["persistentID"] not in data]
    for i, t in enumerate(todo, 1):
        progress(f"apple catalog {i}/{len(todo)}: {t['name']} - {t['artist']}")
        try:
            data[t["persistentID"]] = match_apple(t, country) or {"miss": True}
        except FetchError as e:
            progress(f"  skipped for now: {e}")
            continue
        if i % save_every == 0:
            store("apple", data)
    store("apple", data)
    return data


# --- Audio features from previews -------------------------------------------------

SR, N_FFT, HOP = 22050, 2048, 512


def decode(url, track_id):
    """Download a preview once and decode it to mono 22.05 kHz samples in [-1, 1]."""
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    m4a = PREVIEWS / f"{track_id}.m4a"
    if not m4a.exists():
        req = urllib.request.Request(url, headers={"User-Agent": "apple-music-organizer/0.1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            m4a.write_bytes(r.read())
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{SR}", "-c", "1", str(m4a), str(wav)],
                       check=True, capture_output=True)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", wavfile.WavFileWarning)  # afconvert adds an extra chunk
            sr, x = wavfile.read(wav)
    return x.astype(np.float32) / 32768.0


def analyze(x):
    frames = np.lib.stride_tricks.sliding_window_view(x, N_FFT)[::HOP] * np.hanning(N_FFT)
    mag = np.abs(np.fft.rfft(frames, axis=1))
    freqs = np.fft.rfftfreq(N_FFT, 1 / SR)
    power = mag ** 2
    total = power.sum(axis=1) + 1e-12

    rms = np.sqrt((frames ** 2).mean(axis=1)) + 1e-9
    rms_db = 20 * np.log10(rms)
    loud = rms_db > rms_db.max() - 40  # ignore silence when averaging

    # Onset strength: positive spectral flux on a log-magnitude spectrum.
    logmag = np.log1p(100 * mag)
    raw_flux = np.maximum(0, np.diff(logmag, axis=0)).sum(axis=1)
    flux = (raw_flux - raw_flux.mean()) / (raw_flux.std() + 1e-9)

    # Tempo: autocorrelation of onset strength, 60-200 BPM, gently preferring ~120.
    ac = np.correlate(flux, flux, mode="full")[len(flux) - 1:]
    ac /= ac[0] + 1e-9
    fps = SR / HOP
    bpms = np.arange(60, 201)
    lags = fps * 60 / bpms
    strength = np.interp(lags, np.arange(len(ac)), ac)
    prior = np.exp(-0.5 * (np.log2(bpms / 120) / 0.9) ** 2)
    i = int(np.argmax(strength * prior))
    tempo, beat_strength = int(bpms[i]), float(strength[i])

    centroid = (mag * freqs).sum(axis=1) / (mag.sum(axis=1) + 1e-9)
    mid = flux[1:-1]
    onset_peaks = (mid > flux[:-2]) & (mid > flux[2:]) & (mid > 1)
    flatness = np.exp(np.log(mag + 1e-9).mean(axis=1)) / (mag.mean(axis=1) + 1e-9)

    return {
        "tempo": tempo,  # estimate; can be half or double the felt tempo
        "beatStrength": round(beat_strength, 3),         # how regular and pronounced the pulse is, 0-1
        "loudnessDb": round(float(rms_db[loud].mean()), 1),
        "dynamicsDb": round(float(rms_db[loud].std()), 1),  # low = compressed and steady
        "brightnessHz": int(np.median(centroid[loud])),
        "bassShare": round(float((power[:, freqs < 150].sum(axis=1) / total)[loud].mean()), 3),
        "onsetRate": round(float(onset_peaks.sum() / (len(x) / SR)), 2),  # distinct hits per second
        "noisiness": round(float(np.median(flatness[loud])), 3),
        "highShare": round(float((power[:, freqs > 4000].sum(axis=1) / total)[loud].mean()), 4),  # hats, distortion
        "fluxMean": round(float(np.log1p(raw_flux[loud[1:]].mean())), 3),  # how hard and often the sound changes
    }


def enrich_audio(lib, progress=print, save_every=10, reanalyze=False):
    """Analyse previews; with reanalyze, re-measure every track (previews are cached, so no downloads)."""
    apple, data = load("apple"), load("audio")
    todo = [t for t in lib["tracks"] if (reanalyze or t["persistentID"] not in data)
            and apple.get(t["persistentID"], {}).get("previewUrl")]
    for i, t in enumerate(todo, 1):
        a = apple[t["persistentID"]]
        progress(f"audio {i}/{len(todo)}: {t['name']} - {t['artist']}")
        try:
            data[t["persistentID"]] = {**analyze(decode(a["previewUrl"], a["trackId"])),
                                       "source": "30s preview", "trackId": a["trackId"]}
        except Exception as e:  # noqa: BLE001 - one bad preview shouldn't stop the run
            progress(f"  failed: {e}")
            data[t["persistentID"]] = {"error": str(e)[:200]}
        if i % save_every == 0:
            store("audio", data)
    store("audio", data)
    return data


# --- MusicBrainz artists -------------------------------------------------------

def musicbrainz_artist(name):
    q = urllib.parse.quote(f'artist:"{name}"')
    res = get_json(f"https://musicbrainz.org/ws/2/artist/?query={q}&fmt=json&limit=5", 90 * DAY)
    for a in res.get("artists", []):
        names = {norm(a["name"])} | {norm(x.get("name")) for x in a.get("aliases", [])}
        if a.get("score", 0) >= 90 and norm(name) in names:
            return {
                "mbid": a["id"], "name": a["name"], "type": a.get("type"), "country": a.get("country"),
                "gender": a.get("gender"), "begin": (a.get("life-span") or {}).get("begin"),
                "about": a.get("disambiguation"),
                "tags": [t["name"] for t in sorted(a.get("tags", []), key=lambda t: -t["count"])][:15],
            }
    return None


def enrich_musicbrainz(lib, progress=print, save_every=20):
    data = load("musicbrainz")
    names = {}
    for t in lib["tracks"]:
        for n in credit_names(t):
            names.setdefault(norm(n), n)
    todo = [(k, n) for k, n in names.items() if k and k not in data]
    for i, (k, n) in enumerate(todo, 1):
        progress(f"musicbrainz {i}/{len(todo)}: {n}")
        try:
            data[k] = musicbrainz_artist(n) or {"miss": True, "name": n}
        except FetchError as e:
            progress(f"  skipped for now: {e}")
            continue
        if i % save_every == 0:
            store("musicbrainz", data)
    store("musicbrainz", data)
    return data
