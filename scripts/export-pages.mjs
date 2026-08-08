import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const clientDir = path.join(root, "dist", "client");
const outputDir = path.join(root, "pages-dist");
const basePath = (process.env.PAGES_BASE_PATH ?? "/awesome-zgca-papers").replace(/\/$/, "");

await rm(outputDir, { recursive: true, force: true });
await mkdir(outputDir, { recursive: true });
await cp(clientDir, outputDir, { recursive: true });

const workerUrl = pathToFileURL(path.join(root, "dist", "server", "index.js"));
workerUrl.searchParams.set("pages", Date.now().toString());
const { default: worker } = await import(workerUrl.href);
const response = await worker.fetch(
  new Request("https://longxiang-ai.github.io/", { headers: { accept: "text/html" } }),
  { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
  { waitUntil() {}, passThroughOnException() {} },
);

if (!response.ok) throw new Error(`Prerender failed with status ${response.status}`);

function withBasePath(content) {
  if (!basePath) return content;
  return content
    .replaceAll('"/_next/', `"${basePath}/_next/`)
    .replaceAll("'/_next/", `'${basePath}/_next/`)
    .replaceAll("url(/_next/", `url(${basePath}/_next/`)
    .replaceAll("url('/_next/", `url('${basePath}/_next/`)
    .replaceAll('url("/_next/', `url("${basePath}/_next/`);
}

const html = withBasePath(await response.text());
await writeFile(path.join(outputDir, "index.html"), html, "utf8");
await writeFile(path.join(outputDir, "404.html"), html, "utf8");
await writeFile(path.join(outputDir, ".nojekyll"), "", "utf8");

const cssManifest = JSON.parse(await readFile(path.join(outputDir, ".vite", "manifest.json"), "utf8"));
for (const value of Object.values(cssManifest)) {
  for (const cssFile of value.css ?? []) {
    const target = path.join(outputDir, cssFile);
    const css = await readFile(target, "utf8");
    await writeFile(target, withBasePath(css), "utf8");
  }
}

console.log(`GitHub Pages export created at ${outputDir} with base path ${basePath || "/"}`);
