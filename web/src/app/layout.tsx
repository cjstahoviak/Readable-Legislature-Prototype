import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "Readable Legislature",
    template: "%s · Readable Legislature",
  },
  description:
    "Congressional bills in plain language, with relevance scores for the demographics they affect. Relevance, not verdicts.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="flex min-h-screen flex-col">
        <header className="border-b border-border bg-card">
          <div className="mx-auto flex w-full max-w-5xl items-center gap-6 px-4 py-3">
            <Link href="/" className="text-lg font-bold tracking-tight">
              Readable Legislature
            </Link>
            <nav className="ml-auto flex items-center gap-4 text-sm">
              <Link href="/" className="text-muted-foreground hover:text-foreground">
                Bills
              </Link>
              <Link
                href="/methodology"
                className="text-muted-foreground hover:text-foreground"
              >
                Methodology
              </Link>
            </nav>
          </div>
        </header>

        <main className="mx-auto w-full max-w-5xl flex-1 px-4 py-8">
          {children}
        </main>

        <footer className="border-t border-border">
          <div className="mx-auto w-full max-w-5xl px-4 py-6 text-xs text-muted-foreground">
            <p>
              Bill data from the official{" "}
              <a
                href="https://api.congress.gov"
                className="underline hover:text-foreground"
              >
                Congress.gov API
              </a>
              . Summaries and relevance scores are AI-generated and can
              contain mistakes — see{" "}
              <Link href="/methodology" className="underline hover:text-foreground">
                how this works
              </Link>
              . This site reports relevance, never verdicts.
            </p>
          </div>
        </footer>
      </body>
    </html>
  );
}
