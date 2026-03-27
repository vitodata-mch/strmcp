import type { Metadata } from "next";
import { ThemeProvider } from "@/components/theme-provider";
import "./globals.css";

export const metadata: Metadata = {
  title: "Vitodata · Live Diktat",
  description: "Streaming-Transkription mit Sliding-Window-Whisper & Selbstkorrektur",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="de"
      className="h-full antialiased dark"
      suppressHydrationWarning
    >
      <head>
        {/* Set data-theme before first paint to prevent flash */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){var t=localStorage.getItem('strmcp-theme');document.documentElement.setAttribute('data-theme',(['obsidian','sardinia','forest'].includes(t)?t:'obsidian'));})()`,
          }}
        />
      </head>
      <body className="min-h-full flex flex-col">
        <ThemeProvider>{children}</ThemeProvider>
      </body>
    </html>
  );
}
