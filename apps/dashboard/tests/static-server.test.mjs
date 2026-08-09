import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { test } from "node:test";

const port = 43120;
const origin = `http://127.0.0.1:${port}`;

test("production start server returns its referenced assets", async (context) => {
  const server = spawn(process.execPath, ["scripts/serve-static.mjs"], {
    cwd: new URL("..", import.meta.url),
    env: { ...process.env, HOST: "127.0.0.1", PORT: String(port) },
    stdio: ["ignore", "pipe", "pipe"],
  });
  context.after(() => server.kill());

  await Promise.race([
    once(server.stdout, "data"),
    once(server, "exit").then(([code]) => {
      throw new Error(`Static server exited before startup with code ${code}`);
    }),
  ]);

  const home = await fetch(`${origin}/`);
  assert.equal(home.status, 200);
  assert.match(home.headers.get("content-type") ?? "", /^text\/html/);
  const html = await home.text();
  const cssPath = html.match(/href="([^"]+\.css)"/)?.[1];
  const scriptPath = html.match(/(?:src|href)="([^"]+\.js)"/)?.[1];
  assert.ok(cssPath, "rendered page must reference a stylesheet");
  assert.ok(scriptPath, "rendered page must reference JavaScript");

  const [css, script, post] = await Promise.all([
    fetch(new URL(cssPath, origin)),
    fetch(new URL(scriptPath, origin)),
    fetch(`${origin}/`, { method: "POST" }),
  ]);
  assert.equal(css.status, 200);
  assert.match(css.headers.get("content-type") ?? "", /^text\/css/);
  assert.equal(script.status, 200);
  assert.match(script.headers.get("content-type") ?? "", /^text\/javascript/);
  assert.equal(post.status, 405);
});
