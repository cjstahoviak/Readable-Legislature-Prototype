import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import {
  AffectsYouBadge,
  ChamberBadge,
  ScoreChip,
  StageBadge,
} from "@/components/badges";
import { cleanName, designation, formatDate, partyState } from "@/lib/format";
import { getBillDetail, type ScoreEntry } from "@/lib/queries";
import {
  dimensionLabel,
  selectionsFromParams,
  valueLabel,
} from "@/lib/taxonomy";

export const revalidate = 300;

type Params = { congress: string; type: string; number: string };
type SearchParams = { [key: string]: string | string[] | undefined };

async function loadBill(params: Params) {
  const congress = Number(params.congress);
  const billNumber = Number(params.number);
  if (!Number.isInteger(congress) || !Number.isInteger(billNumber)) {
    return null;
  }
  return getBillDetail(congress, params.type.toLowerCase(), billNumber);
}

export async function generateMetadata({
  params,
}: {
  params: Promise<Params>;
}): Promise<Metadata> {
  const bill = await loadBill(await params);
  if (!bill) return {};
  return {
    title: `${designation(bill.billType, bill.billNumber)}: ${bill.title ?? ""}`,
    description: bill.summaryTldr ?? undefined,
  };
}

export default async function BillPage({
  params,
  searchParams,
}: {
  params: Promise<Params>;
  searchParams: Promise<SearchParams>;
}) {
  const bill = await loadBill(await params);
  if (!bill) notFound();

  const selections = selectionsFromParams(await searchParams);
  const selectedKeys = new Set(
    selections.map((s) => `${s.dimension}:${s.value}`),
  );
  const matchedScores = bill.scores.filter((s) =>
    selectedKeys.has(`${s.dimension}:${s.value}`),
  );
  const tgMatch = bill.targetGroups.some(
    (g) =>
      g.conditions.length > 0 &&
      g.conditions.every((c) => selectedKeys.has(`${c.dimension}:${c.value}`)),
  );

  const byDimension = new Map<string, ScoreEntry[]>();
  for (const s of bill.scores) {
    const list = byDimension.get(s.dimension) ?? [];
    list.push(s);
    byDimension.set(s.dimension, list);
  }

  const backHref = selections.length
    ? `/?${new URLSearchParams(
        selections.map((s) => [s.dimension, s.value]),
      ).toString()}`
    : "/";

  return (
    <article className="mx-auto max-w-3xl space-y-8">
      <div>
        <Link
          href={backHref}
          className="text-sm text-muted-foreground hover:text-foreground hover:underline"
        >
          ← All bills
        </Link>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <ChamberBadge billType={bill.billType} />
          <StageBadge stage={bill.stage} />
          {tgMatch && <AffectsYouBadge />}
        </div>
        <h1 className="mt-3 text-2xl font-bold leading-tight tracking-tight">
          <span className="text-muted-foreground">
            {designation(bill.billType, bill.billNumber)}
          </span>{" "}
          {bill.title}
        </h1>
        <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-2 text-sm md:grid-cols-3">
          {bill.sponsorName && (
            <div>
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                Sponsor
              </dt>
              <dd>
                {cleanName(bill.sponsorName)}{" "}
                {partyState(bill.sponsorParty, bill.sponsorState)}
              </dd>
            </div>
          )}
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">
              Cosponsors
            </dt>
            <dd>{bill.cosponsorCount}</dd>
          </div>
          {bill.introducedDate && (
            <div>
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                Introduced
              </dt>
              <dd>{formatDate(bill.introducedDate)}</dd>
            </div>
          )}
          {bill.committees.length > 0 && (
            <div className="col-span-2 md:col-span-3">
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                {bill.committees.length > 1 ? "Committees" : "Committee"}
              </dt>
              <dd>{bill.committees.join("; ")}</dd>
            </div>
          )}
        </dl>
      </div>

      <section>
        <h2 className="text-lg font-semibold">Summary</h2>
        {bill.summaryTldr ? (
          <>
            <p className="mt-2 font-medium">{bill.summaryTldr}</p>
            {bill.summaryOverview
              ?.split(/\n\n+/)
              .map((para, i) => (
                <p key={i} className="mt-3 text-sm leading-relaxed">
                  {para}
                </p>
              ))}
          </>
        ) : (
          <p className="mt-2 text-sm text-muted-foreground">
            This bill hasn&apos;t been analyzed yet.
          </p>
        )}
      </section>

      {bill.targetGroups.length > 0 && (
        <section>
          <h2 className="text-lg font-semibold">
            Who this bill explicitly affects
          </h2>
          <div className="mt-3 space-y-3">
            {bill.targetGroups.map((g) => {
              const isMatch =
                g.conditions.length > 0 &&
                g.conditions.every((c) =>
                  selectedKeys.has(`${c.dimension}:${c.value}`),
                );
              return (
                <div
                  key={g.id}
                  className={`rounded-lg border p-4 ${
                    isMatch
                      ? "border-green-600 bg-green-50 dark:border-green-700 dark:bg-green-950/40"
                      : "border-border bg-card"
                  }`}
                >
                  <div className="flex flex-wrap items-center gap-1.5 text-sm font-medium">
                    {g.conditions.length === 0 ? (
                      <span>
                        {g.criteria.length > 0
                          ? "A group outside our demographic categories"
                          : "Essentially everyone"}
                      </span>
                    ) : (
                      g.conditions.map((c, i) => (
                        <span
                          key={`${c.dimension}:${c.value}`}
                          className="flex items-center gap-1.5"
                        >
                          {i > 0 && (
                            <span className="text-xs text-muted-foreground">
                              AND
                            </span>
                          )}
                          <span className="rounded-full bg-secondary px-2.5 py-0.5 text-xs font-semibold">
                            {valueLabel(c.dimension, c.value)}
                          </span>
                        </span>
                      ))
                    )}
                    {isMatch && (
                      <span className="ml-auto text-xs font-semibold text-green-700 dark:text-green-400">
                        Matches you
                      </span>
                    )}
                  </div>
                  {g.criteria.length > 0 && (
                    <p className="mt-2 text-xs text-muted-foreground">
                      Specifically: {g.criteria.join("; ")}
                    </p>
                  )}
                  <p className="mt-2 text-sm">{g.reason}</p>
                  {g.agreement !== null && g.agreement < 1 && (
                    <p className="mt-1 text-xs text-muted-foreground">
                      Identified in {Math.round(g.agreement * 100)}% of
                      analysis runs.
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      )}

      {bill.scores.length > 0 && (
        <section>
          <h2 className="text-lg font-semibold">
            {selections.length > 0
              ? "Why this may matter to you"
              : "Who this bill is relevant to"}
          </h2>
          {selections.length > 0 &&
            (matchedScores.length > 0 ? (
              <div className="mt-3 space-y-3">
                {matchedScores.map((s) => (
                  <div
                    key={`${s.dimension}:${s.value}`}
                    className="rounded-lg border border-border bg-card p-4"
                  >
                    <ScoreChip
                      dimension={s.dimension}
                      value={s.value}
                      score={s.score}
                    />
                    <p className="mt-2 text-sm">{s.reason}</p>
                    {s.agreement !== null && s.agreement < 1 && (
                      <p className="mt-1 text-xs text-muted-foreground">
                        {Math.round(s.agreement * 100)}% agreement across
                        analysis runs.
                      </p>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <p className="mt-2 text-sm text-muted-foreground">
                None of your selections were flagged for this bill.
              </p>
            ))}

          <details className="mt-4" open={selections.length === 0}>
            <summary className="cursor-pointer text-sm font-medium text-muted-foreground hover:text-foreground">
              All flagged groups ({bill.scores.length})
            </summary>
            <div className="mt-3 space-y-4">
              {[...byDimension.entries()].map(([dim, entries]) => (
                <div key={dim}>
                  <h3 className="text-sm font-semibold">
                    {dimensionLabel(dim)}
                  </h3>
                  <ul className="mt-1 space-y-2">
                    {entries.map((s) => (
                      <li key={s.value} className="text-sm">
                        <ScoreChip
                          dimension={s.dimension}
                          value={s.value}
                          score={s.score}
                        />
                        <span className="ml-2 text-muted-foreground">
                          {s.reason}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          </details>
        </section>
      )}

      {bill.actions.length > 0 && (
        <section>
          <h2 className="text-lg font-semibold">History</h2>
          <ol className="mt-3 space-y-3 border-l border-border pl-4">
            {bill.actions.slice(0, 30).map((a, i) => (
              <li key={i} className="text-sm">
                <span className="font-medium">{formatDate(a.date)}</span>
                <span className="ml-2 text-muted-foreground">{a.text}</span>
              </li>
            ))}
            {bill.actions.length > 30 && (
              <li className="text-xs text-muted-foreground">
                … {bill.actions.length - 30} earlier actions on
                congress.gov
              </li>
            )}
          </ol>
        </section>
      )}

      <section className="rounded-lg border border-border bg-card p-4 text-sm">
        {bill.sourceUrl && (
          <p>
            <a
              href={bill.sourceUrl}
              className="font-medium underline hover:no-underline"
            >
              Read the full bill text on congress.gov →
            </a>
            {bill.textVersionType && (
              <span className="ml-2 text-xs text-muted-foreground">
                (version analyzed: {bill.textVersionType})
              </span>
            )}
          </p>
        )}
        {bill.llmModel && (
          <p className="mt-2 text-xs text-muted-foreground">
            Summary and scores generated by {bill.llmModel}
            {bill.llmSamples && bill.llmSamples > 1
              ? ` (${bill.llmSamples} analysis runs)`
              : ""}
            {bill.llmProcessedAt
              ? ` on ${formatDate(bill.llmProcessedAt)}`
              : ""}
            . AI-generated content can contain mistakes —{" "}
            <Link href="/methodology" className="underline">
              methodology
            </Link>
            .
          </p>
        )}
      </section>
    </article>
  );
}
