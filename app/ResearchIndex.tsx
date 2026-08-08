"use client";

import { useEffect, useMemo, useRef, useState } from "react";

type Locale = "zh" | "en";
type InstitutionId = "zgca" | "zgci";
type WorkType = "article" | "conference" | "preprint" | "dataset" | "report" | "whitepaper" | "patent";
type EvidenceLevel = "structured" | "exact-affiliation" | "official-listing" | "association";

type Author = { name: string; institutions?: InstitutionId[] };
type Link = { label: string; url: string };
type Evidence = {
  level: EvidenceLevel;
  institution: InstitutionId;
  matchedText: string;
  source: string;
  sourceUrl: string;
};

type Work = {
  id: string;
  type: WorkType;
  title: string;
  authors: Author[];
  institutions: InstitutionId[];
  rawAffiliations: string[];
  relationType: "affiliation" | "official-output" | "assignee";
  publishedAt: string;
  year: number;
  venue: string;
  status: "published" | "preprint" | "released";
  topics: string[];
  abstract: string;
  identifiers: { doi?: string; arxiv?: string; patent?: string };
  links: Link[];
  versions: { label: string; url: string }[];
  evidence: Evidence[];
  sources: string[];
  updatedAt: string;
};

type Stats = {
  total: number;
  thisYear: number;
  institutions: Record<InstitutionId, number>;
  byType: Record<string, number>;
  lastUpdated: string;
  sourceCount: number;
};

const copy = {
  zh: {
    eyebrow: "开放学术基础设施 · 每日更新",
    titleA: "发现中关村两院的",
    titleB: "每一项研究成果",
    intro: "聚合北京中关村学院与中关村人工智能研究院的论文、数据集、报告和专利。每条记录都保留可追溯的机构证据。",
    search: "搜索标题、作者、主题或期刊…",
    total: "收录成果",
    newYear: "本年新增",
    institutions: "覆盖机构",
    sources: "数据来源",
    updated: "最近更新",
    all: "全部成果",
    papers: "论文与预印本",
    datasets: "数据集",
    reports: "报告与白皮书",
    patents: "专利",
    filters: "筛选",
    institution: "单位",
    year: "年份",
    status: "状态",
    allInstitutions: "全部单位",
    allYears: "全部年份",
    allStatuses: "全部状态",
    published: "已发表",
    preprint: "预印本",
    released: "已发布",
    results: "项结果",
    clear: "清除筛选",
    noResults: "没有找到匹配成果",
    noResultsHelp: "试试缩短关键词或清除筛选条件。",
    details: "查看详情",
    abstract: "摘要",
    affiliationEvidence: "机构证据",
    authors: "作者",
    versions: "版本与资源",
    cite: "引用与导出",
    copyBibtex: "复制 BibTeX",
    copied: "已复制",
    close: "关闭",
    methodology: "收录方法",
    methodologyText: "本索引优先覆盖所有公开可检索成果。结构化机构、原始署名、官方发布页与作者成果页会被分别标注，便于判断证据强度。",
    fullData: "下载全部 JSON",
    bibtex: "下载 BibTeX",
    feed: "订阅更新",
    correct: "提交纠错",
    transparency: "透明、可追溯、可协作",
    transparencyText: "数据自动聚合，但从不隐藏依据。你可以检查每一条机构匹配、访问原始来源，或通过 GitHub 修正记录。",
    coverage: "覆盖说明",
    coverageText: "“所有成果”指公开数据源范围内的最大覆盖，不代表绝对完整。来源失败不会删除已有记录。",
    code: "查看源码",
  },
  en: {
    eyebrow: "Open scholarly infrastructure · Updated daily",
    titleA: "Discover every research output from",
    titleB: "ZGCA × ZGCI",
    intro: "A traceable index of papers, datasets, reports and patents from Zhongguancun Academy and the Zhongguancun Institute of Artificial Intelligence.",
    search: "Search titles, authors, topics or venues…",
    total: "Research outputs",
    newYear: "Added this year",
    institutions: "Institutions",
    sources: "Data sources",
    updated: "Last updated",
    all: "All outputs",
    papers: "Papers & preprints",
    datasets: "Datasets",
    reports: "Reports & white papers",
    patents: "Patents",
    filters: "Filters",
    institution: "Institution",
    year: "Year",
    status: "Status",
    allInstitutions: "All institutions",
    allYears: "All years",
    allStatuses: "All statuses",
    published: "Published",
    preprint: "Preprint",
    released: "Released",
    results: "results",
    clear: "Clear filters",
    noResults: "No matching outputs",
    noResultsHelp: "Try a shorter query or clear your filters.",
    details: "View details",
    abstract: "Abstract",
    affiliationEvidence: "Institution evidence",
    authors: "Authors",
    versions: "Versions & resources",
    cite: "Cite & export",
    copyBibtex: "Copy BibTeX",
    copied: "Copied",
    close: "Close",
    methodology: "How inclusion works",
    methodologyText: "This index optimizes for coverage across public sources. Structured institutions, raw affiliations, official releases and author listings are labeled separately so evidence strength stays visible.",
    fullData: "Download all JSON",
    bibtex: "Download BibTeX",
    feed: "Subscribe to updates",
    correct: "Submit a correction",
    transparency: "Transparent, traceable, collaborative",
    transparencyText: "Data is aggregated automatically, but evidence is never hidden. Inspect every match, follow the original source, or correct a record on GitHub.",
    coverage: "Coverage note",
    coverageText: "“All outputs” means maximum coverage across public sources, not a guarantee of absolute completeness. A source outage never removes existing records.",
    code: "View source",
  },
} as const;

const institutionNames: Record<InstitutionId, { zh: string; en: string; short: string }> = {
  zgca: { zh: "北京中关村学院", en: "Zhongguancun Academy", short: "ZGCA" },
  zgci: { zh: "中关村人工智能研究院", en: "Zhongguancun Institute of AI", short: "ZGCI" },
};

const evidenceLabels: Record<EvidenceLevel, { zh: string; en: string }> = {
  structured: { zh: "结构化机构", en: "Structured institution" },
  "exact-affiliation": { zh: "原始署名", en: "Exact affiliation" },
  "official-listing": { zh: "官方发布", en: "Official listing" },
  association: { zh: "成果关联", en: "Output association" },
};

const typeLabels: Record<WorkType, { zh: string; en: string }> = {
  article: { zh: "期刊论文", en: "Journal article" },
  conference: { zh: "会议论文", en: "Conference paper" },
  preprint: { zh: "预印本", en: "Preprint" },
  dataset: { zh: "数据集", en: "Dataset" },
  report: { zh: "研究报告", en: "Research report" },
  whitepaper: { zh: "白皮书", en: "White paper" },
  patent: { zh: "专利", en: "Patent" },
};

function initials(name: string) {
  return name.split(/\s+/).map((part) => part[0]).join("").slice(0, 2).toUpperCase();
}

function bibtex(work: Work) {
  const key = `${work.authors[0]?.name.split(" ").at(-1)?.toLowerCase() ?? "zgca"}${work.year}`.replace(/[^a-z0-9]/g, "");
  const kind = work.type === "conference" ? "inproceedings" : work.type === "whitepaper" || work.type === "report" ? "techreport" : "article";
  return `@${kind}{${key},\n  title = {${work.title}},\n  author = {${work.authors.map((author) => author.name).join(" and ")}},\n  year = {${work.year}},\n  ${kind === "article" ? "journal" : kind === "inproceedings" ? "booktitle" : "institution"} = {${work.venue}}${work.identifiers.doi ? `,\n  doi = {${work.identifiers.doi}}` : ""}${work.identifiers.arxiv ? `,\n  eprint = {${work.identifiers.arxiv}}` : ""}\n}`;
}

export function ResearchIndex({ works, stats }: { works: Work[]; stats: Stats }) {
  const [locale, setLocale] = useState<Locale>("zh");
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState("all");
  const [institution, setInstitution] = useState("all");
  const [year, setYear] = useState("all");
  const [status, setStatus] = useState("all");
  const [selected, setSelected] = useState<Work | null>(null);
  const [copied, setCopied] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const t = copy[locale];

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const work = params.get("work");
    const lang = params.get("lang");
    if (lang === "en" || lang === "zh") setLocale(lang);
    if (work) setSelected(works.find((item) => item.id === work) ?? null);
  }, [works]);

  useEffect(() => {
    document.documentElement.lang = locale === "zh" ? "zh-CN" : "en";
  }, [locale]);

  useEffect(() => {
    const focusSearch = (event: KeyboardEvent) => {
      if (event.key === "/" && !event.metaKey && !event.ctrlKey && document.activeElement?.tagName !== "INPUT") {
        event.preventDefault();
        searchRef.current?.focus();
      }
    };
    document.addEventListener("keydown", focusSearch);
    return () => document.removeEventListener("keydown", focusSearch);
  }, []);

  useEffect(() => {
    if (!selected) return;
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && closeDetail();
    document.addEventListener("keydown", onKey);
    document.body.classList.add("modal-open");
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.classList.remove("modal-open");
    };
  }, [selected]);

  const years = useMemo(() => [...new Set(works.map((work) => work.year))].sort((a, b) => b - a), [works]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return works
      .filter((work) => {
        if (tab === "papers" && !["article", "conference", "preprint"].includes(work.type)) return false;
        if (tab === "datasets" && work.type !== "dataset") return false;
        if (tab === "reports" && !["report", "whitepaper"].includes(work.type)) return false;
        if (tab === "patents" && work.type !== "patent") return false;
        if (institution !== "all" && !work.institutions.includes(institution as InstitutionId)) return false;
        if (year !== "all" && work.year !== Number(year)) return false;
        if (status !== "all" && work.status !== status) return false;
        if (!needle) return true;
        const haystack = [work.title, work.venue, work.abstract, ...work.topics, ...work.authors.map((author) => author.name)].join(" ").toLowerCase();
        return haystack.includes(needle);
      })
      .sort((a, b) => b.publishedAt.localeCompare(a.publishedAt));
  }, [works, query, tab, institution, year, status]);

  function openDetail(work: Work) {
    setSelected(work);
    const url = new URL(window.location.href);
    url.searchParams.set("work", work.id);
    url.searchParams.set("lang", locale);
    window.history.pushState({}, "", url);
  }

  function closeDetail() {
    setSelected(null);
    setCopied(false);
    const url = new URL(window.location.href);
    url.searchParams.delete("work");
    window.history.pushState({}, "", url);
  }

  async function copyCitation() {
    if (!selected) return;
    await navigator.clipboard.writeText(bibtex(selected));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  }

  const hasFilters = Boolean(query || tab !== "all" || institution !== "all" || year !== "all" || status !== "all");

  return (
    <main>
      <header className="site-header">
        <a className="brand" href="./" aria-label="Awesome ZGCA Papers home">
          <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>
          <span><b>AWESOME ZGCA</b><small>RESEARCH INDEX</small></span>
        </a>
        <nav aria-label="Primary navigation">
          <a href="#method">{t.methodology}</a>
          <a href="https://github.com/longxiang-ai/awesome-zgca-papers" target="_blank" rel="noreferrer">GitHub ↗</a>
          <button className="language-toggle" onClick={() => setLocale(locale === "zh" ? "en" : "zh")} aria-label={locale === "zh" ? "Switch to English" : "切换至中文"}>
            <span className={locale === "zh" ? "active" : ""}>中</span><span className={locale === "en" ? "active" : ""}>EN</span>
          </button>
        </nav>
      </header>

      <section className="hero">
        <div className="hero-grid" aria-hidden="true" />
        <div className="hero-orbit orbit-one" aria-hidden="true" />
        <div className="hero-orbit orbit-two" aria-hidden="true" />
        <div className="hero-copy">
          <div className="eyebrow"><span />{t.eyebrow}</div>
          <h1>{t.titleA}<br /><em>{t.titleB}</em></h1>
          <p>{t.intro}</p>
          <div className="hero-search">
            <span aria-hidden="true">⌕</span>
            <label className="sr-only" htmlFor="global-search">{t.search}</label>
            <input ref={searchRef} id="global-search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t.search} />
            <kbd>/</kbd>
          </div>
        </div>
        <div className="hero-side" aria-hidden="true">
          <span className="side-index">INDEX / 001</span>
          <div className="monogram">Z<span>×</span>A</div>
          <p>ZHONGGUANCUN<br />ACADEMY</p>
        </div>
      </section>

      <section className="stats-strip" aria-label="Collection statistics">
        <article><strong>{String(stats.total).padStart(2, "0")}</strong><span>{t.total}</span></article>
        <article><strong>+{stats.thisYear}</strong><span>{t.newYear}</span></article>
        <article><strong>02</strong><span>{t.institutions}</span></article>
        <article><strong>{String(stats.sourceCount).padStart(2, "0")}</strong><span>{t.sources}</span></article>
        <article className="updated-stat"><span>{t.updated}</span><strong>{stats.lastUpdated.slice(0, 10)}</strong></article>
      </section>

      <section className="catalog" id="catalog">
        <div className="catalog-heading">
          <div><span className="section-number">01</span><h2>{locale === "zh" ? "研究成果索引" : "Research output index"}</h2></div>
          <p>{locale === "zh" ? "从署名证据出发，连接论文与真实机构。" : "Connecting outputs to institutions through inspectable evidence."}</p>
        </div>

        <div className="tabs" role="tablist" aria-label="Output types">
          {([
            ["all", t.all], ["papers", t.papers], ["datasets", t.datasets], ["reports", t.reports], ["patents", t.patents],
          ] as const).map(([value, label]) => (
            <button key={value} role="tab" aria-selected={tab === value} className={tab === value ? "active" : ""} onClick={() => setTab(value)}>{label}</button>
          ))}
        </div>

        <div className="catalog-layout">
          <aside className="filters" aria-label={t.filters}>
            <div className="filter-title"><b>{t.filters}</b><span>{filtered.length.toString().padStart(2, "0")}</span></div>
            <label>{t.institution}
              <select value={institution} onChange={(event) => setInstitution(event.target.value)}>
                <option value="all">{t.allInstitutions}</option>
                <option value="zgca">{institutionNames.zgca[locale]}</option>
                <option value="zgci">{institutionNames.zgci[locale]}</option>
              </select>
            </label>
            <label>{t.year}
              <select value={year} onChange={(event) => setYear(event.target.value)}>
                <option value="all">{t.allYears}</option>
                {years.map((item) => <option value={item} key={item}>{item}</option>)}
              </select>
            </label>
            <label>{t.status}
              <select value={status} onChange={(event) => setStatus(event.target.value)}>
                <option value="all">{t.allStatuses}</option>
                <option value="published">{t.published}</option>
                <option value="preprint">{t.preprint}</option>
                <option value="released">{t.released}</option>
              </select>
            </label>
            {hasFilters && <button className="clear-button" onClick={() => { setQuery(""); setTab("all"); setInstitution("all"); setYear("all"); setStatus("all"); }}>× {t.clear}</button>}
            <div className="filter-note"><span>i</span><p>{t.coverageText}</p></div>
          </aside>

          <div className="results">
            <div className="result-count"><b>{filtered.length}</b> {t.results}<span />{query && <small>“{query}”</small>}</div>
            {filtered.length ? filtered.map((work, index) => (
              <article className="work-card" key={work.id}>
                <div className="work-index">{String(index + 1).padStart(2, "0")}</div>
                <div className="work-body">
                  <div className="work-meta">
                    <span className={`type-badge type-${work.type}`}>{typeLabels[work.type][locale]}</span>
                    <span>{work.year}</span><span>·</span><span>{work.venue}</span>
                  </div>
                  <button className="work-title" onClick={() => openDetail(work)}>{work.title}</button>
                  <p className="author-line">{work.authors.map((author) => author.name).join(", ")}</p>
                  <div className="work-footer">
                    <div className="institution-tags">
                      {work.institutions.map((id) => <span key={id} className={`institution-tag ${id}`}><i />{institutionNames[id].short}</span>)}
                      <span className={`evidence-badge evidence-${work.evidence[0].level}`}>{evidenceLabels[work.evidence[0].level][locale]}</span>
                    </div>
                    <div className="work-links">
                      {work.links.slice(0, 3).map((link) => <a key={link.label} href={link.url} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>{link.label} ↗</a>)}
                      <button onClick={() => openDetail(work)}>{t.details} →</button>
                    </div>
                  </div>
                </div>
              </article>
            )) : (
              <div className="empty-state"><span>∅</span><h3>{t.noResults}</h3><p>{t.noResultsHelp}</p></div>
            )}
          </div>
        </div>
      </section>

      <section className="method" id="method">
        <div className="method-copy">
          <span className="section-number light">02</span>
          <p className="method-kicker">OPEN / TRACEABLE / DAILY</p>
          <h2>{t.transparency}</h2>
          <p>{t.transparencyText}</p>
          <div className="method-actions">
            <a href="data/works.json" download>{t.fullData} ↓</a>
            <a href="data/works.bib" download>{t.bibtex} ↓</a>
            <a href="feed.xml">{t.feed} ↗</a>
          </div>
        </div>
        <div className="method-grid">
          <article><span>01</span><b>DISCOVER</b><p>{locale === "zh" ? "并行查询公开学术与机构来源" : "Query public scholarly and institutional sources"}</p></article>
          <article><span>02</span><b>VERIFY</b><p>{locale === "zh" ? "匹配结构化单位与原始署名" : "Match structured institutions and raw affiliations"}</p></article>
          <article><span>03</span><b>MERGE</b><p>{locale === "zh" ? "合并 DOI、arXiv 与出版版本" : "Merge DOI, arXiv and published versions"}</p></article>
          <article><span>04</span><b>PUBLISH</b><p>{locale === "zh" ? "每日生成开放数据并发布" : "Generate open data and publish every day"}</p></article>
        </div>
      </section>

      <footer>
        <div className="footer-brand"><span className="brand-mark" aria-hidden="true"><i /><i /><i /></span><b>AWESOME ZGCA PAPERS</b></div>
        <p>{t.coverageText}</p>
        <div><a href="https://github.com/longxiang-ai/awesome-zgca-papers/issues/new" target="_blank" rel="noreferrer">{t.correct}</a><a href="https://github.com/longxiang-ai/awesome-zgca-papers" target="_blank" rel="noreferrer">{t.code}</a></div>
      </footer>

      {selected && (
        <div className="detail-overlay" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && closeDetail()}>
          <section className="detail-panel" role="dialog" aria-modal="true" aria-labelledby="detail-title">
            <button className="detail-close" onClick={closeDetail} aria-label={t.close}>×</button>
            <div className="detail-head">
              <div className="work-meta"><span className={`type-badge type-${selected.type}`}>{typeLabels[selected.type][locale]}</span><span>{selected.year}</span><span>·</span><span>{selected.venue}</span></div>
              <h2 id="detail-title">{selected.title}</h2>
              <div className="detail-institutions">{selected.institutions.map((id) => <span key={id} className={`institution-tag ${id}`}><i />{institutionNames[id][locale]}</span>)}</div>
            </div>
            <div className="detail-content">
              <section><h3>{t.abstract}</h3><p className="abstract-copy">{selected.abstract}</p></section>
              <section><h3>{t.authors}</h3><div className="author-grid">{selected.authors.map((author) => <div key={author.name}><span>{initials(author.name)}</span><p><b>{author.name}</b>{author.institutions?.map((id) => <small key={id}>{institutionNames[id].short}</small>)}</p></div>)}</div></section>
              <section><h3>{t.affiliationEvidence}</h3><div className="evidence-list">{selected.evidence.map((item) => <a key={`${item.source}-${item.institution}`} href={item.sourceUrl} target="_blank" rel="noreferrer"><span className={`evidence-dot evidence-${item.level}`} /><div><b>{evidenceLabels[item.level][locale]}</b><p>“{item.matchedText}”</p><small>{item.source} ↗</small></div></a>)}</div></section>
              <section><h3>{t.versions}</h3><div className="resource-list">{[...selected.versions, ...selected.links].map((item) => <a key={`${item.label}-${item.url}`} href={item.url} target="_blank" rel="noreferrer"><span>{item.label}</span><b>↗</b></a>)}</div></section>
              <section><h3>{t.cite}</h3><pre>{bibtex(selected)}</pre><button className="copy-button" onClick={copyCitation}>{copied ? `✓ ${t.copied}` : t.copyBibtex}</button></section>
            </div>
          </section>
        </div>
      )}
    </main>
  );
}
