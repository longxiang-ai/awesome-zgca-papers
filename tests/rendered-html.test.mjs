import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);

async function render(path = "/") {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(new Request(`http://localhost${path}`, { headers: { accept: "text/html" } }), {
    ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
  }, { waitUntil() {}, passThroughOnException() {} });
}

test("server-renders the completed bilingual research index", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /Awesome ZGCA Papers/i);
  assert.match(html, /研究成果索引/);
  assert.match(html, /Data Preparation for Large Language Models/);
  assert.match(html, /Zhongguancun Academy/);
  assert.doesNotMatch(html, /codex-preview|Your site is taking shape|react-loading-skeleton/i);
});

test("ships static data exports and site metadata", async () => {
  const [works, stats, bib, layout] = await Promise.all([
    readFile(new URL("../public/data/works.json", import.meta.url), "utf8"),
    readFile(new URL("../public/data/stats.json", import.meta.url), "utf8"),
    readFile(new URL("../public/data/works.bib", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
  ]);
  assert.ok(JSON.parse(works).length >= 7);
  assert.equal(JSON.parse(stats).total, JSON.parse(works).length);
  assert.match(bib, /@article|@inproceedings/);
  assert.match(layout, /Awesome ZGCA Papers/);
  assert.doesNotMatch(layout, /Starter Project|codex-preview/);
  await assert.rejects(access(new URL("../app/_sites-preview/SkeletonPreview.tsx", import.meta.url)));
});
