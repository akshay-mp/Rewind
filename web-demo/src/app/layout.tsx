import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { Toaster } from "@/components/ui/toaster";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Rewind × Deep Research — time-travel debugging for AI agents",
  description:
    "Capture a LangChain deep-research run, step down through the recorded spans, edit a prompt, and step up — only the divergent tail calls the live model.",
  keywords: [
    "Rewind",
    "LangChain",
    "Deep Research",
    "LangGraph",
    "agent debugging",
    "prompt engineering",
  ],
  authors: [{ name: "Rewind × Deep Research demo" }],
  icons: {
    icon: "/rewind.svg",
  },
  openGraph: {
    title: "Rewind × Deep Research",
    description:
      "Time-travel debugging for a LangChain deep-research agent.",
    siteName: "Rewind × Deep Research",
    type: "website",
  },
  twitter: {
    card: "summary_large_image",
    title: "Rewind × Deep Research",
    description:
      "Time-travel debugging for a LangChain deep-research agent.",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased bg-background text-foreground`}
      >
        {children}
        <Toaster />
      </body>
    </html>
  );
}
