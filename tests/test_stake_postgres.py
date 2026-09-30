"""Exercise the actual JSONB boundary; SQLite accepts strings PostgreSQL rejects."""
import copy
import json
import os
import unittest

from economic_machine.values import digest
from finance_service.stake_market import normalize
from test_stake_observations import AT, evidence


@unittest.skipUnless(os.environ.get("STAKE_METADATA_POSTGRES_DSN"),
                     "dedicated PostgreSQL metadata test DSN not configured")
class StakePostgresTests(unittest.TestCase):
    def test_onchain_url_survives_jsonb_and_hash_replay(self):
        import psycopg
        from psycopg.types.json import Jsonb
        raw = evidence()
        raw_url = "\x00updateName1530608873463"
        raw["witnesses"]["witnesses"][0]["url"] = raw_url
        with psycopg.connect(os.environ["STAKE_METADATA_POSTGRES_DSN"]) as db:
            with self.assertRaises(psycopg.errors.UntranslatableCharacter):
                db.execute("SELECT %s::jsonb", (Jsonb(raw),))
            db.rollback()
            market = normalize(raw, AT)
            stored = db.execute("SELECT %s::jsonb", (Jsonb(market),)).fetchone()[0]
        self.assertEqual(stored, market)
        self.assertEqual(normalize(stored["evidence"], AT), market)
        self.assertEqual(json.loads(stored["evidence"]["witnesses"]["witnesses"][0]["url"]["value"]), raw_url)
        without_hash = copy.deepcopy(stored)
        self.assertEqual(without_hash.pop("hash"), digest(without_hash))
