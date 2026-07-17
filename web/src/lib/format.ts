// Display helpers for bill identity and lifecycle.

const DESIGNATIONS: Record<string, string> = {
  hr: "H.R.",
  s: "S.",
  hjres: "H.J.Res.",
  sjres: "S.J.Res.",
  hconres: "H.Con.Res.",
  sconres: "S.Con.Res.",
  hres: "H.Res.",
  sres: "S.Res.",
};

export function designation(billType: string, billNumber: number): string {
  return `${DESIGNATIONS[billType] ?? billType.toUpperCase()} ${billNumber}`;
}

export function chamber(billType: string): "House" | "Senate" {
  return billType.startsWith("h") ? "House" : "Senate";
}

export const STAGE_LABELS: Record<string, string> = {
  introduced: "Introduced",
  committee: "In committee",
  floor: "On the floor",
  passed_house: "Passed House",
  passed_senate: "Passed Senate",
  to_president: "To the President",
  enacted: "Became law",
  vetoed: "Vetoed",
  failed: "Failed",
};

export function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage;
}

/** Congress.gov fullName often embeds "[R-TX-8]" — strip it. */
export function cleanName(name: string | null): string {
  return (name ?? "").replace(/\s*\[[^\]]*\]/g, "").trim();
}

/** "Rep. Jane Doe (D-CA)" style suffix from party + state. */
export function partyState(
  party: string | null,
  state: string | null,
): string {
  const p = party?.[0]?.toUpperCase();
  if (p && state) return `(${p}-${state})`;
  if (state) return `(${state})`;
  return "";
}

export function formatDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(`${iso.slice(0, 10)}T00:00:00Z`);
  return d.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}
