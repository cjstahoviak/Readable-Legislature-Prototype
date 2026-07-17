import Link from "next/link";
import type { Tab } from "@/lib/queries";

const TABS: { id: Tab; label: string }[] = [
  { id: "all", label: "All" },
  { id: "active", label: "Active" },
  { id: "law", label: "Became law" },
];

export function Tabs({
  active,
  searchParams,
}: {
  active: Tab;
  searchParams: URLSearchParams;
}) {
  return (
    <nav className="flex gap-1 border-b border-border" aria-label="Bill status">
      {TABS.map((tab) => {
        const params = new URLSearchParams(searchParams);
        if (tab.id === "all") params.delete("tab");
        else params.set("tab", tab.id);
        const qs = params.toString();
        const isActive = tab.id === active;
        return (
          <Link
            key={tab.id}
            href={qs ? `/?${qs}` : "/"}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium ${
              isActive
                ? "border-foreground text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
