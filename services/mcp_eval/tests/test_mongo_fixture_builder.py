import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from build_task_mongo_fixture import (  # noqa: E402
    _content_digest,
    _fixture_files,
    _source_database_dir,
)


class MongoFixtureBuilderTests(unittest.TestCase):
    def test_arbitrary_source_database_is_normalized_from_dump_root(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "custom_seed"
            database.mkdir()
            (database / "orders.bson").write_bytes(b"synthetic-bson")
            (database / "orders.metadata.json").write_text(
                "{}", encoding="utf-8",
            )
            source = _source_database_dir(root, "custom_seed")
            files = _fixture_files(source)
            self.assertEqual(database, source)
            self.assertEqual(2, len(files))
            self.assertEqual(64, len(_content_digest(source, files)))

    def test_dump_without_bson_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "empty"
            database.mkdir()
            (database / "readme.txt").write_text("not a dump", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "no BSON"):
                _fixture_files(database)


if __name__ == "__main__":
    unittest.main()
