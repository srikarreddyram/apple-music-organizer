from sandbox import SandboxTest, track
import plans

T = lambda i: {"id": i, "name": i, "artist": "x"}


class PlanTests(SandboxTest):
    tracks = [track(i, i, "x") for i in "ABCWYZ"]
    playlists = [
        {"persistentID": "P1", "name": "Mine", "smart": False, "specialKind": "none", "trackIDs": ["A", "B"]},
        {"persistentID": "S1", "name": "Smart", "smart": True, "specialKind": "none", "trackIDs": ["A"]},
    ]

    def approved(self, title, ops):
        plan = plans.new_plan(title, ops)
        plans.set_approval(plan, "all", True)
        return plan

    def test_apply_verify_and_undo_round_trip(self):
        plan = self.approved("t", [
            {"op": "create_playlist", "name": "New", "ref": "new"},
            {"op": "add_tracks", "playlist": {"ref": "new", "name": "New"}, "tracks": [T("A"), T("C")]},
            {"op": "add_tracks", "playlist": {"id": "P1", "name": "Mine"}, "tracks": [T("B"), T("C")]},
            {"op": "remove_tracks", "playlist": {"id": "P1", "name": "Mine"}, "tracks": [T("A")]},
        ])
        done, undo = plans.apply_plan(plan)
        self.assertEqual([o["status"] for o in done], ["applied"] * 4)
        self.assertEqual(self.music.playlists["P1"]["trackIDs"], ["B", "C"])
        # Undo skips removing tracks from the playlist it deletes anyway.
        self.assertEqual([o["op"] for o in undo["ops"]], ["add_tracks", "remove_tracks", "delete_playlist"])
        plans.set_approval(undo, "all", True)
        plans.apply_plan(undo)
        self.assertEqual(sorted(self.music.playlists), ["P1", "S1"])
        self.assertEqual(sorted(self.music.playlists["P1"]["trackIDs"]), ["A", "B"])

    def test_refuses_smart_playlists_and_foreign_deletes(self):
        p = self.approved("t", [{"op": "add_tracks", "playlist": {"id": "S1", "name": "Smart"}, "tracks": [T("C")]}])
        plans.apply_plan(p)
        self.assertEqual(p["ops"][0]["status"], "failed")
        p = self.approved("t", [{"op": "delete_playlist", "playlist": {"id": "P1", "name": "Mine"}}])
        plans.apply_plan(p)
        self.assertEqual(p["ops"][0]["status"], "failed")
        self.assertIn("P1", self.music.playlists)

    def test_partial_failure_stops_and_undoes_only_what_happened(self):
        self.music.fail_add.add("Z")
        p = self.approved("t", [
            {"op": "add_tracks", "playlist": {"id": "P1", "name": "Mine"}, "tracks": [T("Y"), T("Z")]},
            {"op": "add_tracks", "playlist": {"id": "P1", "name": "Mine"}, "tracks": [T("W")]},
        ])
        _, undo = plans.apply_plan(p)
        self.assertEqual([o["status"] for o in p["ops"]], ["failed", "pending"])
        self.assertEqual([t["id"] for t in undo["ops"][0]["tracks"]], ["Y"])
        self.assertTrue(plans.AUDIT.exists())

    def test_dependent_add_fails_without_its_create(self):
        plan = plans.new_plan("t", [
            {"op": "create_playlist", "name": "New", "ref": "new"},
            {"op": "add_tracks", "playlist": {"ref": "new", "name": "New"}, "tracks": [T("A")]},
        ])
        plans.set_approval(plan, "2", True)
        plans.apply_plan(plan)
        self.assertEqual(plan["ops"][1]["status"], "failed")
        self.assertEqual(len(self.music.playlists), 2)
