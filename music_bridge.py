"""Talks to the macOS Music app through JavaScript for Automation (osascript).

Everything in this module is read-only unless a function name says otherwise.
"""
import json
import subprocess

TRACK_PROPS = [
    "persistentID", "name", "artist", "albumArtist", "album", "genre", "year",
    "duration", "playedCount", "skippedCount", "favorited", "disliked",
    "dateAdded", "playedDate", "cloudStatus", "trackNumber", "discNumber",
]

READ_LIBRARY_JS = r"""
const m = Application("Music");
const PROPS = %s;

function bulk(collection, prop) {
  try { return collection[prop](); } catch (e) { return null; }
}

function columns(collection) {
  const cols = {};
  for (const p of PROPS) cols[p] = bulk(collection, p);
  const n = cols.persistentID ? cols.persistentID.length : 0;
  const rows = [];
  for (let i = 0; i < n; i++) {
    const r = {};
    for (const p of PROPS) r[p] = cols[p] ? cols[p][i] : null;
    rows.push(r);
  }
  return rows;
}

const lib = m.libraryPlaylists[0];
const tracks = columns(lib.tracks);

const playlists = m.userPlaylists().map(p => {
  const o = { persistentID: p.persistentID(), name: p.name(), smart: false, specialKind: "none" };
  try { o.smart = p.smart(); } catch (e) {}
  try { o.specialKind = p.specialKind(); } catch (e) {}
  o.trackIDs = bulk(p.tracks, "persistentID") || [];
  return o;
});

JSON.stringify({ tracks, playlists });
"""


class MusicError(RuntimeError):
    pass


def run_jxa(script, timeout=600):
    proc = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", script],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise MusicError(proc.stderr.strip() or "osascript failed")
    return proc.stdout


def read_library():
    """Return {"tracks": [...], "playlists": [...]} straight from the Music app."""
    out = run_jxa(READ_LIBRARY_JS % json.dumps(TRACK_PROPS))
    return json.loads(out)
