import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import storage
from storage import JsonStore, transaction


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.stores = [JsonStore(self.directory / name) for name in ("lobbies.json", "stats.json", "wallet.json")]
        for store in self.stores:
            store.data = {"record": {"value": 1}}
            asyncio.run(store.save())

    def test_commit_all_files_and_remove_journal(self):
        async def run():
            with transaction(*self.stores):
                for store in self.stores:
                    store.data["record"]["value"] = 2
                    await store.save()
                    self.assertEqual(json.loads(store.path.read_text())["record"]["value"], 1)
        asyncio.run(run())
        self.assertFalse((self.directory / storage.JOURNAL).exists())
        self.assertTrue(all(JsonStore(s.path).load()["record"]["value"] == 2 for s in self.stores))

    def test_crash_between_files_recovers_whole_result(self):
        original = storage._write_json
        def interrupted(path, data):
            if path.name == "stats.json":
                raise OSError("disk interrupted")
            original(path, data)
        with self.assertRaises(OSError), patch.object(storage, "_write_json", interrupted):
            with transaction(*self.stores):
                for store in self.stores:
                    store.data["record"]["value"] = 3
        self.assertTrue((self.directory / storage.JOURNAL).exists())
        # Load even the first file to recover the other two before exposing state.
        self.assertEqual(JsonStore(self.stores[0].path).load()["record"]["value"], 3)
        self.assertTrue(all(JsonStore(s.path).load()["record"]["value"] == 3 for s in self.stores))
        self.assertFalse((self.directory / storage.JOURNAL).exists())

    def test_failure_before_journal_restores_live_record(self):
        live = self.stores[0].data["record"]
        with self.assertRaises(OSError), patch.object(storage, "_write_json", side_effect=OSError("full disk")):
            with transaction(*self.stores):
                live["value"] = 99
        self.assertEqual(live["value"], 1)
        self.assertIs(self.stores[0].data["record"], live)
        self.assertTrue(all(JsonStore(s.path).load()["record"]["value"] == 1 for s in self.stores))

    def test_body_exception_rolls_back_without_touching_disk(self):
        with self.assertRaises(RuntimeError):
            with transaction(*self.stores):
                self.stores[0].data["record"]["value"] = 9
                raise RuntimeError("calculation failed")
        self.assertEqual(self.stores[0].data["record"]["value"], 1)

    def test_corrupt_data_is_not_silently_erased(self):
        self.stores[0].path.write_text("broken")
        with self.assertRaises(ValueError):
            self.stores[0].load()
        self.assertEqual(self.stores[0].path.read_text(), "broken")

    def test_next_save_finishes_previous_transaction(self):
        original = storage._write_json
        def interrupted(path, data):
            if path.name == "stats.json":
                raise OSError("interrupted")
            original(path, data)
        with self.assertRaises(OSError), patch.object(storage, "_write_json", interrupted):
            with transaction(*self.stores):
                for store in self.stores:
                    store.data["record"]["value"] = 5
        self.stores[2].data["record"]["value"] += 1
        asyncio.run(self.stores[2].save())
        self.assertEqual(JsonStore(self.stores[1].path).load()["record"]["value"], 5)
        self.assertEqual(JsonStore(self.stores[2].path).load()["record"]["value"], 6)
