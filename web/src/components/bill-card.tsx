import Link from "next/link";
import type { BillListItem } from "@/lib/queries";
import { cleanName, designation, formatDate, partyState } from "@/lib/format";
import {
  AffectsYouBadge,
  ChamberBadge,
  ScoreChip,
  StageBadge,
} from "./badges";

export function BillCard({
  bill,
  filterQuery,
}: {
  bill: BillListItem;
  /** Current filter query string, carried onto the detail link so the
   * detail page can highlight the visitor's matches. */
  filterQuery: string;
}) {
  const href = `/bill/${bill.congress}/${bill.billType}/${bill.billNumber}${
    filterQuery ? `?${filterQuery}` : ""
  }`;
  return (
    <article className="rounded-lg border border-border bg-card p-5 shadow-sm">
      <div className="flex flex-wrap items-center gap-2">
        <ChamberBadge billType={bill.billType} />
        <StageBadge stage={bill.stage} />
        {bill.tgMatch && <AffectsYouBadge />}
        {bill.policyArea && (
          <span className="ml-auto text-xs text-muted-foreground">
            {bill.policyArea}
          </span>
        )}
      </div>

      <h3 className="mt-3 text-lg font-semibold leading-snug">
        <Link href={href} className="hover:underline">
          <span className="text-muted-foreground">
            {designation(bill.billType, bill.billNumber)}
          </span>{" "}
          {bill.title}
        </Link>
      </h3>

      <p className="mt-2 text-sm text-muted-foreground">
        {bill.summaryTldr ?? "Not yet analyzed — summary coming soon."}
      </p>

      {bill.chips.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {bill.chips.map((c) => (
            <ScoreChip
              key={`${c.dimension}:${c.value}`}
              dimension={c.dimension}
              value={c.value}
              score={c.score}
            />
          ))}
        </div>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {bill.sponsorName && (
          <span>
            {cleanName(bill.sponsorName)}{" "}
            {partyState(bill.sponsorParty, bill.sponsorState)}
          </span>
        )}
        {bill.latestActionDate && (
          <span>Last action {formatDate(bill.latestActionDate)}</span>
        )}
        <Link href={href} className="ml-auto font-medium text-foreground hover:underline">
          View bill →
        </Link>
      </div>
    </article>
  );
}
