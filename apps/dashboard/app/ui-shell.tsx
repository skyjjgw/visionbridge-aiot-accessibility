"use client";

import { ToastProvider } from "@heroui/react";

export function UIShell({ children }: { children: React.ReactNode }) {
  return (
    <>
      {children}
      <ToastProvider placement="bottom-end" />
    </>
  );
}
