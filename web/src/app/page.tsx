import { Suspense } from "react";
import Link from "next/link";
import { BillCard } from "@/components/bill-card";
import { FilterPanel } from "@/components/filter-panel";
import { Tabs } from "@/components/tabs";
import { listBills, type Tab } from "@/lib/queries";
import { selectionsFromParams } from "@/lib/taxonomy";

export const dynamic = "force-dynamic";

type SearchParams = { [key: string]: string | string[] | undefined };

function toQueryString(params: SearchParams): URLSearchParams {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    const value = Array.isArray(v) ? v[0] : v;
    if (value) qs.set(k, value);
  }
  return qs;
}

export default async function HomePage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const params = await searchParams;
  const selections = selectionsFromParams(params);
  const rawTab = Array.isArray(params.tab) ? params.tab[0] : params.tab;
  const tab: Tab = rawTab === "active" || rawTab === "law" ? rawTab : "all";

  const bills = await listBills(selections, tab);
  const matchedBills = bills.filter((b) => b.matched);
  const otherBills = bills.filter((b) => !b.matched);
  const filterQuery = toQueryString(params).toString();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">
          Bills in Congress
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Plain-language summaries with relevance scores for the people
          each bill affects.{" "}
          {selections.length > 0 && (
            <>
              Ranked by relevance to your {selections.length} selection
              {selections.length > 1 ? "s" : ""} —{" "}
              <Link
                href="/methodology#ranking"
                className="underline hover:text-foreground"
              >
                how ranking works
              </Link>
              .
            </>
          )}
        </p>
      </div>

      <Suspense>
        <FilterPanel />
      </Suspense>

      <Tabs active={tab} searchParams={toQueryString(params)} />

      {bills.length === 0 && (
        <p className="py-12 text-center text-sm text-muted-foreground">
          No bills here yet.
        </p>
      )}

      <div className="space-y-4">
        {selections.length > 0 ? (
          <>
            {matchedBills.map((bill) => (
              <BillCard key={bill.id} bill={bill} filterQuery={filterQuery} />
            ))}
            {matchedBills.length === 0 && (
              <p className="rounded-lg border border-border bg-card p-5 text-sm text-muted-foreground">
                No analyzed bills match your selections yet. Bills below
                are shown by recency instead.
              </p>
            )}
            {otherBills.length > 0 && (
              <div className="flex items-center gap-3 pt-2">
                <div className="h-px flex-1 bg-border" />
                <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Other recent bills
                </span>
                <div className="h-px flex-1 bg-border" />
              </div>
            )}
            {otherBills.map((bill) => (
              <BillCard key={bill.id} bill={bill} filterQuery={filterQuery} />
            ))}
          </>
        ) : (
          bills.map((bill) => (
            <BillCard key={bill.id} bill={bill} filterQuery={filterQuery} />
          ))
        )}
      </div>
    </div>
  );
}
