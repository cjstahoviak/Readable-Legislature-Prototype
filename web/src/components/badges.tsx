// Pill badges and score chips, following the v0 design language:
// rounded-full pills, outline chamber badges, tinted stage badges, and
// score chips where green = high (2) and amber = moderate (1).
import { chamber, stageLabel } from "@/lib/format";
import { dimensionLabel, valueLabel } from "@/lib/taxonomy";

const PILL =
  "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap";

export function ChamberBadge({ billType }: { billType: string }) {
  return <span className={`${PILL} border-border text-foreground`}>{chamber(billType)}</span>;
}

const STAGE_STYLES: Record<string, string> = {
  introduced: "border-border bg-secondary text-secondary-foreground",
  committee:
    "border-blue-200 bg-blue-100 text-blue-900 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-200",
  floor:
    "border-purple-200 bg-purple-100 text-purple-900 dark:border-purple-900 dark:bg-purple-950 dark:text-purple-200",
  passed_house:
    "border-amber-200 bg-amber-100 text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200",
  passed_senate:
    "border-amber-200 bg-amber-100 text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200",
  to_president:
    "border-amber-300 bg-amber-200 text-amber-900 dark:border-amber-800 dark:bg-amber-900 dark:text-amber-100",
  enacted:
    "border-green-200 bg-green-100 text-green-900 dark:border-green-900 dark:bg-green-950 dark:text-green-200",
  vetoed:
    "border-red-200 bg-red-100 text-red-900 dark:border-red-900 dark:bg-red-950 dark:text-red-200",
  failed:
    "border-red-200 bg-red-100 text-red-900 dark:border-red-900 dark:bg-red-950 dark:text-red-200",
};

export function StageBadge({ stage }: { stage: string }) {
  const style = STAGE_STYLES[stage] ?? STAGE_STYLES.introduced;
  return <span className={`${PILL} ${style}`}>{stageLabel(stage)}</span>;
}

/** Shown when the visitor's selections satisfy a bill's target group. */
export function AffectsYouBadge() {
  return (
    <span
      className={`${PILL} border-green-700 bg-green-700 text-white dark:border-green-600 dark:bg-green-600`}
    >
      Directly affects you
    </span>
  );
}

const SCORE_STYLES: Record<number, string> = {
  2: "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300",
  1: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
};

export function ScoreChip({
  dimension,
  value,
  score,
}: {
  dimension: string;
  value: string;
  score: number;
}) {
  const style = SCORE_STYLES[score] ?? SCORE_STYLES[1];
  const impact = score === 2 ? "direct impact" : "moderate impact";
  return (
    <span
      className={`inline-block max-w-full truncate align-bottom text-xs px-2 py-0.5 rounded-full whitespace-nowrap ${style}`}
      title={`${dimensionLabel(dimension)}: ${impact}`}
    >
      {dimensionLabel(dimension)}: {valueLabel(dimension, value)}
    </span>
  );
}
