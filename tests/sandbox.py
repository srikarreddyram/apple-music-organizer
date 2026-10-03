"""Test sandbox: a fake Music app and temp data dirs, so nothing real is read or written."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import labels  # noqa: E402
import metadata  # noqa: E402
import music_bridge  # noqa: E402
import organizer  # noqa: E402
import plans  # noqa: E402
import ui  # noqa: E402


def track(pid, name, artist, genre="Pop", album=None, plays=0, in_library=True):
    return {"persistentID": pid, "name": name, "artist": artist, "albumArtist": artist, "album": album or name,
            "genre": genre, "year": 2020, "duration": 200.0, "playedCount": plays, "skippedCount": 0,
            "favorited": False, "disliked": False, "dateAdded": None, "playedDate": None,
            "cloudStatus": "subscription", "trackNumber": 1, "discNumber": 1, "inLibrary": in_library}


class FakeMusic:
    """In-memory stand-in for the music_bridge functions."""

    def __init__(self, tracks, playlists):
        self.tracks = {t["persistentID"]: t for t in tracks}
        self.playlists = {p["persistentID"]: dict(p, trackIDs=list(p["trackIDs"])) for p in playlists}
        self.fail_add = set()
        self.next_id = 1

    def read_playlist(self, pid):
        p = self.playlists.get(pid)
        return json.loads(json.dumps(p)) if p else None

    def _editable(self, pid):
        p = self.playlists.get(pid)
        if not p:
            raise music_bridge.MusicError(f"playlist {pid} not found")
        if p["smart"] or p["specialKind"] != "none":
            raise music_bridge.MusicError("not an editable user playlist")
        return p

    def create_playlist(self, name):
        pid = f"NEW{self.next_id:013d}"
        self.next_id += 1
        self.playlists[pid] = {"persistentID": pid, "name": name, "smart": False, "specialKind": "none",
                               "trackIDs": []}
        return pid

    def add_tracks(self, pid, ids):
        p = self._editable(pid)
        out = {}
        for i in ids:
            if i in self.fail_add or i not in self.tracks:
                out[i] = "boom"
            else:
                p["trackIDs"].append(i)
                out[i] = None
        return out

    def remove_tracks(self, pid, ids):
        p = self._editable(pid)
        p["trackIDs"] = [i for i in p["trackIDs"] if i not in ids]
        return {i: None for i in ids}

    def delete_playlist(self, pid):
        self._editable(pid)
        del self.playlists[pid]

    def read_library(self):
        return {"tracks": [dict(t) for t in self.tracks.values()],
                "playlists": [json.loads(json.dumps(p)) for p in self.playlists.values()]}


class Dialogs:
    """Scripted answers for ui.choose / ui.alert / ui.ask, recording what was shown."""

    def __init__(self):
        self.choices, self.alerts, self.asks, self.shown = [], [], [], []

    def choose(self, items, prompt, multiple=False, ok="OK", preselect_all=False):
        self.shown.append(("choose", prompt, list(items)))
        answer = self.choices.pop(0)
        return list(range(len(items))) if answer == "all" else answer

    def alert(self, message, buttons=("OK",), default=None):
        self.shown.append(("alert", message, buttons))
        return self.alerts.pop(0) if self.alerts else buttons[-1]

    def ask(self, prompt, default=""):
        self.shown.append(("ask", prompt))
        return self.asks.pop(0)


class SandboxTest(unittest.TestCase):
    """Points every data path at a temp dir and swaps Music and dialogs for fakes."""

    tracks = []
    playlists = []

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = []
        self.patch(plans, "PLANS", self.tmp / "plans")
        self.patch(plans, "AUDIT", self.tmp / "audit.jsonl")
        self.patch(plans, "CREATED", self.tmp / "created.json")
        self.patch(labels, "LABELS", self.tmp / "labels")
        self.patch(metadata, "META", self.tmp / "metadata")
        self.patch(organizer, "DATA", self.tmp)
        self.patch(organizer, "INVENTORY", self.tmp / "inventory.json")
        self.patch(ui, "INVENTORY", self.tmp / "inventory.json")
        self.music = FakeMusic(self.tracks, self.playlists)
        for name in ("read_playlist", "create_playlist", "add_tracks", "remove_tracks", "delete_playlist",
                     "read_library"):
            self.patch(music_bridge, name, getattr(self.music, name))
        self.dialogs = Dialogs()
        for name in ("choose", "alert", "ask"):
            self.patch(ui, name, getattr(self.dialogs, name))
        self.patch(ui, "notify", lambda msg: None)
        organizer.scan()

    def patch(self, obj, name, value):
        self.patches.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def tearDown(self):
        for obj, name, value in reversed(self.patches):
            setattr(obj, name, value)

    def set_labels(self, rows):
        """rows: {pid: (mood, energy, contexts, language, style)}"""
        data = {pid: {"mood": m, "energy": e, "contexts": c, "language": lang, "style": s,
                      "confidence": "high", "source": "test"} for pid, (m, e, c, lang, s) in rows.items()}
        labels.save("inferred", data)
