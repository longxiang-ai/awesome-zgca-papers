#!/usr/bin/env python3
"""Polite, resumable arXiv HTML affiliation backfill.

The initial historical scan is intentionally local: its SQLite checkpoint is
ignored by Git, while verified matches are merged into the public Work index.
All arXiv requests are sequential and spaced by at least three seconds.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import http.client
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import pipeline  # noqa: E402

ROOT = SCRIPT_DIR.parent
DEFAULT_DB = ROOT / "var" / "arxiv-html-scan" / "checkpoint.sqlite3"
PARTNER_CONFIG = ROOT / "data" / "arxiv-partner-institutions.json"
DEFAULT_START = "2024-06-01"
DEFAULT_REQUEST_TIMEOUT = 20.0
MAX_SCAN_ATTEMPTS = 3
OAI_BASE = "https://oaipmh.arxiv.org/oai"
HTML_BASE = "https://arxiv.org/html"
OAI_NS = "http://www.openarchives.org/OAI/2.0/"
ARXIV_NS = "http://arxiv.org/OAI/arXiv/"
USER_AGENT = "awesome-zgca-papers-html-backfill/1.0 (https://github.com/longxiang-ai/awesome-zgca-papers)"
ARXIV_SOURCE_ID = "S4306400194"
TRANSIENT_STATUS = {429, 500, 502, 503, 504}


class AffiliationBlockParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.buffer: list[str] = []
        self.blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = dict(attrs).get("class") or ""
        if self.depth:
            self.depth += 1
        elif {"ltx_role_affiliation", "ltx_role_author"}.intersection(classes.split()):
            self.depth = 1
            self.buffer = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.depth and tag.lower() == "br":
            self.buffer.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if not self.depth:
            return
        self.depth -= 1
        if not self.depth:
            block = pipeline.normalize_space(" ".join(self.buffer))
            if block:
                self.blocks.append(block)
            self.buffer = []

    def handle_data(self, data: str) -> None:
        if self.depth and pipeline.normalize_space(data):
            self.buffer.append(data)


def extract_affiliation_blocks(html_source: str) -> list[str]:
    parser = AffiliationBlockParser()
    parser.feed(html_source)
    return list(dict.fromkeys(parser.blocks))


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS papers (
            arxiv_id TEXT PRIMARY KEY,
            created TEXT NOT NULL,
            updated TEXT,
            title TEXT NOT NULL,
            authors_json TEXT NOT NULL,
            categories TEXT NOT NULL,
            abstract TEXT NOT NULL,
            doi TEXT,
            journal_ref TEXT,
            comments TEXT,
            prefilter_source TEXT,
            prefilter_institutions_json TEXT NOT NULL DEFAULT '[]',
            scan_status TEXT NOT NULL DEFAULT 'pending',
            html_url TEXT,
            http_status INTEGER,
            matches_json TEXT NOT NULL DEFAULT '[]',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_scanned_at TEXT,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS papers_scan_queue
            ON papers(scan_status, created, arxiv_id);
        CREATE INDEX IF NOT EXISTS papers_scan_queue_by_id
            ON papers(scan_status, arxiv_id);
        CREATE TABLE IF NOT EXISTS scan_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS partner_institutions (
            name TEXT PRIMARY KEY,
            openalex_id TEXT,
            display_name TEXT,
            ror TEXT,
            resolved_at TEXT
        );
        """
    )
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(papers)").fetchall()}
    if "prefilter_source" not in columns:
        connection.execute("ALTER TABLE papers ADD COLUMN prefilter_source TEXT")
    if "prefilter_institutions_json" not in columns:
        connection.execute("ALTER TABLE papers ADD COLUMN prefilter_institutions_json TEXT NOT NULL DEFAULT '[]'")
    connection.commit()
    return connection


def get_meta(connection: sqlite3.Connection, key: str, default: str = "") -> str:
    row = connection.execute("SELECT value FROM scan_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO scan_meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def request_openalex(url: str) -> dict[str, Any]:
    for attempt in range(5):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in TRANSIENT_STATUS or attempt == 4:
                raise
            wait = PacedClient._retry_after(error.headers, attempt) + random.uniform(0, 2)
            print(f"[openalex backoff] HTTP {error.code}; pausing {wait:.0f}s")
            time.sleep(wait)
    raise RuntimeError("unreachable")


def load_partner_config() -> dict[str, Any]:
    return json.loads(PARTNER_CONFIG.read_text(encoding="utf-8"))


def resolve_partner_institution(connection: sqlite3.Connection, institution: dict[str, Any]) -> dict[str, str]:
    cached = connection.execute(
        "SELECT * FROM partner_institutions WHERE name = ? AND openalex_id IS NOT NULL",
        (institution["name"],),
    ).fetchone()
    if cached:
        return dict(cached)
    params = {"search": institution["name"], "filter": "country_code:CN", "per-page": "10"}
    api_key = os.getenv("OPENALEX_API_KEY")
    if api_key:
        params["api_key"] = api_key
    payload = request_openalex("https://api.openalex.org/institutions?" + urllib.parse.urlencode(params))
    expected = pipeline.normalize_space(institution["name"]).casefold()
    aliases = {expected, *(pipeline.normalize_space(alias).casefold() for alias in institution.get("aliases", []))}
    candidates = [
        item for item in payload.get("results", [])
        if pipeline.normalize_space(item.get("display_name")).casefold() in aliases
    ]
    if not candidates:
        raise ValueError(f"Could not resolve partner institution: {institution['name']}")
    selected = candidates[0]
    resolved = {
        "name": institution["name"],
        "openalex_id": selected["id"].rsplit("/", 1)[-1],
        "display_name": selected["display_name"],
        "ror": selected.get("ror") or "",
        "resolved_at": utc_now(),
    }
    connection.execute(
        """
        INSERT INTO partner_institutions(name, openalex_id, display_name, ror, resolved_at)
        VALUES (:name, :openalex_id, :display_name, :ror, :resolved_at)
        ON CONFLICT(name) DO UPDATE SET
            openalex_id = excluded.openalex_id,
            display_name = excluded.display_name,
            ror = excluded.ror,
            resolved_at = excluded.resolved_at
        """,
        resolved,
    )
    connection.commit()
    time.sleep(1.0)
    return resolved


def abstract_from_inverted_index(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    positioned = [(position, word) for word, positions in index.items() for position in positions]
    return pipeline.normalize_space(" ".join(word for _, word in sorted(positioned)))


def arxiv_ids_from_openalex(record: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for location in record.get("locations", []):
        values.extend([location.get("landing_page_url") or "", location.get("pdf_url") or ""])
    combined = " ".join(values)
    return list(dict.fromkeys(re.findall(r"arxiv\.org/(?:abs|pdf|html)/([0-9]{4}\.[0-9]{4,5})", combined, re.I)))


def id_is_in_date_window(arxiv_id: str, start: str, until: str) -> bool:
    match = re.fullmatch(r"(\d{2})(\d{2})\.\d{4,5}", arxiv_id)
    if not match:
        return False
    year = 2000 + int(match.group(1))
    month = int(match.group(2))
    first_day = f"{year:04d}-{month:02d}-01"
    return first_day >= start[:7] + "-01" and first_day <= until[:7] + "-01"


def upsert_openalex_work(
    connection: sqlite3.Connection,
    record: dict[str, Any],
    partner: dict[str, str],
    start: str,
    until: str,
) -> int:
    inserted = 0
    authors = [{"name": pipeline.normalize_space(item.get("author", {}).get("display_name"))} for item in record.get("authorships", [])]
    raw_affiliations = [
        pipeline.normalize_space(raw)
        for authorship in record.get("authorships", [])
        if any(item.get("id", "").rsplit("/", 1)[-1] == partner["openalex_id"] for item in authorship.get("institutions", []))
        for raw in authorship.get("raw_affiliation_strings", [])
        if pipeline.normalize_space(raw)
    ]
    partner_evidence = {
        "name": partner["display_name"],
        "openalexId": partner["openalex_id"],
        "rawAffiliations": list(dict.fromkeys(raw_affiliations)),
    }
    publication_date = record.get("publication_date") or start
    for arxiv_id in arxiv_ids_from_openalex(record):
        if not id_is_in_date_window(arxiv_id, start, until):
            continue
        before = connection.execute("SELECT 1 FROM papers WHERE arxiv_id = ?", (arxiv_id,)).fetchone()
        existing = connection.execute(
            "SELECT prefilter_institutions_json FROM papers WHERE arxiv_id = ?", (arxiv_id,)
        ).fetchone()
        partners = json.loads(existing["prefilter_institutions_json"]) if existing else []
        if not any(item.get("openalexId") == partner["openalex_id"] for item in partners):
            partners.append(partner_evidence)
        connection.execute(
            """
            INSERT INTO papers(
                arxiv_id, created, updated, title, authors_json, categories, abstract, doi,
                journal_ref, comments, prefilter_source, prefilter_institutions_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(arxiv_id) DO UPDATE SET
                title = excluded.title,
                authors_json = excluded.authors_json,
                abstract = CASE WHEN length(excluded.abstract) > length(papers.abstract) THEN excluded.abstract ELSE papers.abstract END,
                doi = COALESCE(excluded.doi, papers.doi),
                prefilter_source = excluded.prefilter_source,
                prefilter_institutions_json = excluded.prefilter_institutions_json
            """,
            (
                arxiv_id, publication_date, publication_date, pipeline.normalize_space(record.get("title")),
                json.dumps(authors, ensure_ascii=False), "", abstract_from_inverted_index(record.get("abstract_inverted_index")),
                (record.get("doi") or "").replace("https://doi.org/", "") or None,
                "", "", "OpenAlex partner institution prefilter", json.dumps(partners, ensure_ascii=False),
            ),
        )
        inserted += int(before is None)
    return inserted


def prefilter_openalex(
    connection: sqlite3.Connection,
    start: str,
    until: str,
    max_institutions: int = 0,
    max_pages: int = 0,
) -> dict[str, int]:
    config = load_partner_config()
    institutions = config["institutions"][:max_institutions or None]
    total_added = 0
    completed = 0
    for institution in institutions:
        partner = resolve_partner_institution(connection, institution)
        state_key = f"openalex_complete:{partner['openalex_id']}:{start}:{until}"
        if get_meta(connection, state_key) == "true":
            completed += 1
            continue
        cursor_key = f"openalex_cursor:{partner['openalex_id']}:{start}:{until}"
        cursor = get_meta(connection, cursor_key, "*") or "*"
        pages = 0
        while not max_pages or pages < max_pages:
            filters = (
                f"institutions.id:{partner['openalex_id']},"
                f"from_publication_date:{start},to_publication_date:{until},"
                f"locations.source.id:{ARXIV_SOURCE_ID}"
            )
            params = {
                "filter": filters,
                "per-page": "200",
                "cursor": cursor,
                "select": "id,title,doi,publication_date,authorships,locations,abstract_inverted_index",
            }
            api_key = os.getenv("OPENALEX_API_KEY")
            if api_key:
                params["api_key"] = api_key
            payload = request_openalex("https://api.openalex.org/works?" + urllib.parse.urlencode(params))
            added = sum(upsert_openalex_work(connection, record, partner, start, until) for record in payload.get("results", []))
            total_added += added
            connection.commit()
            pages += 1
            cursor = payload.get("meta", {}).get("next_cursor") or ""
            set_meta(connection, cursor_key, cursor)
            connection.commit()
            print(f"[prefilter] {partner['display_name']} page={pages} new_arxiv_ids={added}")
            if not cursor or not payload.get("results"):
                set_meta(connection, state_key, "true")
                connection.commit()
                completed += 1
                break
            time.sleep(1.0)
    return {"institutions": len(institutions), "completed": completed, "new_arxiv_ids": total_added}


class PacedClient:
    def __init__(
        self,
        connection: sqlite3.Connection,
        delay: float = 3.5,
        jitter: float = 0.5,
        rest_every: int = 500,
        rest_seconds: float = 300.0,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        if delay < 3.0:
            raise ValueError("arXiv requests must be spaced by at least 3 seconds")
        self.connection = connection
        self.delay = delay
        self.jitter = max(0.0, jitter)
        self.rest_every = max(0, rest_every)
        self.rest_seconds = max(0.0, rest_seconds)
        self.request_timeout = max(5.0, request_timeout)
        self.request_count = 0

    def _pace(self) -> None:
        last_request = float(get_meta(self.connection, "last_arxiv_request_epoch", "0") or 0)
        defer_until = float(get_meta(self.connection, "arxiv_defer_until_epoch", "0") or 0)
        minimum_wait = self.delay + random.uniform(0, self.jitter)
        remaining = max(minimum_wait - (time.time() - last_request), defer_until - time.time())
        if remaining > 0:
            time.sleep(remaining)
        if self.rest_every and self.request_count and self.request_count % self.rest_every == 0:
            print(f"[rest] {self.request_count} requests completed; pausing {self.rest_seconds:.0f}s")
            time.sleep(self.rest_seconds)

    @staticmethod
    def _retry_after(headers: Any, attempt: int) -> float:
        raw = headers.get("Retry-After") if headers else None
        if raw:
            try:
                return max(3.0, float(raw))
            except ValueError:
                parsed = email.utils.parsedate_to_datetime(raw)
                return max(3.0, parsed.timestamp() - time.time())
        return min(900.0, 30.0 * (2 ** attempt))

    def get(self, url: str, retries: int = 5) -> tuple[int, str, dict[str, str]]:
        for attempt in range(retries + 1):
            self._pace()
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xml;q=0.9,*/*;q=0.5"},
            )
            set_meta(self.connection, "last_arxiv_request_epoch", str(time.time()))
            self.connection.commit()
            self.request_count += 1
            try:
                with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                    body = response.read().decode("utf-8", errors="replace")
                    return response.status, body, dict(response.headers.items())
            except urllib.error.HTTPError as error:
                if error.code not in TRANSIENT_STATUS or attempt == retries:
                    if error.code in TRANSIENT_STATUS:
                        wait = self._retry_after(error.headers, attempt) + random.uniform(0, 3)
                        set_meta(self.connection, "arxiv_defer_until_epoch", str(time.time() + wait))
                        self.connection.commit()
                        print(f"[defer] HTTP {error.code}; next request will wait {wait:.0f}s")
                    body = error.read().decode("utf-8", errors="replace")
                    return error.code, body, dict(error.headers.items())
                wait = self._retry_after(error.headers, attempt) + random.uniform(0, 3)
                print(f"[backoff] HTTP {error.code}; pausing {wait:.0f}s before retry")
                time.sleep(wait)
            except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
                if attempt == retries:
                    raise
                wait = min(900.0, 30.0 * (2 ** attempt)) + random.uniform(0, 3)
                print(f"[backoff] {type(error).__name__}; pausing {wait:.0f}s before retry")
                time.sleep(wait)
        raise RuntimeError("unreachable")


def text(node: ET.Element | None, name: str) -> str:
    if node is None:
        return ""
    return pipeline.normalize_space(node.findtext(f"{{{ARXIV_NS}}}{name}", ""))


def parse_oai_records(payload: str) -> tuple[list[dict[str, Any]], str, str]:
    root = ET.fromstring(payload)
    error_node = root.find(f"{{{OAI_NS}}}error")
    if error_node is not None:
        raise ValueError(f"OAI {error_node.attrib.get('code', 'error')}: {pipeline.normalize_space(error_node.text)}")
    records: list[dict[str, Any]] = []
    last_datestamp = ""
    for record in root.findall(f".//{{{OAI_NS}}}record"):
        header = record.find(f"{{{OAI_NS}}}header")
        if header is None or header.attrib.get("status") == "deleted":
            continue
        last_datestamp = max(last_datestamp, pipeline.normalize_space(header.findtext(f"{{{OAI_NS}}}datestamp", "")))
        metadata = record.find(f"{{{OAI_NS}}}metadata")
        arxiv = metadata.find(f"{{{ARXIV_NS}}}arXiv") if metadata is not None else None
        if arxiv is None:
            continue
        authors = []
        for author in arxiv.findall(f".//{{{ARXIV_NS}}}author"):
            name = pipeline.normalize_space(" ".join(filter(None, [
                author.findtext(f"{{{ARXIV_NS}}}forenames", ""),
                author.findtext(f"{{{ARXIV_NS}}}keyname", ""),
                author.findtext(f"{{{ARXIV_NS}}}suffix", ""),
            ])))
            if name:
                authors.append({"name": name})
        records.append({
            "arxiv_id": text(arxiv, "id").split("v", 1)[0],
            "created": text(arxiv, "created"),
            "updated": text(arxiv, "updated"),
            "title": text(arxiv, "title"),
            "authors": authors,
            "categories": text(arxiv, "categories"),
            "abstract": text(arxiv, "abstract"),
            "doi": text(arxiv, "doi"),
            "journal_ref": text(arxiv, "journal-ref"),
            "comments": text(arxiv, "comments"),
        })
    token_node = root.find(f".//{{{OAI_NS}}}resumptionToken")
    token = pipeline.normalize_space(token_node.text) if token_node is not None else ""
    return records, token, last_datestamp


def upsert_records(connection: sqlite3.Connection, records: Iterable[dict[str, Any]], start: str) -> int:
    added = 0
    for record in records:
        if not record["arxiv_id"] or record["created"] < start:
            continue
        before = connection.total_changes
        connection.execute(
            """
            INSERT INTO papers(
                arxiv_id, created, updated, title, authors_json, categories, abstract, doi, journal_ref, comments
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(arxiv_id) DO UPDATE SET
                updated = excluded.updated,
                title = excluded.title,
                authors_json = excluded.authors_json,
                categories = excluded.categories,
                abstract = excluded.abstract,
                doi = excluded.doi,
                journal_ref = excluded.journal_ref,
                comments = excluded.comments
            """,
            (
                record["arxiv_id"], record["created"], record["updated"], record["title"],
                json.dumps(record["authors"], ensure_ascii=False), record["categories"], record["abstract"],
                record["doi"], record["journal_ref"], record["comments"],
            ),
        )
        added += int(connection.total_changes > before)
    return added


def harvest(
    connection: sqlite3.Connection,
    client: PacedClient,
    start: str,
    until: str,
    max_pages: int = 0,
) -> dict[str, int]:
    stored_start = get_meta(connection, "harvest_start")
    stored_until = get_meta(connection, "harvest_until")
    token = get_meta(connection, "harvest_token") if stored_start == start and stored_until == until else ""
    resume_from = get_meta(connection, "harvest_resume_from", start) if stored_start == start else start
    if stored_start != start or stored_until != until:
        set_meta(connection, "harvest_start", start)
        set_meta(connection, "harvest_until", until)
        set_meta(connection, "harvest_token", "")
        set_meta(connection, "harvest_resume_from", start)
        set_meta(connection, "harvest_complete", "false")
        connection.commit()
    pages = 0
    seen = 0
    while not max_pages or pages < max_pages:
        if token:
            query = urllib.parse.urlencode({"verb": "ListRecords", "resumptionToken": token})
        else:
            query = urllib.parse.urlencode({
                "verb": "ListRecords", "metadataPrefix": "arXiv", "from": resume_from, "until": until,
            })
        status, payload, _ = client.get(f"{OAI_BASE}?{query}")
        if status != 200:
            raise RuntimeError(f"OAI-PMH returned HTTP {status}")
        try:
            records, next_token, last_datestamp = parse_oai_records(payload)
        except ValueError as error:
            if token and "badResumptionToken" in str(error):
                token = ""
                resume_from = get_meta(connection, "harvest_last_datestamp", start) or start
                set_meta(connection, "harvest_token", "")
                set_meta(connection, "harvest_resume_from", resume_from)
                connection.commit()
                print(f"[resume] OAI token expired; restarting at {resume_from} with duplicate-safe upserts")
                continue
            raise
        seen += upsert_records(connection, records, start)
        pages += 1
        token = next_token
        set_meta(connection, "harvest_token", token)
        if last_datestamp:
            set_meta(connection, "harvest_last_datestamp", last_datestamp)
        set_meta(connection, "harvest_pages", str(int(get_meta(connection, "harvest_pages", "0")) + 1))
        connection.commit()
        total = connection.execute("SELECT COUNT(*) AS count FROM papers").fetchone()["count"]
        print(f"[harvest] page={pages} candidates={total} new_or_updated={seen}")
        if not token:
            set_meta(connection, "harvest_complete", "true")
            set_meta(connection, "harvest_completed_at", utc_now())
            connection.commit()
            break
    return {"pages": pages, "stored": seen}


def get_single_record(connection: sqlite3.Connection, client: PacedClient, arxiv_id: str) -> None:
    query = urllib.parse.urlencode({
        "verb": "GetRecord", "identifier": f"oai:arXiv.org:{arxiv_id}", "metadataPrefix": "arXiv",
    })
    status, payload, _ = client.get(f"{OAI_BASE}?{query}")
    if status != 200:
        raise RuntimeError(f"OAI-PMH returned HTTP {status} for {arxiv_id}")
    records, _, _ = parse_oai_records(payload)
    if not records:
        raise ValueError(f"No OAI metadata found for {arxiv_id}")
    upsert_records(connection, records, "0000-01-01")
    connection.execute(
        """
        UPDATE papers SET
            scan_status = 'pending', error = NULL, prefilter_source = 'manual verification',
            prefilter_institutions_json = '[]'
        WHERE arxiv_id = ?
        """,
        (arxiv_id,),
    )
    connection.commit()


def find_institution_matches(html_source: str) -> list[dict[str, str]]:
    matches: list[dict[str, str]] = []
    plain_source = pipeline.clean_html_text(html_source)
    plain_lowered = plain_source.lower()
    seen: set[tuple[str, str, str]] = set()
    for block in extract_affiliation_blocks(html_source):
        for institution_id, alias in pipeline.match_institutions(block):
            key = (institution_id, alias.lower(), "exact-affiliation")
            if key in seen:
                continue
            seen.add(key)
            segments = [pipeline.normalize_space(segment) for segment in block.split("|")]
            affiliation_segment = next(
                (segment for segment in segments if alias.lower() in segment.lower()),
                block,
            )
            matches.append({
                "institution": institution_id,
                "alias": alias,
                "level": "exact-affiliation",
                "snippet": affiliation_segment[:900],
            })
    for institution_id, institution in pipeline.INSTITUTIONS.items():
        for alias in institution["aliases"]:
            if (institution_id, alias.lower(), "exact-affiliation") in seen:
                continue
            position = plain_lowered.find(alias.lower())
            if position < 0:
                continue
            snippet = plain_source[max(0, position - 180):position + len(alias) + 180]
            local_matches = pipeline.match_institutions(snippet)
            if any(item[0] == institution_id and item[1].lower() == alias.lower() for item in local_matches):
                key = (institution_id, alias.lower(), "association")
                if key not in seen:
                    seen.add(key)
                    matches.append({
                        "institution": institution_id,
                        "alias": alias,
                        "level": "association",
                        "snippet": snippet[:500],
                    })
    return matches


def scan_one(connection: sqlite3.Connection, client: PacedClient, row: sqlite3.Row) -> str:
    arxiv_id = row["arxiv_id"]
    html_url = f"{HTML_BASE}/{arxiv_id}v1"
    try:
        # A broken connection must not hold the whole queue in exponential backoff.
        # Record one attempt, move the paper behind all pending work, and retry it
        # after the first pass. Server-directed HTTP backoff is still respected.
        status, payload, _ = client.get(html_url, retries=0)
        if status == 200:
            matches = find_institution_matches(payload)
            scan_status = "matched" if matches else "no_match"
            error = None
        elif status in {404, 406}:
            matches = []
            scan_status = "html_unavailable"
            error = f"HTTP {status}"
        else:
            matches = []
            scan_status = "retry"
            error = f"HTTP {status}"
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exception:
        status = None
        matches = []
        scan_status = "retry"
        error = f"{type(exception).__name__}: {exception}"
    connection.execute(
        """
        UPDATE papers SET
            scan_status = ?, html_url = ?, http_status = ?, matches_json = ?, attempts = attempts + 1,
            last_scanned_at = ?, error = ?
        WHERE arxiv_id = ?
        """,
        (scan_status, html_url, status, json.dumps(matches, ensure_ascii=False), utc_now(), error, arxiv_id),
    )
    connection.commit()
    return scan_status


def candidate_from_row(row: sqlite3.Row, include_association: bool = False) -> dict[str, Any] | None:
    matches = [
        item for item in json.loads(row["matches_json"])
        if item["level"] == "exact-affiliation" or include_association
    ]
    if not matches:
        return None
    institutions = list(dict.fromkeys(item["institution"] for item in matches))
    evidence = [{
        "level": item["level"],
        "institution": item["institution"],
        "matchedText": item["snippet"],
        "source": "arXiv HTML v1",
        "sourceUrl": row["html_url"],
    } for item in matches]
    published_at = row["created"]
    venue = row["journal_ref"] or "arXiv"
    status = "published" if row["journal_ref"] else "preprint"
    identifiers = {"arxiv": row["arxiv_id"], "doi": f"10.48550/arXiv.{row['arxiv_id']}"}
    if row["doi"]:
        identifiers["publishedDoi"] = row["doi"].lower()
    candidate = {
        "type": "article" if status == "published" else "preprint",
        "title": row["title"],
        "authors": json.loads(row["authors_json"]),
        "institutions": institutions,
        "rawAffiliations": list(dict.fromkeys(item["snippet"] for item in matches)),
        "relationType": "affiliation" if any(item["level"] == "exact-affiliation" for item in matches) else "association",
        "publishedAt": published_at,
        "year": int(published_at[:4]),
        "venue": venue,
        "status": status,
        "topics": row["categories"].split(),
        "abstract": pipeline.clean_abstract(row["abstract"]),
        "identifiers": identifiers,
        "links": [
            {"label": "arXiv", "url": f"https://arxiv.org/abs/{row['arxiv_id']}"},
            {"label": "HTML", "url": row["html_url"]},
            {"label": "PDF", "url": f"https://arxiv.org/pdf/{row['arxiv_id']}"},
        ],
        "versions": [{"label": "arXiv v1", "url": f"https://arxiv.org/abs/{row['arxiv_id']}v1"}],
        "evidence": evidence,
        "sources": ["arXiv", "arXiv HTML"],
    }
    candidate["id"] = pipeline.stable_id(candidate)
    return candidate


def merge_matches(connection: sqlite3.Connection, include_association: bool = False) -> tuple[int, int]:
    rows = connection.execute("SELECT * FROM papers WHERE scan_status = 'matched' ORDER BY created").fetchall()
    candidates = [candidate for row in rows if (candidate := candidate_from_row(row, include_association))]
    existing = pipeline.load_works()
    merged, added = pipeline.deduplicate(existing, candidates)
    coverage_path = pipeline.PUBLIC / "data" / "coverage.json"
    coverage = {}
    if coverage_path.exists():
        coverage = json.loads(coverage_path.read_text(encoding="utf-8")).get("sources", {})
    stats = scan_stats(connection)
    coverage["arxiv_html_backfill"] = (
        f"local checkpoint {stats['scanned']}/{stats['total']}; "
        f"{stats['exact_matches']} exact affiliation matches"
    )
    changed = pipeline.export_outputs(merged, coverage)
    print(f"[merge] candidates={len(candidates)} added={added} updated_files={len(changed)}")
    return len(candidates), added


def scan(
    connection: sqlite3.Connection,
    client: PacedClient,
    limit: int = 0,
    include_association: bool = False,
    merge: bool = True,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    processed = 0
    while not limit or processed < limit:
        batch_size = min(100, limit - processed) if limit else 100
        rows = connection.execute(
            """
            SELECT * FROM papers
            WHERE scan_status = 'pending' AND attempts < ? AND prefilter_source IS NOT NULL
            ORDER BY arxiv_id LIMIT ?
            """,
            (MAX_SCAN_ATTEMPTS, batch_size),
        ).fetchall()
        if not rows:
            rows = connection.execute(
                """
                SELECT * FROM papers
                WHERE scan_status = 'retry' AND attempts < ? AND prefilter_source IS NOT NULL
                ORDER BY last_scanned_at, arxiv_id LIMIT ?
                """,
                (MAX_SCAN_ATTEMPTS, batch_size),
            ).fetchall()
        if not rows:
            break
        for row in rows:
            result = scan_one(connection, client, row)
            processed += 1
            counts[result] = counts.get(result, 0) + 1
            if result == "matched" or processed % 25 == 0:
                print(f"[scan] processed={processed} {row['arxiv_id']} -> {result}")
        if merge and processed % 500 == 0:
            merge_matches(connection, include_association)
    if merge:
        merge_matches(connection, include_association)
    return counts


def scan_ids(
    connection: sqlite3.Connection,
    client: PacedClient,
    ids: Iterable[str],
    merge: bool = True,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for arxiv_id in ids:
        row = connection.execute("SELECT * FROM papers WHERE arxiv_id = ?", (arxiv_id,)).fetchone()
        if row is None:
            continue
        result = scan_one(connection, client, row)
        counts[result] = counts.get(result, 0) + 1
        print(f"[verify] {arxiv_id} -> {result}")
    if merge:
        merge_matches(connection)
    return counts


def scan_stats(connection: sqlite3.Connection) -> dict[str, Any]:
    total = connection.execute("SELECT COUNT(*) AS count FROM papers").fetchone()["count"]
    status_rows = connection.execute(
        "SELECT scan_status, COUNT(*) AS count FROM papers GROUP BY scan_status"
    ).fetchall()
    statuses = {row["scan_status"]: row["count"] for row in status_rows}
    exact = connection.execute(
        "SELECT COUNT(*) AS count FROM papers WHERE scan_status = 'matched' AND matches_json LIKE '%exact-affiliation%'"
    ).fetchone()["count"]
    retryable = connection.execute(
        "SELECT COUNT(*) AS count FROM papers WHERE scan_status = 'retry' AND attempts < ?",
        (MAX_SCAN_ATTEMPTS,),
    ).fetchone()["count"]
    exhausted = statuses.get("retry", 0) - retryable
    scanned = total - statuses.get("pending", 0) - retryable
    background_pid = int(get_meta(connection, "background_scan_pid", "0") or 0)
    background_running = False
    if background_pid:
        try:
            os.kill(background_pid, 0)
            background_running = True
        except PermissionError:
            background_running = True
        except ProcessLookupError:
            background_running = False
    return {
        "total": total,
        "scanned": scanned,
        "pending": statuses.get("pending", 0),
        "retry": retryable,
        "retry_exhausted": exhausted,
        "matched": statuses.get("matched", 0),
        "exact_matches": exact,
        "no_match": statuses.get("no_match", 0),
        "html_unavailable": statuses.get("html_unavailable", 0),
        "partner_institutions": len(load_partner_config()["institutions"]),
        "resolved_partner_institutions": connection.execute(
            "SELECT COUNT(*) AS count FROM partner_institutions WHERE openalex_id IS NOT NULL"
        ).fetchone()["count"],
        "completed_partner_queries": connection.execute(
            "SELECT COUNT(*) AS count FROM scan_meta WHERE key LIKE 'openalex_complete:%' AND value = 'true'"
        ).fetchone()["count"],
        "background_pid": background_pid or None,
        "background_running": background_running,
    }


def start_background_scan(database: Path, args: argparse.Namespace) -> int:
    check_connection = connect_database(database)
    try:
        existing_pid = int(get_meta(check_connection, "background_scan_pid", "0") or 0)
    finally:
        check_connection.close()
    if existing_pid:
        try:
            os.kill(existing_pid, 0)
            raise RuntimeError(f"A background scan is already running with PID {existing_pid}")
        except PermissionError:
            raise RuntimeError(f"A background scan is already running with PID {existing_pid}")
        except ProcessLookupError:
            pass
    log_path = database.parent / "scan.log"
    command = [
        sys.executable, "-u", str(Path(__file__).resolve()), "--database", str(database), "scan",
        "--limit", str(args.limit), "--delay", str(args.delay), "--jitter", str(args.jitter),
        "--rest-every", str(args.rest_every), "--rest-seconds", str(args.rest_seconds),
        "--request-timeout", str(args.request_timeout),
    ]
    if args.include_association:
        command.append("--include-association")
    environment = dict(os.environ)
    for name in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
        environment.pop(name, None)
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    connection = connect_database(database)
    try:
        set_meta(connection, "background_scan_pid", str(process.pid))
        set_meta(connection, "background_scan_started_at", utc_now())
        connection.commit()
    finally:
        connection.close()
    print(f"Background scan started: pid={process.pid} log={log_path}")
    return process.pid


def pacing_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--delay", type=float, default=3.5, help="Minimum seconds between requests; values below 3 are rejected")
    parser.add_argument("--jitter", type=float, default=0.5, help="Random extra delay per request")
    parser.add_argument("--rest-every", type=int, default=500, help="Take a longer rest after this many requests; 0 disables")
    parser.add_argument("--rest-seconds", type=float, default=300, help="Long-rest duration")
    parser.add_argument(
        "--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT,
        help="Seconds before a stalled request is deferred to the retry queue",
    )


def make_client(connection: sqlite3.Connection, args: argparse.Namespace) -> PacedClient:
    return PacedClient(
        connection, args.delay, args.jitter, args.rest_every, args.rest_seconds, args.request_timeout,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prefilter_parser = subparsers.add_parser("prefilter", help="Build the candidate queue from partner institutions in OpenAlex")
    prefilter_parser.add_argument("--from", dest="start", default=DEFAULT_START)
    prefilter_parser.add_argument("--until", default=dt.datetime.now(dt.timezone.utc).date().isoformat())
    prefilter_parser.add_argument("--institutions", type=int, default=0, help="Maximum partner institutions; 0 means all")
    prefilter_parser.add_argument("--pages", type=int, default=0, help="Maximum pages per institution; 0 means all")

    scan_parser = subparsers.add_parser("scan", help="Scan pending HTML pages in chronological order")
    scan_parser.add_argument("--limit", type=int, default=0, help="Maximum papers; 0 means all pending")
    scan_parser.add_argument("--include-association", action="store_true", help="Publish body-text matches as weak associations")
    scan_parser.add_argument("--no-merge", action="store_true")
    pacing_arguments(scan_parser)

    verify_parser = subparsers.add_parser("verify", help="Fetch metadata and HTML for specific arXiv IDs")
    verify_parser.add_argument("ids", nargs="+")
    verify_parser.add_argument("--no-merge", action="store_true")
    pacing_arguments(verify_parser)

    run_parser = subparsers.add_parser("run", help="Resume partner prefiltering, then scan the historical queue")
    run_parser.add_argument("--from", dest="start", default=DEFAULT_START)
    run_parser.add_argument("--until", default=dt.datetime.now(dt.timezone.utc).date().isoformat())
    run_parser.add_argument("--institutions", type=int, default=0)
    run_parser.add_argument("--prefilter-pages", type=int, default=0)
    run_parser.add_argument("--scan-limit", type=int, default=0)
    run_parser.add_argument("--include-association", action="store_true")
    pacing_arguments(run_parser)

    subparsers.add_parser("status", help="Show checkpoint progress")
    merge_parser = subparsers.add_parser("merge", help="Merge already verified HTML matches")
    merge_parser.add_argument("--include-association", action="store_true")
    start_parser = subparsers.add_parser("start", help="Run the polite HTML queue in the background")
    start_parser.add_argument("--limit", type=int, default=0)
    start_parser.add_argument("--include-association", action="store_true")
    pacing_arguments(start_parser)

    args = parser.parse_args()
    connection = connect_database(args.database)
    try:
        if args.command == "prefilter":
            print(json.dumps(prefilter_openalex(connection, args.start, args.until, args.institutions, args.pages), indent=2))
        elif args.command == "scan":
            print(json.dumps(scan(connection, make_client(connection, args), args.limit, args.include_association, not args.no_merge), indent=2))
        elif args.command == "verify":
            client = make_client(connection, args)
            ids = [arxiv_id.split("v", 1)[0] for arxiv_id in args.ids]
            for arxiv_id in ids:
                get_single_record(connection, client, arxiv_id)
            print(json.dumps(scan_ids(connection, client, ids, merge=not args.no_merge), indent=2))
        elif args.command == "run":
            prefilter_openalex(connection, args.start, args.until, args.institutions, args.prefilter_pages)
            client = make_client(connection, args)
            print(json.dumps(scan(connection, client, args.scan_limit, args.include_association), indent=2))
        elif args.command == "merge":
            merge_matches(connection, args.include_association)
        elif args.command == "start":
            connection.close()
            start_background_scan(args.database, args)
            return
        else:
            stats = scan_stats(connection)
            pending_seconds = (stats["pending"] + stats["retry"]) * 3.75
            stats["minimum_eta_hours"] = round(pending_seconds / 3600, 1)
            print(json.dumps(stats, ensure_ascii=False, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
