import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "BMS 日志分析 Agent",
  description: "多通道BMS日志、规则证据、关键波形与使用者自带Key的Agent分析。",
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body className="antialiased">{children}</body>
    </html>
  );
}
