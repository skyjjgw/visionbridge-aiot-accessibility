import { copyFile, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const serverDir = resolve("dist/server");
const rscEntry = resolve(serverDir, "index.js");
const appEntry = resolve(serverDir, "app-entry.js");
const ssrEntry = resolve(serverDir, "ssr/index.js");

await copyFile(rscEntry, appEntry);

const ssrSource = await readFile(ssrEntry, "utf8");
const redirectedSource = ssrSource.replace(
  'import("../index.js")',
  'import("../app-entry.js")',
);

if (redirectedSource === ssrSource) {
  throw new Error("Unable to locate the vinext RSC entry import in the SSR worker");
}

await writeFile(ssrEntry, redirectedSource);
await writeFile(rscEntry, 'export { default } from "./ssr/index.js";\n');
