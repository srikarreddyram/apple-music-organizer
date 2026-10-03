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
const seen = new Set(tracks.map(t => t.persistentID));
tracks.forEach(t => { t.inLibrary = true; });

// Apple Music songs can sit in a playlist without being added to the library,
// so pick up their metadata from the playlist itself.
const playlists = m.userPlaylists().map(p => {
  const o = { persistentID: p.persistentID(), name: p.name(), smart: false, specialKind: "none" };
  try { o.smart = p.smart(); } catch (e) {}
  try { o.specialKind = p.specialKind(); } catch (e) {}
  const rows = columns(p.tracks);
  o.trackIDs = rows.map(r => r.persistentID);
  for (const r of rows) {
    if (seen.has(r.persistentID)) continue;
    seen.add(r.persistentID);
    r.inLibrary = false;
    tracks.push(r);
  }
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


# --- Live playlist access -------------------------------------------------
# Scripts below take one JSON argument and return JSON. Playlists and tracks
# are always addressed by persistentID, never by name.

JXA_PRELUDE = r"""
const m = Application("Music");

function playlistByID(pid) {
  const found = m.userPlaylists.whose({ persistentID: pid })();
  return found.length ? found[0] : null;
}

function describe(p) {
  const o = { persistentID: p.persistentID(), name: p.name(), smart: false, specialKind: "none" };
  try { o.smart = p.smart(); } catch (e) {}
  try { o.specialKind = p.specialKind(); } catch (e) {}
  try { o.trackIDs = p.tracks.persistentID(); } catch (e) { o.trackIDs = []; }
  return o;
}

// Only plain user playlists may be changed: never smart, special or library playlists.
function editablePlaylist(pid) {
  const p = playlistByID(pid);
  if (!p) throw new Error("playlist " + pid + " not found");
  const d = describe(p);
  if (d.smart || d.specialKind !== "none") throw new Error("playlist " + d.name + " is not an editable user playlist");
  return p;
}

// A track may live only in a playlist (Apple Music song never added to the library).
function trackByID(tid) {
  const inLib = m.libraryPlaylists[0].tracks.whose({ persistentID: tid })();
  if (inLib.length) return inLib[0];
  for (const p of m.userPlaylists()) {
    const hit = p.tracks.whose({ persistentID: tid })();
    if (hit.length) return hit[0];
  }
  return null;
}
"""


def run_jxa_json(body, arg):
    script = JXA_PRELUDE + "\nfunction run(argv) {\n  const a = JSON.parse(argv[0]);\n" + body + "\n}"
    proc = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", script, json.dumps(arg)],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        raise MusicError(proc.stderr.strip() or "osascript failed")
    return json.loads(proc.stdout)


def read_playlist(pid):
    """Current state of one playlist, or None if it no longer exists."""
    return run_jxa_json("""
  const p = playlistByID(a.pid);
  return JSON.stringify(p ? describe(p) : null);
""", {"pid": pid})


def create_playlist(name):
    """WRITES: make a new empty user playlist. Returns its persistentID."""
    return run_jxa_json("""
  const p = m.make({ new: "userPlaylist", withProperties: { name: a.name } });
  return JSON.stringify(p.persistentID());
""", {"name": name})


def add_tracks(pid, track_ids):
    """WRITES: append tracks to a user playlist. Returns {trackID: error or None}."""
    return run_jxa_json("""
  const p = editablePlaylist(a.pid);
  const out = {};
  for (const tid of a.tids) {
    try {
      const t = trackByID(tid);
      if (!t) throw new Error("track not found");
      m.duplicate(t, { to: p });
      out[tid] = null;
    } catch (e) { out[tid] = String(e); }
  }
  return JSON.stringify(out);
""", {"pid": pid, "tids": list(track_ids)})


def remove_tracks(pid, track_ids):
    """WRITES: remove every entry of these tracks from one user playlist.

    Deleting a track reference that belongs to a user playlist only takes it
    out of that playlist; the library playlist is refused by editablePlaylist.
    """
    return run_jxa_json("""
  const p = editablePlaylist(a.pid);
  const out = {};
  for (const tid of a.tids) {
    try {
      const hits = p.tracks.whose({ persistentID: tid })();
      for (const t of hits) t.delete();
      out[tid] = null;
    } catch (e) { out[tid] = String(e); }
  }
  return JSON.stringify(out);
""", {"pid": pid, "tids": list(track_ids)})


def delete_playlist(pid):
    """WRITES: delete a user playlist (its songs stay in the library)."""
    return run_jxa_json("""
  editablePlaylist(a.pid).delete();
  return JSON.stringify(null);
""", {"pid": pid})
