"""The menu bar app's JSON interface, against the fake Music app."""
import io
import json
import sys

from sandbox import SandboxTest  # noqa: I001 - puts the project on sys.path first
from test_ui import LABELS, PLAYLISTS, TRACKS
import api
import music_bridge


class ApiTests(SandboxTest):
    tracks = TRACKS
    playlists = PLAYLISTS

    def setUp(self):
        super().setUp()
        self.set_labels(LABELS)

    def call(self, command, *args, request=None):
        self.patch(api, "read_request", lambda: request or {})
        out = io.StringIO()
        self.patch(sys, "stdout", out)
        api.main(command, list(args))
        sys.stdout = sys.__stdout__
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def test_split_then_apply_with_a_chosen_name(self):
        plan = self.call("split", "PMIX")["plan"]
        first_add = next(o for o in plan["ops"] if o["op"] == "add_tracks")
        create = next(o for o in plan["ops"] if o["op"] == "create_playlist" and o["ref"] == first_add["ref"])
        res = self.call("apply", request={"plan": plan["id"], "ops": [first_add["n"]],
                                          "names": {str(create["n"]): "Gym Rats Only"}})
        self.assertEqual(res["applied"], 2, res)  # the add pulled in its create
        self.assertIn("Gym Rats Only", [p["name"] for p in self.music.playlists.values()])
        self.assertTrue(res["undo"])
        undo = self.call("undo", res["undo"])
        self.assertEqual(undo["applied"], undo["total"])
        self.assertNotIn("Gym Rats Only", [p["name"] for p in self.music.playlists.values()])

    def test_belong_then_add(self):
        res = self.call("belong", "TEL4")
        sug = res["songs"][0]["suggestions"][0]
        self.assertEqual(sug["playlist"], "Telugu Tunes")
        out = self.call("add", request={"picks": [["TEL4", sug["playlistId"]]]})
        self.assertEqual(out["applied"], 1)
        self.assertIn("TEL4", self.music.playlists["PTEL"]["trackIDs"])

    def test_apply_needs_an_explicit_choice(self):
        plan = self.call("split", "PMIX")["plan"]
        self.assertIn("error", self.call("apply", request={"plan": plan["id"], "ops": []}))
        self.assertEqual(len(self.music.playlists), len(PLAYLISTS))

    def test_context_reports_the_open_playlist(self):
        self.patch(music_bridge, "current_context", lambda: {
            "playlist": {"persistentID": "PMIX", "name": "Them", "smart": False, "specialKind": "none"},
            "selection": [{"id": "RAP1", "name": "HUMBLE.", "artist": "Kendrick Lamar"}]})
        ctx = self.call("context")
        self.assertEqual(ctx["playlist"]["name"], "Them")
        self.assertEqual(ctx["selection"][0]["scanned"], True)

    def test_errors_come_back_as_json(self):
        self.assertIn("error", self.call("split", "NOPE"))
