"""End-to-end runs of the Music menu flows against the fake Music app."""
import artist
import labels
import metadata
import plans
import reorg
import ui
from sandbox import SandboxTest, track

TRACKS = [
    track("TEL1", "Samajavaragamana", "S.S. Thaman & Sid Sriram", "Telugu"),
    track("TEL2", "Buttabomma", "Armaan Malik & S.S. Thaman", "Telugu"),
    track("TEL3", "Inthandham", "Vishal Chandrashekar", "Telugu"),
    track("TEL4", "Ringa Ringa", "Devi Sri Prasad & Priya Hemesh", "Telugu"),      # in no playlist
    track("HIN1", "Tum Hi Ho", "Mithoon & Arijit Singh", "Bollywood"),
    track("HIN2", "Channa Mereya", "Pritam & Arijit Singh", "Bollywood"),
    track("HIN3", "Kesariya", "Pritam & Arijit Singh", "Bollywood"),
    track("RAP1", "HUMBLE.", "Kendrick Lamar", "Hip-Hop/Rap"),
    track("RAP2", "DNA.", "Kendrick Lamar", "Hip-Hop/Rap"),
    track("RAP3", "Money Trees", "Kendrick Lamar", "Hip-Hop/Rap"),
    track("RAP4", "luther", "Kendrick Lamar", "Hip-Hop/Rap"),                      # not in Kendrick playlist
    track("POP1", "Espresso", "Sabrina Carpenter", "Pop"),
]
PLAYLISTS = [
    {"persistentID": "PTEL", "name": "Telugu Tunes", "smart": False, "specialKind": "none",
     "trackIDs": ["TEL1", "TEL2", "TEL3"]},
    {"persistentID": "PHIN", "name": "Hindi Tunes", "smart": False, "specialKind": "none",
     "trackIDs": ["HIN1", "HIN2", "HIN3"]},
    {"persistentID": "PKEN", "name": "Kendrick ", "smart": False, "specialKind": "none",
     "trackIDs": ["RAP1", "RAP2", "RAP3"]},
    {"persistentID": "PMIX", "name": "Them", "smart": False, "specialKind": "none",
     "trackIDs": ["RAP1", "RAP2", "RAP3", "RAP4", "POP1", "RAP1"]},
]
LABELS = {
    "TEL1": ("romantic", 2, ["wind-down"], "telugu", "film-melody"),
    "TEL2": ("romantic", 3, ["sing-along"], "telugu", "film-melody"),
    "TEL3": ("romantic", 2, ["wind-down"], "telugu", "film-melody"),
    "TEL4": ("hype", 5, ["party"], "telugu", "film-dance"),
    "HIN1": ("romantic", 2, ["wind-down"], "hindi", "film-melody"),
    "HIN2": ("heartbreak", 2, ["late-night"], "hindi", "film-melody"),
    "HIN3": ("romantic", 3, ["drive"], "hindi", "film-melody"),
    "RAP1": ("hype", 5, ["workout"], "english", "trap"),
    "RAP2": ("aggressive", 5, ["workout"], "english", "trap"),
    "RAP3": ("chill", 2, ["late-night"], "english", "conscious-rap"),
    "RAP4": ("romantic", 2, ["late-night"], "english", "rnb"),
    "POP1": ("confident", 4, ["party"], "english", "dance-pop"),
}


class FlowTests(SandboxTest):
    tracks = TRACKS
    playlists = PLAYLISTS

    def setUp(self):
        super().setUp()
        self.set_labels(LABELS)

    def test_belong_adds_to_language_and_artist_playlists_only(self):
        self.dialogs.choices = ["all"]
        ui.belong(["TEL4", "RAP4"])
        offered = self.dialogs.shown[0][2]
        self.assertTrue(any("Ringa Ringa" in o and "Telugu Tunes" in o for o in offered))
        self.assertTrue(any("luther" in o and "Kendrick" in o for o in offered))
        self.assertFalse(any("Ringa Ringa" in o and "Hindi Tunes" in o for o in offered), offered)
        self.assertIn("TEL4", self.music.playlists["PTEL"]["trackIDs"])
        self.assertIn("RAP4", self.music.playlists["PKEN"]["trackIDs"])
        self.assertNotIn("TEL4", self.music.playlists["PHIN"]["trackIDs"])

    def test_belong_cancel_changes_nothing(self):
        self.dialogs.choices = ["all"]
        self.dialogs.alerts = ["Cancel"]
        before = {k: list(v["trackIDs"]) for k, v in self.music.playlists.items()}
        ui.belong(["TEL4"])
        self.assertEqual(before, {k: v["trackIDs"] for k, v in self.music.playlists.items()})

    def test_review_pulls_in_the_create_for_a_new_playlist(self):
        lib = ui.load_lib()
        plans.save_plan(plans.new_plan("Split Them", reorg.split_ops(lib, lib["playlists"][3], min_size=1)))
        self.dialogs.choices = [[0], [1]]  # the plan, then only the first add op
        ui.review()
        new = [p for p in self.music.playlists.values() if p["name"].startswith("Them · ")]
        self.assertEqual(len(new), 1)
        self.assertTrue(new[0]["trackIDs"])

    def test_split_partitions_every_song_once(self):
        lib = ui.load_lib()
        ops = reorg.split_ops(lib, lib["playlists"][3], min_size=1)
        placed = [t["id"] for o in ops if o["op"] == "add_tracks" for t in o["tracks"]]
        self.assertEqual(sorted(placed), sorted(set(PLAYLISTS[3]["trackIDs"])))
        gym = next(o for o in ops if o["op"] == "add_tracks" and o["playlist"]["name"] == "Them · Gym")
        self.assertEqual([t["id"] for t in gym["tracks"]], ["RAP1", "RAP2"])

    def test_artist_playlist_creates_then_fills_after_adding(self):
        new_song = {"name": "Not Like Us", "artist": "Kendrick Lamar", "album": "Not Like Us", "genre": "Hip-Hop/Rap",
                    "url": "https://music.apple.com/x", "trackId": 1, "popularity": 1.0, "libraryID": None,
                    "genreFit": 1, "soundFit": None, "soundScore": None, "score": 1}
        owned = dict(new_song, name="HUMBLE.", libraryID="RAP1", score=0.9)
        self.patch(artist, "best_songs", lambda lib, name, **kw: {
            "artist": "Kendrick Lamar", "artistId": 1, "country": "in", "soundUsed": False,
            "picks": [new_song, owned]})
        self.patch(ui.subprocess, "run", lambda *a, **k: None)  # don't really open URLs

        def user_adds_song(message, buttons=("OK",), default=None):
            self.dialogs.shown.append(("alert", message, buttons))
            if "Next" in buttons:  # the user clicks + in Music, then Next
                self.music.tracks["NEW1"] = track("NEW1", "Not Like Us", "Kendrick Lamar", "Hip-Hop/Rap")
                return "Next"
            return {"Apply": "Apply", "Start": "Start"}.get(buttons[-1], buttons[-1])
        self.patch(ui, "alert", user_adds_song)
        self.dialogs.asks = ["Kendrick Lamar"]
        self.dialogs.choices = ["all"]
        ui.artist_playlist()
        made = next(p for p in self.music.playlists.values() if p["name"] == "Kendrick Lamar · For You")
        self.assertEqual(made["trackIDs"], ["RAP1", "NEW1"])

    def test_refresh_queues_fill_for_songs_added_later(self):
        lib = ui.load_lib()
        result = {"artist": "Kendrick Lamar", "picks": [
            {"name": "Not Like Us", "artist": "Kendrick Lamar", "album": "x", "url": "u", "trackId": 1,
             "libraryID": None, "popularity": 1, "genreFit": 1}]}
        ops, waiting = artist.plan_ops(lib, result)
        plan = plans.new_plan("Artist playlist: Kendrick Lamar", ops, lib)
        plan["waiting"] = waiting
        plans.set_approval(plan, "all", True)
        plans.apply_plan(plan)
        self.music.tracks["NEW1"] = track("NEW1", "Not Like Us", "Kendrick Lamar", "Hip-Hop/Rap")
        for name in ("enrich_apple", "enrich_audio", "enrich_musicbrainz"):
            self.patch(metadata, name, lambda lib, **kw: {})
        ui.refresh()
        fills = [p for p in plans.all_plans() if p["title"].startswith("Fill ")]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["ops"][0]["tracks"][0]["id"], "NEW1")
        self.assertEqual(plans.load_plan(plan["id"])["waiting"], [])

    def test_manual_label_overrides_inferred(self):
        labels.save("manual", {"RAP3": {"energy": 5, "mood": "hype"}})
        lib = ui.load_lib()
        ops = reorg.split_ops(lib, lib["playlists"][3], min_size=1)
        gym = next(o for o in ops if o["op"] == "add_tracks" and o["playlist"]["name"] == "Them · Gym")
        self.assertIn("RAP3", [t["id"] for t in gym["tracks"]])
