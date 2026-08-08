import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("arxiv_html_backfill", ROOT / "scripts" / "arxiv_html_backfill.py")
backfill = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(backfill)


class HtmlMatchingTests(unittest.TestCase):
    def test_transnormal_affiliation_is_an_exact_html_match(self):
        source = """
        <span class="ltx_creator ltx_role_author">
          <span class="ltx_personname">Mingwei Li</span>
          <br/>1 Zhejiang University, 2 Zhongguancun Academy, Beijing, China
        </span>
        """
        matches = backfill.find_institution_matches(source)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["institution"], "zgca")
        self.assertEqual(matches[0]["level"], "exact-affiliation")

    def test_body_reference_is_kept_for_audit_but_not_exact(self):
        source = "<section><p>We compare with a report from Zhongguancun Academy.</p></section>"
        matches = backfill.find_institution_matches(source)
        self.assertEqual(matches[0]["level"], "association")

    def test_casia_name_is_not_treated_as_the_target_academy(self):
        source = '<span class="ltx_role_affiliation">CASIA Zhongguancun Academy, Beijing</span>'
        self.assertEqual(backfill.find_institution_matches(source), [])


class PartnerPrefilterTests(unittest.TestCase):
    def test_partner_configuration_is_traceable_and_nonempty(self):
        config = backfill.load_partner_config()
        self.assertIn("github.com/bjzgcai", config["source"])
        self.assertGreaterEqual(len(config["institutions"]), 19)
        self.assertIn("Zhejiang University", [item["name"] for item in config["institutions"]])

    def test_extracts_arxiv_locations_and_applies_month_window(self):
        record = {"locations": [
            {"landing_page_url": "https://arxiv.org/abs/2602.00839", "pdf_url": "https://arxiv.org/pdf/2602.00839"}
        ]}
        self.assertEqual(backfill.arxiv_ids_from_openalex(record), ["2602.00839"])
        self.assertTrue(backfill.id_is_in_date_window("2406.00001", "2024-06-01", "2026-08-08"))
        self.assertFalse(backfill.id_is_in_date_window("2405.99999", "2024-06-01", "2026-08-08"))

    def test_openalex_prefilter_records_partner_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = backfill.connect_database(Path(directory) / "scan.sqlite3")
            try:
                record = {
                    "title": "Example",
                    "doi": None,
                    "publication_date": "2026-02-01",
                    "abstract_inverted_index": {"Example": [0], "abstract": [1]},
                    "locations": [{"landing_page_url": "https://arxiv.org/abs/2602.00839", "pdf_url": None}],
                    "authorships": [{
                        "author": {"display_name": "Mingwei Li"},
                        "institutions": [{"id": "https://openalex.org/I1"}],
                        "raw_affiliation_strings": ["Zhejiang University"],
                    }],
                }
                partner = {"display_name": "Zhejiang University", "openalex_id": "I1"}
                added = backfill.upsert_openalex_work(connection, record, partner, "2024-06-01", "2026-08-08")
                row = connection.execute("SELECT * FROM papers WHERE arxiv_id = '2602.00839'").fetchone()
                self.assertEqual(added, 1)
                self.assertEqual(row["prefilter_source"], "OpenAlex partner institution prefilter")
                self.assertEqual(json.loads(row["prefilter_institutions_json"])[0]["name"], "Zhejiang University")
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
