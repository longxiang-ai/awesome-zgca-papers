import importlib.util
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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
        self.assertTrue(all(item.get("openalexId", "").startswith("I") for item in config["institutions"]))

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


class PacedClientTests(unittest.TestCase):
    def test_connection_reset_during_response_read_is_retried(self):
        class FakeResponse:
            status = 200
            headers = {}

            def __init__(self, body=None, error=None):
                self.body = body
                self.error = error

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                if self.error:
                    raise self.error
                return self.body

        with tempfile.TemporaryDirectory() as directory:
            connection = backfill.connect_database(Path(directory) / "scan.sqlite3")
            client = backfill.PacedClient(connection)
            responses = [
                FakeResponse(error=ConnectionResetError(54, "Connection reset by peer")),
                FakeResponse(body=b"<html>ok</html>"),
            ]
            try:
                with mock.patch.object(client, "_pace"), \
                     mock.patch.object(backfill.time, "sleep"), \
                     mock.patch.object(backfill.urllib.request, "urlopen", side_effect=responses) as urlopen:
                    status, body, _ = client.get("https://arxiv.org/html/2505.11151v1", retries=1)
                self.assertEqual(status, 200)
                self.assertEqual(body, "<html>ok</html>")
                self.assertEqual(urlopen.call_count, 2)
            finally:
                connection.close()

    def test_html_scan_defers_network_failure_without_inline_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = backfill.connect_database(Path(directory) / "scan.sqlite3")
            try:
                backfill.upsert_records(connection, [{
                    "arxiv_id": "2505.11151", "created": "2025-05-01", "updated": "",
                    "title": "Example", "authors": [], "categories": "cs.CV", "abstract": "",
                    "doi": "", "journal_ref": "", "comments": "",
                }], "2024-06-01")
                connection.execute(
                    "UPDATE papers SET prefilter_source = 'test' WHERE arxiv_id = '2505.11151'"
                )
                connection.commit()
                row = connection.execute("SELECT * FROM papers WHERE arxiv_id = '2505.11151'").fetchone()
                client = mock.Mock()
                client.get.side_effect = TimeoutError("timed out")

                result = backfill.scan_one(connection, client, row)

                self.assertEqual(result, "retry")
                client.get.assert_called_once_with("https://arxiv.org/html/2505.11151v1", retries=0)
                stored = connection.execute(
                    "SELECT scan_status, attempts FROM papers WHERE arxiv_id = '2505.11151'"
                ).fetchone()
                self.assertEqual((stored["scan_status"], stored["attempts"]), ("retry", 1))
            finally:
                connection.close()


class ScanQueueTests(unittest.TestCase):
    def test_pending_papers_are_processed_before_retry_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = backfill.connect_database(Path(directory) / "scan.sqlite3")
            try:
                records = []
                for arxiv_id in ("2406.00001", "2501.00001"):
                    records.append({
                        "arxiv_id": arxiv_id, "created": "2025-01-01", "updated": "",
                        "title": arxiv_id, "authors": [], "categories": "cs.CV", "abstract": "",
                        "doi": "", "journal_ref": "", "comments": "",
                    })
                backfill.upsert_records(connection, records, "2024-06-01")
                connection.execute("UPDATE papers SET prefilter_source = 'test'")
                connection.execute(
                    "UPDATE papers SET scan_status = 'retry', attempts = 1 WHERE arxiv_id = '2406.00001'"
                )
                connection.commit()
                client = mock.Mock()
                client.get.return_value = (404, "", {})

                backfill.scan(connection, client, limit=1, merge=False)

                client.get.assert_called_once_with("https://arxiv.org/html/2501.00001v1", retries=0)
                retry_row = connection.execute(
                    "SELECT scan_status, attempts FROM papers WHERE arxiv_id = '2406.00001'"
                ).fetchone()
                self.assertEqual((retry_row["scan_status"], retry_row["attempts"]), ("retry", 1))
            finally:
                connection.close()


class PersistentStateTests(unittest.TestCase):
    @staticmethod
    def record(arxiv_id, status="no_match", **updates):
        record = {
            "arxivId": arxiv_id, "status": status,
            "firstSeenAt": "2026-08-01T00:00:00Z", "checkedAt": "2026-08-02T00:00:00Z",
            "nextRetryAt": None, "attempts": 1, "unavailableAttempts": 0,
            "httpStatus": 200, "lastError": None, "exactMatch": status == "matched",
        }
        record.update(updates)
        return record

    def write_state(self, path, records):
        path.write_text(
            "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in records),
            encoding="utf-8",
        )

    def insert_candidate(self, connection, arxiv_id="2608.99991"):
        backfill.upsert_records(connection, [{
            "arxiv_id": arxiv_id, "created": "2026-08-01", "updated": "",
            "title": "Incremental paper", "authors": [{"name": "A. Author"}],
            "categories": "cs.CV", "abstract": "Abstract", "doi": "",
            "journal_ref": "", "comments": "",
        }], "2024-06-01")
        connection.execute(
            "UPDATE papers SET prefilter_source='test', first_seen_at='2026-08-01T00:00:00Z' WHERE arxiv_id=?",
            (arxiv_id,),
        )
        connection.commit()

    def test_restore_skips_completed_history_and_export_is_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.jsonl"
            self.write_state(state, [self.record("2608.00002"), self.record("2608.00001", "matched")])
            connection = backfill.connect_database(Path(directory) / "scan.sqlite3")
            try:
                self.assertEqual(backfill.restore_state(connection, state), 2)
                client = mock.Mock()
                self.assertEqual(backfill.scan_incremental(connection, client), {})
                client.get.assert_not_called()
                self.assertTrue(backfill.export_state(connection, state))
                first = state.read_bytes()
                self.assertFalse(backfill.export_state(connection, state))
                self.assertEqual(state.read_bytes(), first)
                self.assertEqual([json.loads(line)["arxivId"] for line in state.read_text().splitlines()], [
                    "2608.00001", "2608.00002",
                ])
            finally:
                connection.close()

    def test_new_exact_candidate_is_scanned_once_and_mergeable(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.jsonl"
            connection = backfill.connect_database(Path(directory) / "scan.sqlite3")
            try:
                self.insert_candidate(connection)
                client = mock.Mock()
                client.get.return_value = (
                    200,
                    '<span class="ltx_role_affiliation">Zhongguancun Academy</span>',
                    {},
                )
                counts = backfill.scan_incremental(connection, client, now=dt.datetime(2026, 8, 13, tzinfo=dt.timezone.utc))
                self.assertEqual(counts, {"matched": 1})
                row = connection.execute("SELECT * FROM papers WHERE arxiv_id='2608.99991'").fetchone()
                candidate = backfill.candidate_from_row(row)
                self.assertEqual(candidate["institutions"], ["zgca"])
                self.assertEqual(backfill.scan_incremental(connection, client), {})
                self.assertEqual(client.get.call_count, 1)
                backfill.export_state(connection, state)
            finally:
                connection.close()
            resumed = backfill.connect_database(Path(directory) / "resumed.sqlite3")
            try:
                backfill.restore_state(resumed, state)
                self.insert_candidate(resumed)
                resumed_client = mock.Mock()
                self.assertEqual(backfill.scan_incremental(resumed, resumed_client), {})
                resumed_client.get.assert_not_called()
            finally:
                resumed.close()

    def test_recent_404_retries_then_becomes_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = backfill.connect_database(Path(directory) / "scan.sqlite3")
            try:
                self.insert_candidate(connection)
                client = mock.Mock()
                client.get.return_value = (404, "", {})
                day = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
                for attempt in range(1, 8):
                    row = connection.execute("SELECT * FROM papers WHERE arxiv_id='2608.99991'").fetchone()
                    status = backfill.scan_one(connection, client, row, incremental=True, now=day + dt.timedelta(days=attempt - 1))
                    self.assertEqual(status, "retry" if attempt < 7 else "html_unavailable")
                stored = connection.execute(
                    "SELECT unavailable_attempts, next_retry_at FROM papers WHERE arxiv_id='2608.99991'"
                ).fetchone()
                self.assertEqual(stored["unavailable_attempts"], 7)
                self.assertIsNone(stored["next_retry_at"])
            finally:
                connection.close()

    def test_404_becomes_terminal_after_fourteen_day_window(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = backfill.connect_database(Path(directory) / "scan.sqlite3")
            try:
                self.insert_candidate(connection)
                client = mock.Mock()
                client.get.return_value = (404, "", {})
                row = connection.execute("SELECT * FROM papers WHERE arxiv_id='2608.99991'").fetchone()
                status = backfill.scan_one(
                    connection, client, row, incremental=True,
                    now=dt.datetime(2026, 8, 15, tzinfo=dt.timezone.utc),
                )
                self.assertEqual(status, "html_unavailable")
            finally:
                connection.close()

    def test_network_error_waits_until_next_day_and_limit_preserves_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = backfill.connect_database(Path(directory) / "scan.sqlite3")
            try:
                for suffix in ("991", "992", "993"):
                    self.insert_candidate(connection, f"2608.99{suffix[-1]}91")
                client = mock.Mock()
                client.get.side_effect = OSError("offline")
                now = dt.datetime(2026, 8, 13, tzinfo=dt.timezone.utc)
                self.assertEqual(backfill.scan_incremental(connection, client, limit=1, now=now), {"retry": 1})
                statuses = connection.execute(
                    "SELECT scan_status, COUNT(*) AS count FROM papers GROUP BY scan_status"
                ).fetchall()
                self.assertEqual({row["scan_status"]: row["count"] for row in statuses}, {"pending": 2, "retry": 1})
                self.assertEqual(backfill.scan_incremental(connection, client, limit=3, now=now), {"retry": 2})
                self.assertEqual(client.get.call_count, 3)
            finally:
                connection.close()

    def test_openalex_failure_preserves_state_and_scans_due_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.jsonl"
            self.write_state(state, [self.record(
                "2608.00001", "retry", nextRetryAt="2026-08-01T00:00:00Z", httpStatus=None,
            )])
            connection = backfill.connect_database(Path(directory) / "scan.sqlite3")
            client = mock.Mock()
            client.get.side_effect = OSError("OAI offline")
            try:
                with mock.patch.object(backfill, "prefilter_openalex", side_effect=OSError("offline")), \
                     mock.patch.object(backfill, "merge_matches", return_value=(0, 0)):
                    result = backfill.run_incremental(
                        connection, client, state, "2026-04-01", "2026-08-13", 300,
                    )
                self.assertEqual(result["discovery"], "unavailable (OSError)")
                client.get.assert_called_once()
                self.assertEqual(len(backfill.load_state_file(state)), 1)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
