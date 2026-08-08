#!/usr/bin/env python3
"""Discover, normalize and export ZGCA/ZGCI research metadata.

The pipeline is deliberately add-only: a temporary source outage can never
erase an existing record. Run `python scripts/pipeline.py fetch` for networked
discovery or `python scripts/pipeline.py build` for deterministic local output.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PUBLIC = ROOT / "public"
USER_AGENT = "awesome-zgca-papers/1.0 (https://github.com/longxiang-ai/awesome-zgca-papers)"

INSTITUTIONS = {
    "zgca": {
        "name": "Zhongguancun Academy",
        "aliases": ["北京中关村学院", "中关村学院", "Zhongguancun Academy", "Beijing Zhongguancun Academy"],
        "restricted": ["ZGCA"],
    },
    "zgci": {
        "name": "Zhongguancun Institute of Artificial Intelligence",
        "aliases": ["中关村人工智能研究院", "Zhongguancun Institute of Artificial Intelligence"],
        "restricted": ["ZGCI"],
    },
}

EXCLUSIONS = [
    "Zhongguancun Science Park",
    "Zhongguancun Hospital",
    "Chinese Academy of Sciences",
    "Zhongguancun Laboratory",
    "Zhongguancun High School",
    "Pinggu Agricultural Zhongguancun College",
]

TYPE_MAP = {
    "journal-article": "article",
    "proceedings-article": "conference",
    "posted-content": "preprint",
    "dataset": "dataset",
    "report": "report",
    "book": "report",
}

ARXIV_NAMESPACE = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
ARXIV_ID_PATTERN = re.compile(r"(?:arxiv\.org/(?:abs|pdf|html)/|arXiv\s*[:：]?\s*)(\d{4}\.\d{4,5})", re.I)


def normalize_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value.lower())


def clean_abstract(value: str | None) -> str:
    text = normalize_space(re.sub(r"<[^>]+>", "", value or ""))
    # Some Crossref records expose only a dangling JATS reference such as "$16".
    return "" if re.fullmatch(r"\$\d+", text) else text


def clean_html_text(value: str | None) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value or "")
    return normalize_space(html.unescape(without_tags))


def extract_arxiv_ids(value: str | None) -> list[str]:
    return list(dict.fromkeys(ARXIV_ID_PATTERN.findall(html.unescape(value or ""))))


def extract_project_links(value: str | None) -> list[dict[str, str]]:
    source = html.unescape(value or "")
    urls = re.findall(r"\]\((https?://[^\s)]+)", source, re.I)
    urls.extend(re.findall(r"href=[\"'](https?://[^\"']+)", source, re.I))
    urls.extend(re.findall(r"(?<![\(\"'=])https?://[^\s\"<>]+", source, re.I))
    links: list[dict[str, str]] = []
    for raw_url in urls:
        url = raw_url.rstrip(".,;:!?，。；：！？)]}）】”’")
        if "img.shields.io/" in url or ")](" in url:
            continue
        if "github.com/" in url:
            label = "Code"
        elif "huggingface.co/datasets/" in url:
            label = "Dataset"
        elif "huggingface.co/" in url:
            label = "Model"
        elif "arxiv.org/" in url:
            continue
        else:
            continue
        item = {"label": label, "url": url}
        if item not in links:
            links.append(item)
    return links


def venue_and_status(value: str | None) -> tuple[str, str]:
    text = clean_html_text(value)
    match = re.search(r"\b(ICML|ICLR|CVPR|ECCV|NeurIPS|ACL|EMNLP|KDD|IROS|ACM\s+MM)\s*['’]?\s*(20\d{2})\b", text, re.I)
    if match:
        return f"{match.group(1).upper()} {match.group(2)}", "published"
    accepted = re.search(r"accepted\s+(?:by|to|at)\s+([^.;\n]{3,80})", text, re.I)
    if accepted:
        return normalize_space(accepted.group(1)), "published"
    return "arXiv", "preprint"


def affiliation_snippet(value: str) -> str:
    for line in value.splitlines():
        if match_institutions(line):
            return clean_html_text(re.sub(r"[#*_`{}^]", "", line))[:500]
    return "Zhongguancun Academy"


def sanitize_links(links: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    """Keep stable, user-facing links and discard badge/dependency URL noise."""
    cleaned: list[dict[str, str]] = []
    seen_labels: set[str] = set()
    for item in links:
        label = normalize_space(item.get("label"))
        url = normalize_space(item.get("url"))
        if not label or not re.match(r"^https?://", url) or "img.shields.io/" in url or ")](" in url:
            continue
        if label == "Code":
            url = url.removesuffix(".git")
        if label in seen_labels:
            continue
        seen_labels.add(label)
        cleaned.append({"label": label, "url": url})
    return cleaned


def match_institutions(text: str, allow_restricted: bool = False) -> list[tuple[str, str]]:
    """Return institution/alias pairs while rejecting geographic false positives."""
    normalized = normalize_space(text)
    lowered = normalized.lower()
    if any(exclusion.lower() in lowered for exclusion in EXCLUSIONS):
        # A true alias may coexist with an excluded institution; only reject when
        # there is no explicit true alias in the same string.
        explicit = any(alias.lower() in lowered for item in INSTITUTIONS.values() for alias in item["aliases"])
        if not explicit:
            return []
    matches: list[tuple[str, str]] = []
    for institution_id, item in INSTITUTIONS.items():
        aliases = list(item["aliases"]) + (list(item["restricted"]) if allow_restricted else [])
        for alias in aliases:
            if alias.lower() in lowered:
                matches.append((institution_id, alias))
                break
    return matches


def stable_id(candidate: dict[str, Any]) -> str:
    identifiers = candidate.get("identifiers", {})
    if identifiers.get("doi"):
        return "doi-" + re.sub(r"[^a-z0-9]+", "-", identifiers["doi"].lower()).strip("-")
    if identifiers.get("arxiv"):
        return "arxiv-" + identifiers["arxiv"].replace(".", "-").lower()
    if identifiers.get("patent"):
        return "patent-" + re.sub(r"[^a-z0-9]+", "-", identifiers["patent"].lower()).strip("-")
    key = f"{normalize_title(candidate.get('title', ''))}|{candidate.get('year', '')}"
    return "work-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def load_works() -> list[dict[str, Any]]:
    jsonl = DATA / "works.jsonl"
    if jsonl.exists():
        return [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    return json.loads((DATA / "works.json").read_text(encoding="utf-8"))


def request_json(url: str, headers: dict[str, str] | None = None) -> Any:
    req_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    req_headers.update(headers or {})
    request = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def request_text(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> str:
    req_headers = {"User-Agent": USER_AGENT}
    req_headers.update(headers or {})
    request = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def year_from_parts(parts: Any) -> int | None:
    try:
        return int(parts[0][0])
    except (TypeError, IndexError, ValueError):
        return None


def evidence_for_affiliations(affiliations: Iterable[str], source: str, source_url: str, level: str = "exact-affiliation") -> tuple[list[str], list[dict[str, str]]]:
    institutions: list[str] = []
    evidence: list[dict[str, str]] = []
    for affiliation in affiliations:
        for institution_id, alias in match_institutions(affiliation):
            if institution_id not in institutions:
                institutions.append(institution_id)
                evidence.append({
                    "level": level,
                    "institution": institution_id,
                    "matchedText": normalize_space(affiliation) or alias,
                    "source": source,
                    "sourceUrl": source_url,
                })
    return institutions, evidence


def arxiv_candidates_for_ids(ids: Iterable[str], contexts: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    requested = list(dict.fromkeys(ids))
    results: list[dict[str, Any]] = []
    for offset in range(0, len(requested), 40):
        batch = requested[offset:offset + 40]
        if not batch:
            continue
        query = urllib.parse.urlencode({"id_list": ",".join(batch), "max_results": str(len(batch))})
        root = ET.fromstring(request_text("https://export.arxiv.org/api/query?" + query))
        for entry in root.findall("atom:entry", ARXIV_NAMESPACE):
            arxiv_id = entry.findtext("atom:id", "", ARXIV_NAMESPACE).rsplit("/", 1)[-1].split("v", 1)[0]
            item_contexts = contexts.get(arxiv_id, [])
            if not item_contexts:
                continue
            institutions: list[str] = []
            evidence: list[dict[str, str]] = []
            raw_affiliations: list[str] = []
            links = [
                {"label": "arXiv", "url": f"https://arxiv.org/abs/{arxiv_id}"},
                {"label": "PDF", "url": f"https://arxiv.org/pdf/{arxiv_id}"},
            ]
            source_names = ["arXiv"]
            context_text = ""
            relation_type = "official-output"
            for context in item_contexts:
                context_text += " " + context.get("text", "")
                source_names.append(context["source"])
                for institution_id in context["institutions"]:
                    if institution_id not in institutions:
                        institutions.append(institution_id)
                    evidence_item = {
                        "level": context["level"], "institution": institution_id,
                        "matchedText": context["matchedText"], "source": context["source"],
                        "sourceUrl": context["sourceUrl"],
                    }
                    if evidence_item not in evidence:
                        evidence.append(evidence_item)
                raw_affiliations.append(context["matchedText"])
                for link in context.get("links", []):
                    if link not in links:
                        links.append(link)
                if context["level"] in {"structured", "exact-affiliation"}:
                    relation_type = "affiliation"
            published = entry.findtext("atom:published", "", ARXIV_NAMESPACE)[:10]
            year = int(published[:4])
            comment = entry.findtext("arxiv:comment", "", ARXIV_NAMESPACE)
            venue, status = venue_and_status(context_text + " " + comment)
            candidate = {
                "type": "conference" if status == "published" else "preprint",
                "title": normalize_space(entry.findtext("atom:title", "", ARXIV_NAMESPACE)),
                "authors": [{"name": normalize_space(node.findtext("atom:name", "", ARXIV_NAMESPACE))} for node in entry.findall("atom:author", ARXIV_NAMESPACE)],
                "institutions": institutions, "rawAffiliations": list(dict.fromkeys(raw_affiliations)),
                "relationType": relation_type, "publishedAt": published, "year": year,
                "venue": venue, "status": status,
                "topics": [node.attrib.get("term", "") for node in entry.findall("atom:category", ARXIV_NAMESPACE)],
                "abstract": clean_abstract(entry.findtext("atom:summary", "", ARXIV_NAMESPACE)),
                "identifiers": {"arxiv": arxiv_id, "doi": f"10.48550/arXiv.{arxiv_id}"},
                "links": links,
                "versions": [{"label": "arXiv v1", "url": f"https://arxiv.org/abs/{arxiv_id}v1"}],
                "evidence": evidence, "sources": list(dict.fromkeys(source_names)),
            }
            candidate["id"] = stable_id(candidate)
            results.append(candidate)
    return results


def crossref_candidate_for_doi(doi: str, context: dict[str, Any]) -> dict[str, Any] | None:
    encoded = urllib.parse.quote(doi, safe="")
    record = request_json(f"https://api.crossref.org/works/{encoded}").get("message", {})
    title = normalize_space((record.get("title") or [""])[0])
    if not title:
        return None
    published = record.get("published", {}).get("date-parts") or record.get("published-online", {}).get("date-parts")
    year = year_from_parts(published) or int(context["publishedAt"][:4])
    source_url = f"https://doi.org/{doi}"
    candidate = {
        "type": TYPE_MAP.get(record.get("type"), "article"), "title": title,
        "authors": [{"name": normalize_space(" ".join([author.get("given", ""), author.get("family", "")]))} for author in record.get("author", [])],
        "institutions": context["institutions"], "rawAffiliations": [context["matchedText"]],
        "relationType": "official-output", "publishedAt": f"{year}-01-01", "year": year,
        "venue": normalize_space((record.get("container-title") or ["Crossref"])[0]), "status": "published",
        "topics": record.get("subject", [])[:6], "abstract": clean_abstract(record.get("abstract")),
        "identifiers": {"doi": doi.lower()},
        "links": [{"label": "DOI", "url": source_url}, {"label": "Official", "url": context["sourceUrl"]}],
        "versions": [{"label": "Publisher version", "url": source_url}],
        "evidence": [{
            "level": context["level"], "institution": institution_id,
            "matchedText": context["matchedText"], "source": context["source"], "sourceUrl": context["sourceUrl"],
        } for institution_id in context["institutions"]],
        "sources": ["Crossref", context["source"]],
    }
    candidate["id"] = stable_id(candidate)
    return candidate


def crossref_adapter(since: str | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in INSTITUTIONS.values():
        for alias in item["aliases"]:
            params = {"query.affiliation": alias, "rows": "100", "select": "DOI,title,author,published,container-title,type,URL,abstract,subject"}
            if since:
                params["filter"] = f"from-index-date:{since}"
            url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
            payload = request_json(url)
            for record in payload.get("message", {}).get("items", []):
                affiliations = [normalize_space(aff.get("name")) for author in record.get("author", []) for aff in author.get("affiliation", [])]
                doi = record.get("DOI", "").lower()
                source_url = f"https://doi.org/{doi}" if doi else record.get("URL", "")
                institutions, evidence = evidence_for_affiliations(affiliations, "Crossref", source_url)
                if not evidence:
                    continue
                published = record.get("published", {}).get("date-parts") or record.get("published-online", {}).get("date-parts")
                year = year_from_parts(published) or dt.datetime.now(dt.timezone.utc).year
                authors = []
                for author in record.get("author", []):
                    name = normalize_space(" ".join([author.get("given", ""), author.get("family", "")]))
                    author_matches = evidence_for_affiliations([aff.get("name", "") for aff in author.get("affiliation", [])], "Crossref", source_url)[0]
                    authors.append({"name": name, **({"institutions": author_matches} if author_matches else {})})
                candidate = {
                    "type": TYPE_MAP.get(record.get("type"), "article"),
                    "title": normalize_space((record.get("title") or [""])[0]),
                    "authors": authors,
                    "institutions": institutions,
                    "rawAffiliations": affiliations,
                    "relationType": "affiliation",
                    "publishedAt": f"{year}-01-01",
                    "year": year,
                    "venue": normalize_space((record.get("container-title") or ["Crossref"])[0]),
                    "status": "published",
                    "topics": record.get("subject", [])[:6],
                    "abstract": clean_abstract(record.get("abstract")),
                    "identifiers": {"doi": doi} if doi else {},
                    "links": [{"label": "DOI", "url": source_url}],
                    "versions": [{"label": "Publisher version", "url": source_url}],
                    "evidence": evidence,
                    "sources": ["Crossref"],
                }
                candidate["id"] = stable_id(candidate)
                results.append(candidate)
    return results


def europe_pmc_adapter(_: str | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in INSTITUTIONS.values():
        for alias in item["aliases"]:
            query = urllib.parse.urlencode({"query": f'AFF:"{alias}"', "format": "json", "pageSize": "100", "resultType": "core"})
            url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + query
            for record in request_json(url).get("resultList", {}).get("result", []):
                author_list = record.get("authorList", {}).get("author", [])
                affiliations = [normalize_space(aff.get("affiliation")) for info in record.get("affiliationList", {}).get("affiliation", []) for aff in [info]]
                institutions, evidence = evidence_for_affiliations(affiliations, "Europe PMC", f"https://europepmc.org/article/MED/{record.get('pmid', '')}", "structured")
                if not evidence:
                    continue
                doi = record.get("doi", "").lower()
                year = int(record.get("pubYear") or dt.datetime.now(dt.timezone.utc).year)
                candidate = {
                    "type": "article", "title": normalize_space(record.get("title")),
                    "authors": [{"name": normalize_space(author.get("fullName"))} for author in author_list],
                    "institutions": institutions, "rawAffiliations": affiliations, "relationType": "affiliation",
                    "publishedAt": f"{year}-01-01", "year": year, "venue": record.get("journalTitle") or "Europe PMC",
                    "status": "published", "topics": [], "abstract": normalize_space(record.get("abstractText")),
                    "identifiers": {"doi": doi} if doi else {},
                    "links": [{"label": "Europe PMC", "url": f"https://europepmc.org/article/MED/{record.get('pmid', '')}"}],
                    "versions": [], "evidence": evidence, "sources": ["Europe PMC"],
                }
                candidate["id"] = stable_id(candidate)
                results.append(candidate)
    return results


def datacite_adapter(_: str | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in INSTITUTIONS.values():
        for alias in item["aliases"]:
            params = urllib.parse.urlencode({"query": f'creators.affiliation.name:"{alias}"', "page[size]": "100"})
            url = "https://api.datacite.org/dois?" + params
            for wrapped in request_json(url).get("data", []):
                record = wrapped.get("attributes", {})
                affiliations = [normalize_space(aff.get("name") if isinstance(aff, dict) else aff) for creator in record.get("creators", []) for aff in creator.get("affiliation", [])]
                source_url = record.get("url") or f"https://doi.org/{record.get('doi', '')}"
                institutions, evidence = evidence_for_affiliations(affiliations, "DataCite", source_url, "structured")
                if not evidence:
                    continue
                year = int(record.get("publicationYear") or dt.datetime.now(dt.timezone.utc).year)
                resource_type = (record.get("types", {}).get("resourceTypeGeneral") or "").lower()
                kind = "dataset" if resource_type == "dataset" else "preprint"
                candidate = {
                    "type": kind, "title": normalize_space((record.get("titles") or [{}])[0].get("title")),
                    "authors": [{"name": normalize_space(item.get("name"))} for item in record.get("creators", [])],
                    "institutions": institutions, "rawAffiliations": affiliations, "relationType": "affiliation",
                    "publishedAt": f"{year}-01-01", "year": year, "venue": normalize_space(record.get("publisher")) or "DataCite",
                    "status": "released" if kind == "dataset" else "preprint", "topics": [subject.get("subject", "") for subject in record.get("subjects", [])[:6]],
                    "abstract": normalize_space(next((description.get("description", "") for description in record.get("descriptions", []) if description.get("descriptionType") == "Abstract"), "")),
                    "identifiers": {"doi": record.get("doi", "").lower()}, "links": [{"label": "DOI", "url": source_url}],
                    "versions": [], "evidence": evidence, "sources": ["DataCite"],
                }
                candidate["id"] = stable_id(candidate)
                results.append(candidate)
    return results


def arxiv_adapter(_: str | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    namespace = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    for alias in [alias for item in INSTITUTIONS.values() for alias in item["aliases"] if not re.search(r"[\u4e00-\u9fff]", alias)]:
        query = urllib.parse.urlencode({"search_query": f'all:"{alias}"', "start": "0", "max_results": "100", "sortBy": "lastUpdatedDate", "sortOrder": "descending"})
        xml = request_text("https://export.arxiv.org/api/query?" + query)
        root = ET.fromstring(xml)
        for entry in root.findall("atom:entry", namespace):
            combined = " ".join(filter(None, [entry.findtext("atom:title", "", namespace), entry.findtext("atom:summary", "", namespace), entry.findtext("arxiv:comment", "", namespace)]))
            institutions, evidence = evidence_for_affiliations([combined], "arXiv metadata", entry.findtext("atom:id", "", namespace), "association")
            if not evidence:
                continue
            arxiv_id = entry.findtext("atom:id", "", namespace).rsplit("/", 1)[-1].split("v", 1)[0]
            published = entry.findtext("atom:published", "", namespace)[:10]
            year = int(published[:4])
            candidate = {
                "type": "preprint", "title": normalize_space(entry.findtext("atom:title", "", namespace)),
                "authors": [{"name": normalize_space(node.findtext("atom:name", "", namespace))} for node in entry.findall("atom:author", namespace)],
                "institutions": institutions, "rawAffiliations": [alias], "relationType": "affiliation",
                "publishedAt": published, "year": year, "venue": "arXiv", "status": "preprint",
                "topics": [node.attrib.get("term", "") for node in entry.findall("atom:category", namespace)],
                "abstract": normalize_space(entry.findtext("atom:summary", "", namespace)),
                "identifiers": {"arxiv": arxiv_id, "doi": f"10.48550/arXiv.{arxiv_id}"},
                "links": [{"label": "arXiv", "url": f"https://arxiv.org/abs/{arxiv_id}"}, {"label": "PDF", "url": f"https://arxiv.org/pdf/{arxiv_id}"}],
                "versions": [], "evidence": evidence, "sources": ["arXiv"],
            }
            candidate["id"] = stable_id(candidate)
            results.append(candidate)
    return results


def openalex_adapter(since: str | None = None) -> list[dict[str, Any]]:
    api_key = os.getenv("OPENALEX_API_KEY")
    results: list[dict[str, Any]] = []
    for institution_id, item in INSTITUTIONS.items():
        params = {"search": item["name"], "per-page": "10"}
        if api_key:
            params["api_key"] = api_key
        institutions_url = "https://api.openalex.org/institutions?" + urllib.parse.urlencode(params)
        institution_records = request_json(institutions_url).get("results", [])
        exact_records = [record for record in institution_records if match_institutions(record.get("display_name", ""), allow_restricted=True)]
        for institution in exact_records:
            work_params = {"filter": f"institutions.id:{institution['id'].rsplit('/', 1)[-1]}", "per-page": "200"}
            if since:
                work_params["filter"] += f",from_updated_date:{since}"
            if api_key:
                work_params["api_key"] = api_key
            url = "https://api.openalex.org/works?" + urllib.parse.urlencode(work_params)
            for record in request_json(url).get("results", []):
                affiliations = [normalize_space(raw) for authorship in record.get("authorships", []) for raw in authorship.get("raw_affiliation_strings", [])]
                source_url = record.get("doi") or record.get("id", "")
                matched, evidence = evidence_for_affiliations(affiliations or [item["name"]], "OpenAlex", source_url, "structured")
                if institution_id not in matched:
                    matched.append(institution_id)
                    evidence.append({"level": "structured", "institution": institution_id, "matchedText": item["name"], "source": "OpenAlex", "sourceUrl": source_url})
                year = int(record.get("publication_year") or dt.datetime.now(dt.timezone.utc).year)
                doi = (record.get("doi") or "").replace("https://doi.org/", "")
                candidate = {
                    "type": "article" if record.get("type") == "article" else "preprint" if record.get("type") == "preprint" else "conference",
                    "title": normalize_space(record.get("display_name")),
                    "authors": [{"name": normalize_space(authorship.get("author", {}).get("display_name"))} for authorship in record.get("authorships", [])],
                    "institutions": matched, "rawAffiliations": affiliations, "relationType": "affiliation",
                    "publishedAt": record.get("publication_date") or f"{year}-01-01", "year": year,
                    "venue": normalize_space((record.get("primary_location") or {}).get("source", {}).get("display_name")) or "OpenAlex",
                    "status": "preprint" if record.get("type") == "preprint" else "published",
                    "topics": [topic.get("display_name", "") for topic in record.get("topics", [])[:6]], "abstract": "",
                    "identifiers": {"doi": doi} if doi else {}, "links": [{"label": "Source", "url": source_url}],
                    "versions": [], "evidence": evidence, "sources": ["OpenAlex"],
                }
                candidate["id"] = stable_id(candidate)
                results.append(candidate)
    return results


def bza_official_adapter(_: str | None = None) -> list[dict[str, Any]]:
    """Discover papers explicitly listed by the Academy's public research feed."""
    url = "https://www.bza.edu.cn/api/innovations/news?" + urllib.parse.urlencode({"page": 1, "limit": 200})
    payload = request_json(url)
    contexts: dict[str, list[dict[str, Any]]] = {}
    doi_contexts: list[tuple[str, dict[str, Any]]] = []
    for item in payload.get("data", []):
        content = item.get("content") or ""
        text = clean_html_text(content)
        official_url = f"https://www.bza.edu.cn/detail/inews_{item['uuid']}"
        if "中关村人工智能研究院" in text or "中关村两院" in text or "中关村两院" in item.get("title", ""):
            institutions = ["zgca", "zgci"]
            matched_text = "北京中关村学院与中关村人工智能研究院（中关村两院）"
        else:
            institutions = ["zgca"]
            matched_text = "北京中关村学院官网科研创新页面"
        links = [{"label": "Official", "url": official_url}, *extract_project_links(content)]
        context = {
            "source": "北京中关村学院官网", "sourceUrl": official_url,
            "institutions": institutions, "matchedText": matched_text,
            "level": "official-listing", "text": item.get("title", "") + " " + text,
            "links": links, "publishedAt": (item.get("date") or "")[:10] or f"{dt.datetime.now(dt.timezone.utc).year}-01-01",
        }
        for arxiv_id in extract_arxiv_ids(content):
            contexts.setdefault(arxiv_id, []).append(context)
        for raw_doi in re.findall(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", html.unescape(content), re.I):
            doi = raw_doi.rstrip(".,;:!?，。；：！？)]}）】”’").lower()
            doi_contexts.append((doi, context))
    results = arxiv_candidates_for_ids(contexts, contexts)
    for doi, context in doi_contexts:
        try:
            candidate = crossref_candidate_for_doi(doi, context)
            if candidate:
                results.append(candidate)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
            continue
    return results


def github_projects_adapter(_: str | None = None) -> list[dict[str, Any]]:
    """Scan official project READMEs for affiliations hidden from scholarly APIs."""
    token = os.getenv("GITHUB_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        repos = request_json(
            "https://api.github.com/orgs/longxiang-ai/repos?per_page=100&type=public&sort=updated",
            headers=headers,
        )
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        repos = request_json(
            "https://api.github.com/users/longxiang-ai/repos?per_page=100&type=public&sort=updated",
            headers=headers,
        )
    contexts: dict[str, list[dict[str, Any]]] = {}
    dataset_specs: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    priority_names = {"transnormal", "tsgs", "human101", "vicasplat", "sdgs.github.io"}
    repos = [
        repo for repo in repos
        if repo.get("name", "").lower() in priority_names
        or (
            not repo.get("name", "").lower().startswith("awesome-")
            and re.search(r"\b(official|paper|implementation|code release)\b", repo.get("description") or "", re.I)
        )
    ]
    repos = sorted(repos, key=lambda item: item.get("name", "").lower() == "transnormal", reverse=True)[:50]
    for repo in repos:
        if repo.get("fork"):
            continue
        try:
            readme_payload = request_json(f"https://api.github.com/repos/{repo['full_name']}/readme", headers=headers)
            readme = base64.b64decode(readme_payload.get("content", "")).decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
            continue
        matches = match_institutions(readme)
        if not matches:
            continue
        institutions = list(dict.fromkeys(institution_id for institution_id, _ in matches))
        snippet = affiliation_snippet(readme)
        repo_url = repo.get("html_url") or f"https://github.com/{repo['full_name']}"
        project_links = [
            {"label": "Code", "url": repo_url},
            *[link for link in extract_project_links(readme) if link["label"] != "Code"],
        ]
        context = {
            "source": "Official GitHub project README", "sourceUrl": repo_url,
            "institutions": institutions, "matchedText": snippet,
            "level": "exact-affiliation", "text": readme[:12000], "links": project_links,
        }
        arxiv_ids = extract_arxiv_ids(readme)
        for arxiv_id in arxiv_ids:
            contexts.setdefault(arxiv_id, []).append(context)
        for dataset_path in re.findall(r"https?://huggingface\.co/datasets/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", readme, re.I):
            dataset_url = f"https://huggingface.co/datasets/{dataset_path}"
            dataset_specs.append((dataset_url, context, repo))

    results = arxiv_candidates_for_ids(contexts, contexts)
    paper_by_source = {context["sourceUrl"]: work for work in results for context in sum(contexts.values(), []) if context["sourceUrl"] in [link["url"] for link in work.get("links", [])]}
    for dataset_url, context, repo in dataset_specs:
        related = paper_by_source.get(context["sourceUrl"])
        published_at = (related or {}).get("publishedAt") or (repo.get("created_at") or "")[:10] or f"{dt.datetime.now(dt.timezone.utc).year}-01-01"
        year = int(published_at[:4])
        title = dataset_url.rstrip("/").rsplit("/", 1)[-1]
        candidate = {
            "type": "dataset", "title": title,
            "authors": [{"name": repo.get("owner", {}).get("login", "longxiang-ai")}],
            "institutions": context["institutions"], "rawAffiliations": [context["matchedText"]],
            "relationType": "official-output", "publishedAt": published_at, "year": year,
            "venue": "Hugging Face Datasets", "status": "released",
            "topics": ["Open Dataset"],
            "abstract": f"Dataset released alongside {related['title']}" if related else f"Dataset released by the {repo['name']} project.",
            "identifiers": {},
            "links": [{"label": "Dataset", "url": dataset_url}, {"label": "Project", "url": context["sourceUrl"]}],
            "versions": [{"label": "Hugging Face dataset", "url": dataset_url}],
            "evidence": [{
                "level": "exact-affiliation", "institution": institution_id,
                "matchedText": context["matchedText"], "source": context["source"], "sourceUrl": context["sourceUrl"],
            } for institution_id in context["institutions"]],
            "sources": ["Official GitHub project README", "Hugging Face"],
        }
        candidate["id"] = stable_id(candidate)
        results.append(candidate)
    return results


ADAPTERS = {
    "crossref": crossref_adapter,
    "openalex": openalex_adapter,
    "europe_pmc": europe_pmc_adapter,
    "arxiv": arxiv_adapter,
    "datacite": datacite_adapter,
    "bza_official": bza_official_adapter,
    "github_projects": github_projects_adapter,
}


def merge_record(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key in ["institutions", "topics", "sources"]:
        merged[key] = list(dict.fromkeys([*existing.get(key, []), *incoming.get(key, [])]))
    merged["rawAffiliations"] = list(dict.fromkeys(
        clean_html_text(value) for value in [*existing.get("rawAffiliations", []), *incoming.get("rawAffiliations", [])]
        if clean_html_text(value)
    ))
    evidence_keys = {(item.get("institution"), item.get("source"), item.get("matchedText")) for item in existing.get("evidence", [])}
    merged["evidence"] = [*existing.get("evidence", []), *[item for item in incoming.get("evidence", []) if (item.get("institution"), item.get("source"), item.get("matchedText")) not in evidence_keys]]
    link_keys = {(item.get("label"), item.get("url")) for item in existing.get("links", [])}
    merged["links"] = sanitize_links([*existing.get("links", []), *[item for item in incoming.get("links", []) if (item.get("label"), item.get("url")) not in link_keys]])
    version_items = [*existing.get("versions", []), *incoming.get("versions", [])]
    incoming_doi = incoming.get("identifiers", {}).get("doi")
    existing_doi = existing.get("identifiers", {}).get("doi")
    if incoming_doi and incoming_doi != existing_doi:
        version_items.append({"label": f"DOI {incoming_doi}", "url": f"https://doi.org/{incoming_doi}"})
    seen_versions: set[tuple[str | None, str | None]] = set()
    merged["versions"] = []
    for item in version_items:
        key = (item.get("label"), item.get("url"))
        if key not in seen_versions:
            seen_versions.add(key)
            merged["versions"].append(item)
    for key in ["abstract", "venue"]:
        if len(str(incoming.get(key, ""))) > len(str(existing.get(key, ""))):
            merged[key] = incoming[key]
    # Prefer formal publication metadata while retaining all version links.
    if existing.get("status") != "published" and incoming.get("status") == "published":
        for key in ["type", "status", "venue", "publishedAt", "year", "identifiers"]:
            merged[key] = incoming.get(key, merged.get(key))
    return merged


def deduplicate(existing: list[dict[str, Any]], discovered: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    # First collapse duplicates already present from an earlier pipeline run.
    by_id: dict[str, dict[str, Any]] = {}
    doi_index: dict[str, str] = {}
    title_index: dict[str, str] = {}
    for original in sorted(existing, key=lambda item: (item.get("status") == "published", item.get("publishedAt", "")), reverse=True):
        work = dict(original)
        work["abstract"] = clean_abstract(work.get("abstract"))
        work["links"] = sanitize_links(work.get("links", []))
        work["rawAffiliations"] = list(dict.fromkeys(
            clean_html_text(value) for value in work.get("rawAffiliations", []) if clean_html_text(value)
        ))
        work["evidence"] = [
            {**item, "matchedText": clean_html_text(item.get("matchedText"))}
            for item in work.get("evidence", [])
        ]
        doi = work.get("identifiers", {}).get("doi", "").lower()
        title_key = normalize_title(work["title"])
        target = (doi_index.get(doi) if doi else None) or title_index.get(title_key)
        if target:
            by_id[target] = merge_record(by_id[target], work)
        else:
            by_id[work["id"]] = work
            if doi:
                doi_index[doi] = work["id"]
            title_index[title_key] = work["id"]
    added = 0
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for work in discovered:
        if not work.get("title") or not work.get("evidence"):
            continue
        doi = work.get("identifiers", {}).get("doi", "").lower()
        target = doi_index.get(doi) if doi else None
        title_key = normalize_title(work["title"])
        target = target or title_index.get(title_key) or work["id"]
        if target in by_id:
            by_id[target] = merge_record(by_id[target], work)
        else:
            work["id"] = target
            work["updatedAt"] = now
            work["links"] = sanitize_links(work.get("links", []))
            by_id[target] = work
            added += 1
            if doi:
                doi_index[doi] = target
            title_index[title_key] = target
    return sorted(by_id.values(), key=lambda item: (item.get("publishedAt", ""), item["id"]), reverse=True), added


def validate(works: list[dict[str, Any]]) -> None:
    required = {"id", "type", "title", "authors", "institutions", "publishedAt", "year", "venue", "status", "identifiers", "links", "evidence", "sources", "updatedAt"}
    ids: set[str] = set()
    for index, work in enumerate(works):
        missing = required - set(work)
        if missing:
            raise ValueError(f"work[{index}] missing fields: {', '.join(sorted(missing))}")
        if work["id"] in ids:
            raise ValueError(f"duplicate work id: {work['id']}")
        ids.add(work["id"])
        if not work["institutions"] or not work["evidence"]:
            raise ValueError(f"{work['id']} has no institution evidence")
        if any(item not in INSTITUTIONS for item in work["institutions"]):
            raise ValueError(f"{work['id']} contains an unknown institution")


def write_if_changed(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def generate_bibtex(works: list[dict[str, Any]]) -> str:
    records = []
    for work in works:
        kind = "inproceedings" if work["type"] == "conference" else "techreport" if work["type"] in {"report", "whitepaper"} else "misc" if work["type"] in {"dataset", "patent"} else "article"
        family = work.get("authors", [{}])[0].get("name", "zgca").split()[-1].lower()
        key = re.sub(r"[^a-z0-9]", "", f"{family}{work['year']}{work['id'][-5:]}")
        venue_field = "booktitle" if kind == "inproceedings" else "institution" if kind == "techreport" else "journal"
        lines = [f"@{kind}{{{key},", f"  title = {{{work['title']}}},", f"  author = {{{' and '.join(item['name'] for item in work['authors'])}}},", f"  year = {{{work['year']}}},", f"  {venue_field} = {{{work['venue']}}}"]
        if work.get("identifiers", {}).get("doi"):
            lines[-1] += ","
            lines.append(f"  doi = {{{work['identifiers']['doi']}}}")
        lines.append("}")
        records.append("\n".join(lines))
    return "\n\n".join(records) + "\n"


def generate_feed(works: list[dict[str, Any]]) -> str:
    latest = works[:20]
    updated = max((work.get("updatedAt", "") for work in works), default="2026-01-01T00:00:00Z")
    entries = []
    for work in latest:
        url = work.get("links", [{}])[0].get("url", "https://longxiang-ai.github.io/awesome-zgca-papers/")
        entries.append(f"  <entry><title>{xml_escape(work['title'])}</title><id>{xml_escape(work['id'])}</id><link href=\"{xml_escape(url)}\"/><updated>{xml_escape(work['updatedAt'])}</updated><summary>{xml_escape(work.get('abstract', ''))}</summary></entry>")
    return "\n".join(["<?xml version=\"1.0\" encoding=\"utf-8\"?>", "<feed xmlns=\"http://www.w3.org/2005/Atom\">", "  <title>Awesome ZGCA Papers</title>", "  <id>https://longxiang-ai.github.io/awesome-zgca-papers/</id>", f"  <updated>{updated}</updated>", *entries, "</feed>", ""])


def xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def generate_readme(works: list[dict[str, Any]], coverage: dict[str, str]) -> str:
    latest = "\n".join(f"- [{work['title']}]({work.get('links', [{}])[0].get('url', '#')}) — {work['venue']} ({work['year']})" for work in works[:8])
    source_rows = "\n".join(f"| {name} | {status} |" for name, status in sorted(coverage.items()))
    return f"""# Awesome ZGCA Papers

[![Daily update](https://github.com/longxiang-ai/awesome-zgca-papers/actions/workflows/update-and-deploy.yml/badge.svg)](https://github.com/longxiang-ai/awesome-zgca-papers/actions/workflows/update-and-deploy.yml)
[![GitHub Pages](https://img.shields.io/badge/site-GitHub%20Pages-087f89)](https://longxiang-ai.github.io/awesome-zgca-papers/)

A bilingual, traceable index of research outputs from **Zhongguancun Academy (北京中关村学院)** and the **Zhongguancun Institute of Artificial Intelligence (中关村人工智能研究院)**.

> Public-source coverage is maximized, but absolute completeness cannot be guaranteed. Every item retains inspectable institution evidence.

## Discovery strategy

- Search structured affiliation metadata across Crossref, OpenAlex, Europe PMC, arXiv and DataCite.
- Backfill papers explicitly announced by the Academy's official research feed, then resolve their arXiv IDs and DOIs.
- Scan official project repositories for exact affiliation lines that are present in a paper PDF or README but absent from arXiv metadata.
- Merge formal publications, preprints and companion datasets without deleting existing records when a source is temporarily unavailable.

## Latest outputs

{latest}

## Data sources

| Source | Status |
| --- | --- |
{source_rows}

## Use the data

- [`public/data/works.json`](public/data/works.json) — normalized metadata
- [`public/data/works.bib`](public/data/works.bib) — BibTeX export
- [`public/data/stats.json`](public/data/stats.json) — collection statistics
- [`public/feed.xml`](public/feed.xml) — Atom feed

## Local development

```bash
npm ci
python3 scripts/pipeline.py build
npm run dev
```

Run networked discovery with `python3 scripts/pipeline.py fetch`. Optional API keys are documented in `.env.example` and should be stored as GitHub Actions secrets.

## Corrections

Open an issue or pull request. Stable overrides live in `data/overrides.yml` and are never replaced by the automated pipeline.

## License

Code is MIT licensed. Aggregated metadata remains subject to its original source terms and always retains provenance links.
"""


def export_outputs(works: list[dict[str, Any]], coverage: dict[str, str]) -> list[Path]:
    validate(works)
    works = sorted(works, key=lambda item: (item.get("publishedAt", ""), item["id"]), reverse=True)
    latest_update = max((work["updatedAt"] for work in works), default="2026-01-01T00:00:00Z")
    current_year = dt.datetime.now(dt.timezone.utc).year
    type_counts = Counter(work["type"] for work in works)
    institution_counts = {institution_id: sum(institution_id in work["institutions"] for work in works) for institution_id in INSTITUTIONS}
    stats = {
        "total": len(works), "thisYear": sum(work["year"] == current_year for work in works),
        "institutions": institution_counts,
        "byType": {kind: type_counts.get(kind, 0) for kind in ["article", "conference", "preprint", "dataset", "report", "whitepaper", "patent"]},
        "lastUpdated": latest_update, "sourceCount": len(coverage),
    }
    coverage_payload = {"generatedAt": latest_update, "mode": "automatic", "sources": coverage}
    pretty = json.dumps(works, ensure_ascii=False, indent=2) + "\n"
    compact = json.dumps(works, ensure_ascii=False, separators=(",", ":")) + "\n"
    jsonl = "\n".join(json.dumps(work, ensure_ascii=False, separators=(",", ":")) for work in works) + "\n"
    outputs = {
        DATA / "works.jsonl": jsonl,
        DATA / "works.json": pretty,
        PUBLIC / "data" / "works.json": compact,
        PUBLIC / "data" / "stats.json": json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
        PUBLIC / "data" / "coverage.json": json.dumps(coverage_payload, ensure_ascii=False, indent=2) + "\n",
        PUBLIC / "data" / "works.bib": generate_bibtex(works),
        PUBLIC / "feed.xml": generate_feed(works),
        ROOT / "README.md": generate_readme(works, coverage),
    }
    changed = [path for path, content in outputs.items() if write_if_changed(path, content)]
    return changed


def run_fetch(since: str | None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    existing = load_works()
    discovered: list[dict[str, Any]] = []
    coverage: dict[str, str] = {}
    for name, adapter in ADAPTERS.items():
        try:
            records = adapter(since)
            discovered.extend(records)
            coverage[name] = f"ok ({len(records)} matched)"
        except (urllib.error.URLError, TimeoutError, ET.ParseError, ValueError, KeyError) as error:
            coverage[name] = f"unavailable ({type(error).__name__})"
            print(f"[warn] {name}: {error}", file=sys.stderr)
    coverage.update({
        "semantic_scholar": "optional key" if not os.getenv("SEMANTIC_SCHOLAR_API_KEY") else "configured",
        "core": "optional key" if not os.getenv("CORE_API_KEY") else "configured",
        "lens": "optional key" if not os.getenv("LENS_API_TOKEN") else "configured",
    })
    merged, added = deduplicate(existing, discovered)
    print(f"Discovered {len(discovered)} candidates; added {added} new records.")
    return merged, coverage


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["build", "fetch", "validate"])
    parser.add_argument("--since", help="Only query records updated since YYYY-MM-DD")
    args = parser.parse_args()
    if args.command == "fetch":
        works, coverage = run_fetch(args.since)
    else:
        works, _ = deduplicate(load_works(), [])
        coverage_path = PUBLIC / "data" / "coverage.json"
        if coverage_path.exists():
            coverage = json.loads(coverage_path.read_text(encoding="utf-8")).get("sources", {})
        else:
            coverage = {
                "arxiv": "available", "bza_official": "available", "core": "optional key", "crossref": "available",
                "datacite": "available", "europe_pmc": "available", "github_projects": "available",
                "lens": "optional key", "openalex": "available with key", "semantic_scholar": "optional key",
            }
    validate(works)
    if args.command != "validate":
        changed = export_outputs(works, coverage)
        print("Updated: " + (", ".join(str(path.relative_to(ROOT)) for path in changed) if changed else "nothing"))
    else:
        print(f"Validated {len(works)} works.")


if __name__ == "__main__":
    main()
