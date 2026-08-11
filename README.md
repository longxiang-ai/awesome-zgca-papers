# Awesome ZGCA Papers

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

- [TacReasoner: A Dynamic Tactile-Language Framework for Interactive Reasoning in Real-World Scenarios](https://arxiv.org/abs/2607.05131) — IROS 2026 (2026)
- [Feeling the Unexpected: ResTacVLA for Contact-Rich Manipulation via Residual Tactile Representation](https://arxiv.org/abs/2607.03387) — IROS 2026 (2026)
- [UrbanWell: Benchmarking Multimodal Large Language Models for Spatio-Temporal Urban Wellbeing Analytics](https://arxiv.org/abs/2606.15890) — KDD Datasets and Benchmarks Track 2026 (2026)
- [SciCustom: A Framework for Custom Evaluation of Scientific Capabilities in Large Language Models](https://arxiv.org/abs/2605.19357) — ACL 2026 (2026)
- [IntentVLA: Short-Horizon Intent Modeling for Aliased Robot Manipulation](https://arxiv.org/abs/2605.14712) — arXiv (2026)
- [PipeSD: An Efficient Cloud-Edge Collaborative Pipeline Inference Framework with Speculative Decoding](https://arxiv.org/abs/2605.13319) — ICML 2026 (2026)
- [Data Preparation for Large Language Models](https://doi.org/10.1007/s11390-026-5948-8) — Journal of Computer Science and Technology (2026)
- [FutureWorld: A Live Reinforcement Learning Environment for Predictive Agents with Real-World Outcome Rewards](https://arxiv.org/abs/2604.26733) — arXiv (2026)

## Data sources

| Source | Status |
| --- | --- |
| arxiv | ok (0 matched) |
| bza_official | ok (21 matched) |
| core | optional key |
| crossref | unavailable (HTTPError) |
| datacite | ok (17 matched) |
| europe_pmc | unavailable (TimeoutError) |
| github_projects | ok (7 matched) |
| lens | optional key |
| openalex | ok (0 matched) |
| semantic_scholar | optional key |

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

## Polite arXiv HTML backfill

Historical affiliation discovery uses the partner-university list published by
[`bjzgcai`](https://github.com/bjzgcai/.github/blob/main/profile/README.md#-partner-universities)
as a structured prefilter. OpenAlex first selects papers involving those institutions and an arXiv location; only that reduced queue is allowed to request `https://arxiv.org/html/<id>v1`.

```bash
python3 scripts/arxiv_html_backfill.py prefilter --from 2024-06-01
python3 scripts/arxiv_html_backfill.py start
python3 scripts/arxiv_html_backfill.py status
```

The local SQLite checkpoint is resumable and ignored by Git. arXiv requests are single-connection, at least 3.5 seconds apart by default, take a five-minute rest every 500 requests, and back off automatically after throttling or server errors. Only HTML matches in the author-affiliation region are published automatically; body-text matches remain audit-only associations.

## Corrections

Open an issue or pull request. Stable overrides live in `data/overrides.yml` and are never replaced by the automated pipeline.

## License

Code is MIT licensed. Aggregated metadata remains subject to its original source terms and always retains provenance links.
