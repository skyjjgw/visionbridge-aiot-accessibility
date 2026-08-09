import assert from "node:assert/strict";
import { readFile, stat } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", {
      headers: { accept: "text/html" },
    }),
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
}

test("server-renders the VisionBridge dashboard", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<html lang="zh-CN">/);
  assert.match(html, /<title>视桥 · 城市盲道智能监管平台<\/title>/);
  assert.match(html, /城市盲道智能监管平台/);
  assert.match(html, /自有云接入/);
  assert.match(html, /树莓派实机/);
});

test("static deployment contains every referenced asset", async () => {
  const html = await readFile(
    new URL("../static-deploy/index.html", import.meta.url),
    "utf8",
  );
  const assetPaths = [
    ...html.matchAll(/(?:href|src)="(\/assets\/[^"]+)"/g),
  ].map((match) => match[1]);

  assert.ok(assetPaths.length >= 2, "expected CSS and JavaScript asset references");
  for (const assetPath of new Set(assetPaths)) {
    const asset = await stat(
      new URL(`../static-deploy${assetPath}`, import.meta.url),
    );
    assert.ok(asset.isFile(), `missing static asset: ${assetPath}`);
    assert.ok(asset.size > 0, `empty static asset: ${assetPath}`);
  }
});

test("documents the direct-to-own-cloud data path and offline fallback", async () => {
  const source = await readFile(
    new URL("../app/vision-bridge-dashboard.tsx", import.meta.url),
    "utf8",
  );

  assert.match(source, /自有云直传/);
  assert.match(source, /Bearer REST/);
  assert.match(source, /运行链路不经过研华或其他第三方平台/);
  assert.match(source, /\/api\/v1\/telemetry/);
  assert.match(source, /linkStatus:\s*"offline"/);
  assert.doesNotMatch(source, /云桥接|签名 REST/);
});

test("volunteer operations declare admin protection and load independent data sources", async () => {
  const source = await readFile(
    new URL("../app/volunteer-admin.tsx", import.meta.url),
    "utf8",
  );

  assert.match(source, /管理员认证保护/);
  assert.match(source, /Promise\.allSettled/);
  assert.match(source, /状态链一致性/);
  assert.match(source, /数据口径/);
  assert.doesNotMatch(source, /Authorization/);
  assert.doesNotMatch(source, /管理员验证|进入审核台/);
});

test("active maps refresh automatically without recreating the AMap instance", async () => {
  const dashboard = await readFile(
    new URL("../app/vision-bridge-dashboard.tsx", import.meta.url),
    "utf8",
  );
  const admin = await readFile(
    new URL("../app/volunteer-admin.tsx", import.meta.url),
    "utf8",
  );

  assert.match(dashboard, /setInterval\(\(\) => void refresh\(\), 3000\)/);
  assert.match(dashboard, /markersRef\.current/);
  assert.match(dashboard, /visibilitychange/);
  assert.match(dashboard, /active: "未接单"/);
  assert.doesNotMatch(dashboard, /\["all", "active", "dispatched", "cleared"\]/);
  assert.match(admin, /setInterval\(\(\) => void load\(true\), 4000\)/);
});

test("edge fleet supports multi-device live WebRTC with HLS fallback", async () => {
  const fleet = await readFile(
    new URL("../app/device-fleet.tsx", import.meta.url),
    "utf8",
  );
  const dashboard = await readFile(
    new URL("../app/vision-bridge-dashboard.tsx", import.meta.url),
    "utf8",
  );

  assert.match(dashboard, /DeviceFleetView/);
  assert.match(fleet, /\/api\/v1\/devices/);
  assert.match(fleet, /WebRTC 低延迟/);
  assert.match(fleet, /HLS 兼容/);
  assert.match(fleet, /devices\.map/);
  assert.match(fleet, /setInterval\(\(\) => void refresh\(\), 3000\)/);
  assert.match(fleet, /visibilitychange/);
  assert.match(fleet, /实时识别画面/);
});
