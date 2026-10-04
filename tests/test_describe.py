"""Describe a playlist: parsing, one sound per playlist, and the app's create flow."""
from sandbox import SandboxTest, track  # noqa: I001 - puts the project on sys.path first
import describe
import ui
from test_api import ApiTests

RAP = [track(f"R{i}", f"Rap {i}", "Kendrick Lamar" if i < 3 else f"Rapper {i}", "Hip-Hop/Rap") for i in range(8)]
POP = [track(f"P{i}", f"Pop {i}", f"Singer {i}", "Pop") for i in range(8)]
TEL = [track(f"T{i}", f"Telugu {i}", f"Composer {i}", "Telugu") for i in range(6)]
ROCK = [track(f"K{i}", f"Rock {i}", "AC/DC", "Rock") for i in range(3)]


class DescribeTests(SandboxTest):
    tracks = RAP + POP + TEL + ROCK
    playlists = [{"persistentID": "ALL", "name": "Everything", "smart": False, "specialKind": "none",
                  "trackIDs": [t["persistentID"] for t in RAP + POP + TEL + ROCK]}]

    def setUp(self):
        super().setUp()
        rows = {}
        for i, t in enumerate(RAP):
            rows[t["persistentID"]] = ("hype" if i % 2 else "chill", 5 if i % 2 else 2, ["workout"] if i % 2 else [],
                                       "english", "trap")
        for i, t in enumerate(POP):
            rows[t["persistentID"]] = ("feel-good", 5 if i % 2 else 2, ["party"], "english", "dance-pop")
        for i, t in enumerate(TEL):
            rows[t["persistentID"]] = ("romantic", 2, ["late-night"], "telugu", "film-melody")
        for t in ROCK:
            rows[t["persistentID"]] = ("hype", 5, ["workout"], "english", "hard-rock")
        self.set_labels(rows)

    def ids(self, text):
        return [t["persistentID"] for t, _ in describe.build(ui.load_lib(), text)["picks"]]

    def test_parse_reads_sound_energy_artists_and_exclusions(self):
        c = describe.parse("hard gym rap like Kendrick, no slow songs, without AC/DC", ui.load_lib())
        self.assertEqual(c["families"], {"rap"})
        self.assertEqual(c["energy"], [4, 5])
        self.assertIn("Kendrick Lamar", c["artists"])
        self.assertIn("workout", c["contexts"])
        self.assertIn("AC/DC", c["exclude_artists"])

    def test_gym_rap_is_only_loud_rap(self):
        got = self.ids("gym rap")
        self.assertTrue(got)
        self.assertTrue(all(i.startswith("R") for i in got), got)  # no AC/DC even though it's gym music
        self.assertTrue(all(int(i[1:]) % 2 for i in got), got)      # only the energy-5 ones

    def test_language_is_a_hard_filter(self):
        got = self.ids("late night telugu melodies")
        self.assertEqual(sorted(got), sorted(t["persistentID"] for t in TEL))

    def test_without_a_named_sound_it_keeps_one_family(self):
        got = self.ids("songs for the gym")
        families = {i[0] for i in got}
        self.assertEqual(len(families), 1, got)

    def test_named_artist_ranks_first(self):
        got = self.ids("rap like Kendrick")
        self.assertTrue(set(got[:3]) <= {"R0", "R1", "R2"}, got)


class DescribeApiTests(ApiTests):
    def test_describe_then_create_then_apply(self):
        res = self.call("describe", "late", "night", "telugu", "melodies")
        self.assertIn("telugu", res["chips"])
        keep = [p["i"] for p in res["picks"]][:2]
        plan = self.call("describe-create", request={"keep": keep})["plan"]
        add = next(o for o in plan["ops"] if o["op"] == "add_tracks")
        self.assertEqual(add["count"], 2)
        self.assertEqual(plan["ops"][0]["name"], "Late Night Telugu Melodies")
        out = self.call("apply", request={"plan": plan["id"], "ops": [add["n"]]})
        self.assertEqual(out["applied"], 2)
        self.assertIn("Late Night Telugu Melodies", [p["name"] for p in self.music.playlists.values()])


class MixedSplitTests(SandboxTest):
    """Splitting a mixed playlist groups by sound first: no AC/DC next to Despacito."""
    tracks = DescribeTests.tracks
    playlists = DescribeTests.playlists

    def setUp(self):
        super().setUp()
        rows = {}
        for i, t in enumerate(RAP):
            rows[t["persistentID"]] = ("hype", 5, ["workout"], "english", "trap")
        for i, t in enumerate(POP):
            rows[t["persistentID"]] = ("feel-good", 5 if i % 2 else 2, ["party"], "english", "dance-pop")
        for t in TEL:
            rows[t["persistentID"]] = ("romantic", 2, ["late-night"], "telugu", "film-melody")
        for t in ROCK:
            rows[t["persistentID"]] = ("hype", 5, ["workout"], "english", "hard-rock")
        self.set_labels(rows)

    def test_every_new_playlist_is_one_sound_and_one_energy_band(self):
        import labels
        import reorg
        lib = ui.load_lib()
        eff = labels.merged()
        ops, left = reorg.split_ops(lib, lib["playlists"][0], min_size=5)
        groups = [[t["id"] for t in o["tracks"]] for o in ops if o["op"] == "add_tracks"]
        self.assertTrue(groups)
        for ids in groups:
            self.assertEqual(len({labels.FAMILY[eff[i]["style"]] for i in ids}), 1, ids)
            energies = [eff[i]["energy"] for i in ids]
            self.assertTrue(max(energies) - min(energies) <= 1, ids)
        self.assertIn("K0", [t["id"] for t in left])  # 3 rock songs: too few for their own playlist
