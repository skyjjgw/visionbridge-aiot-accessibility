import type { Metadata } from "next";
import "./globals.css";
import { UIShell } from "./ui-shell";

export const metadata: Metadata = {
  title: "视桥 · 城市盲道智能监管平台",
  description: "基于边缘智能与自有云直传的城市盲道违规占用监测及事件处置平台",
  icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
  openGraph: {
    title: "视桥 · 城市盲道智能监管平台",
    description: "从边缘识别到自有云处置，让每一段盲道都可感知、可追踪、可闭环。",
    images: ["/og.png"],
  },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body className="dark"><UIShell>{children}</UIShell></body>
    </html>
  );
}
