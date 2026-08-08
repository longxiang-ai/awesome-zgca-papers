import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pipeline", ROOT / "scripts" / "pipeline.py")
pipeline = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(pipeline)


class MatchingTests(unittest.TestCase):
    def test_accepts_verified_english_and_chinese_aliases(self):
        self.assertEqual(pipeline.match_institutions("2 Beijing Zhongguancun Academy, Beijing, China")[0][0], "zgca")
        self.assertEqual(pipeline.match_institutions("北京中关村学院，海淀区")[0][0], "zgca")
        self.assertEqual(pipeline.match_institutions("中关村人工智能研究院")[0][0], "zgci")

    def test_rejects_ambiguous_acronyms_without_structured_context(self):
        self.assertEqual(pipeline.match_institutions("ZGCA research"), [])
        self.assertEqual(pipeline.match_institutions("ZGCI research"), [])

    def test_rejects_common_false_positives(self):
        for text in [
            "Beijing Zhongguancun Hospital, China",
            "Zhongguancun Science Park, Beijing",
            "Institute of Computing Technology, Chinese Academy of Sciences, Zhongguancun",
            "Zhongguancun Laboratory, Beijing",
        ]:
            self.assertEqual(pipeline.match_institutions(text), [], text)


class DataTests(unittest.TestCase):
    def test_seed_data_is_valid_and_unique(self):
        works = json.loads((ROOT / "data" / "works.json").read_text(encoding="utf-8"))
        pipeline.validate(works)
        self.assertEqual(len({work["id"] for work in works}), len(works))

    def test_dedupes_doi_and_preserves_existing_record(self):
        existing = json.loads((ROOT / "data" / "works.json").read_text(encoding="utf-8"))
        incoming = dict(existing[0])
        incoming["id"] = "different-source-id"
        incoming["sources"] = ["Another source"]
        merged, added = pipeline.deduplicate(existing, [incoming])
        self.assertEqual(added, 0)
        target = next(item for item in merged if item["id"] == existing[0]["id"])
        self.assertIn("Another source", target["sources"])

    def test_dedupes_repository_versions_by_normalized_title(self):
        existing = json.loads((ROOT / "data" / "works.json").read_text(encoding="utf-8"))
        first = dict(existing[0])
        first["id"] = "doi-version-one"
        first["identifiers"] = {"doi": "10.5281/zenodo.1"}
        second = dict(existing[0])
        second["id"] = "doi-version-two"
        second["identifiers"] = {"doi": "10.5281/zenodo.2"}
        merged, _ = pipeline.deduplicate([first, second], [])
        self.assertEqual(len(merged), 1)
        self.assertTrue(any("10.5281/zenodo.2" in item["url"] for item in merged[0]["versions"]))

    def test_build_is_stable_without_new_records(self):
        works = pipeline.load_works()
        coverage = {"test": "ok"}
        original_root, original_data, original_public = pipeline.ROOT, pipeline.DATA, pipeline.PUBLIC
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            try:
                pipeline.ROOT = sandbox
                pipeline.DATA = sandbox / "data"
                pipeline.PUBLIC = sandbox / "public"
                pipeline.export_outputs(works, coverage)
                first = (pipeline.PUBLIC / "data" / "works.json").read_bytes()
                pipeline.export_outputs(works, coverage)
                second = (pipeline.PUBLIC / "data" / "works.json").read_bytes()
            finally:
                pipeline.ROOT, pipeline.DATA, pipeline.PUBLIC = original_root, original_data, original_public
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
