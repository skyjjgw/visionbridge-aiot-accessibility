import { cp, mkdir, rm, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const projectRoot = resolve(import.meta.dirname, "..");
const clientRoot = resolve(projectRoot, "dist", "client");
const outputRoot = resolve(projectRoot, "static-deploy");
const workerPath = resolve(projectRoot, "dist", "server", "index.js");

await rm(outputRoot, { force: true, recursive: true });
await mkdir(outputRoot, { recursive: true });
await cp(clientRoot, outputRoot, { recursive: true, force: true });

const workerUrl = pathToFileURL(workerPath);
workerUrl.searchParams.set("export", Date.now().toString());
const { default: worker } = await import(workerUrl.href);
const response = await worker.fetch(
  new Request("http://localhost/", { headers: { accept: "text/html" } }),
  {
    ASSETS: {
      fetch: async () => new Response("Not found", { status: 404 }),
    },
  },
  {
    waitUntil() {},
    passThroughOnException() {},
  },
);

if (!response.ok) {
  throw new Error(`Static export failed with HTTP ${response.status}`);
}

const html = await response.text();
if (!html.includes("视桥") || !html.includes("自有云接入")) {
  throw new Error("Static export does not contain the expected dashboard content");
}

await writeFile(resolve(outputRoot, "index.html"), html, "utf8");
console.log(`Static dashboard exported to ${outputRoot}`);
