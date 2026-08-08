# Awesome ZGCA Papers

[![Daily update](https://github.com/longxiang-ai/awesome-zgca-papers/actions/workflows/update-and-deploy.yml/badge.svg)](https://github.com/longxiang-ai/awesome-zgca-papers/actions/workflows/update-and-deploy.yml)
[![GitHub Pages](https://img.shields.io/badge/site-GitHub%20Pages-087f89)](https://longxiang-ai.github.io/awesome-zgca-papers/)

A bilingual, traceable index of research outputs from **Zhongguancun Academy (北京中关村学院)** and the **Zhongguancun Institute of Artificial Intelligence (中关村人工智能研究院)**.

> Public-source coverage is maximized, but absolute completeness cannot be guaranteed. Every item retains inspectable institution evidence.

## Latest outputs

- [Data Preparation for Large Language Models](https://doi.org/10.1007/s11390-026-5948-8) — Journal of Computer Science and Technology (2026)
- [Efficient Reasoning with Balanced Thinking](https://rebalance-ai.github.io/) — ICLR 2026 (2026)
- [PoliCon: Evaluating LLMs on Achieving Diverse Political Consensus Objectives](https://arxiv.org/abs/2505.19558) — ICLR 2026 (2026)
- [北京市生成式人工智能大模型产业发展与治理白皮书](https://www.iie.ac.cn/xwdt/kydt/202603/t20260303_8147538.html) — 第四届北京人工智能产业创新发展大会 (2026)
- [SciNet: Evaluating AI Agents in Relation-Aware Scientific Literature Retrieval](https://arxiv.org/abs/2601.03260) — arXiv (2026)
- [IgGM2: An All-Atom Foundation Model for Adaptive Immune Receptor Design](https://doi.org/10.64898/2026.07.09.737510) — Crossref (2026)
- [Functional Locality–Aligned Learning Reveals Structure–Function Causality in Enzyme Kinetics](https://doi.org/10.64898/2026.03.04.709726) — Crossref (2026)
- [Ophiuchus-Ab: A Versatile Generative Foundation Model for Advanced Antibody-Based Immunotherapy](https://doi.org/10.64898/2026.02.02.703197) — Crossref (2026)

## Data sources

| Source | Status |
| --- | --- |
| arxiv | available |
| core | optional key |
| crossref | available |
| datacite | available |
| europe_pmc | available |
| lens | optional key |
| official_sites | available |
| openalex | available with key |
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

## Corrections

Open an issue or pull request. Stable overrides live in `data/overrides.yml` and are never replaced by the automated pipeline.

## License

Code is MIT licensed. Aggregated metadata remains subject to its original source terms and always retains provenance links.
