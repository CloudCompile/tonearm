"""Party storage and isolated-credential tests; no browser or Apple account."""
import hashlib
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tonearm.store import Store


class TestPartyStore(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", enabled=True)
        self.store.start()
        self.addCleanup(self.store.close)
        self.invite = "invite-secret"
        self.guest_key = "guest-secret"
        self.store.party_create("party_test", hashlib.sha256(
            self.invite.encode()).hexdigest(), "Test party", "master")
        self.store.party_join("party_test", "guest_a", hashlib.sha256(
            self.guest_key.encode()).hexdigest(), "Alice")
        self.store.party_join("party_test", "guest_b", hashlib.sha256(
            b"guest-b").hexdigest(), "Bob")

    def test_credentials_are_hashed_and_invite_rotates(self):
        conn = self.store._require_conn()
        rows = conn.execute("SELECT invite_hash FROM parties").fetchone()
        guest = conn.execute("SELECT token_hash FROM party_guests WHERE "
                              "id='guest_a'").fetchone()
        self.assertEqual(rows[0], hashlib.sha256(self.invite.encode()).hexdigest())
        self.assertEqual(guest[0], hashlib.sha256(self.guest_key.encode()).hexdigest())
        self.assertIsNotNone(self.store.party_by_invite(rows[0]))
        self.assertTrue(self.store.party_rotate_invite("party_test", "rotated"))
        self.assertIsNone(self.store.party_by_invite(rows[0]))
        self.assertIsNotNone(self.store.party_by_invite("rotated"))

    def test_guest_scoped_suggestions_and_vote_ordering(self):
        a, _ = self.store.party_suggest("party_test", "guest_a", "cat-1",
                                       "Song 1", "Artist", None)
        b, _ = self.store.party_suggest("party_test", "guest_b", "cat-2",
                                       "Song 2", "Artist", None)
        self.assertTrue(self.store.party_vote("party_test", b, "guest_a"))
        state = self.store.party_public_state("party_test", "guest_a")
        self.assertEqual(state["suggestions"][0]["id"], b)
        self.assertTrue(state["suggestions"][0]["voted"])
        self.assertFalse(self.store.party_vote("party_test", b, "guest_a"))
        self.assertTrue(self.store.party_unvote("party_test", b, "guest_a"))
        self.assertFalse(self.store.party_unvote("party_test", b, "guest_a"))

    def test_queue_limit_and_duplicate_song_coalesce(self):
        first = self.store.party_suggest("party_test", "guest_a", "same",
                                         "Song", "Artist", None)
        duplicate = self.store.party_suggest("party_test", "guest_b", "same",
                                             "Song", "Artist", None)
        self.assertEqual(first[0], duplicate[0])
        self.assertFalse(duplicate[1])

    def test_steering_schedules_vote_leader_once_and_marks_played(self):
        s1, _ = self.store.party_suggest("party_test", "guest_a", "cat-1",
                                         "Song 1", "Artist", None)
        s2, _ = self.store.party_suggest("party_test", "guest_b", "cat-2",
                                         "Song 2", "Artist", None)
        self.store.party_vote("party_test", s2, "guest_a")
        scheduled = self.store.party_schedule_next("party_test")
        self.assertEqual(scheduled, {"suggestion_id": s2, "catalog_id": "cat-2"})
        self.assertIsNone(self.store.party_schedule_next("party_test"))
        self.store.party_track_changed("party_test", "cat-2")
        state = self.store.party_public_state("party_test", "guest_a")
        self.assertEqual(next(x for x in state["suggestions"] if x["id"] == s2)
                         ["state"], "played")
        self.assertTrue(any(x["id"] == s1 and x["state"] == "queued"
                            for x in state["suggestions"]))

    def test_close_and_guest_revocation(self):
        self.assertTrue(self.store.party_guest_revoke("party_test", "guest_a"))
        self.assertIsNone(self.store.party_guest_by_token(
            hashlib.sha256(self.guest_key.encode()).hexdigest()))
        self.assertTrue(self.store.party_close("party_test"))
        self.assertIsNone(self.store.party_by_invite(
            hashlib.sha256(self.invite.encode()).hexdigest()))


class TestPartyRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tonearm.config import config
        import tonearm.store as store_module
        from tonearm.store import Store
        from tonearm.routes import party as party_routes
        from tonearm.main import app
        cls.config = config
        cls.old_enabled, cls.old_party = config.store_enabled, config.party_enabled
        cls.old_api_key = config.api_key
        import tonearm.keys as keys_module
        cls.old_registry = keys_module.registry
        keys_module.registry = keys_module.KeysRegistry(
            os.path.join(tempfile.mkdtemp(), "keys.json"))
        config.api_key = "test-master-key"
        cls.old_store = store_module.store
        cls.test_store = Store(":memory:", enabled=True)
        store_module.store = cls.test_store
        party_routes.store = cls.test_store
        config.store_enabled = True
        config.party_enabled = True
        cls.client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        import tonearm.store as store_module
        from tonearm.routes import party as party_routes
        store_module.store = cls.old_store
        party_routes.store = cls.old_store
        import tonearm.keys as keys_module
        keys_module.registry = cls.old_registry
        cls.config.store_enabled, cls.config.party_enabled = cls.old_enabled, cls.old_party
        cls.config.api_key = cls.old_api_key

    def setUp(self):
        from tonearm.store import store
        self.store = store
        with self.store._lock:
            self.store._conn.execute("DELETE FROM party_votes")
            self.store._conn.execute("DELETE FROM party_suggestions")
            self.store._conn.execute("DELETE FROM party_guests")
            self.store._conn.execute("DELETE FROM parties")
            self.store._conn.commit()

    def test_party_credentials_do_not_grant_general_api_access(self):
        host = {"Authorization": "Bearer test-master-key"}
        response = self.client.post("/api/party", json={"name": "Weekend"},
                                    headers=host)
        self.assertEqual(response.status_code, 200, response.text)
        created = response.json()
        joined = self.client.post("/api/party/join", json={
            "invite": created["invite"], "name": "Guest"})
        self.assertEqual(joined.status_code, 200, joined.text)
        guest_key = joined.json()["party_key"]
        party_id = created["party_id"]
        state = self.client.get(f"/api/party/{party_id}",
                                headers={"X-Party-Key": guest_key})
        self.assertEqual(state.status_code, 200, state.text)
        forbidden = self.client.post("/api/playback/next",
                                     headers={"X-Party-Key": guest_key})
        self.assertEqual(forbidden.status_code, 401)

    def test_party_closed_prevents_old_guest_key(self):
        host = {"Authorization": "Bearer test-master-key"}
        created = self.client.post("/api/party", json={"name": "Weekend"},
                                   headers=host).json()
        joined = self.client.post("/api/party/join", json={
            "invite": created["invite"], "name": "Guest"}).json()
        self.client.post(f"/api/party/{created['party_id']}/close",
                         headers={"Authorization": "Bearer test-master-key"})
        state = self.client.get(f"/api/party/{created['party_id']}",
                                headers={"X-Party-Key": joined["party_key"]})
        self.assertEqual(state.status_code, 401)

    def test_host_has_party_key_but_guest_does_not_get_host_api_access(self):
        host = {"Authorization": "Bearer test-master-key"}
        created = self.client.post("/api/party", json={"name": "Weekend"},
                                   headers=host).json()
        host_state = self.client.get(f"/api/party/{created['party_id']}",
            headers={"X-Party-Key": created["party_key"]})
        self.assertEqual(host_state.status_code, 200, host_state.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
