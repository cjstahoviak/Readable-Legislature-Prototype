import { pool } from "./db";
import type { Selection } from "./taxonomy";

export type Tab = "all" | "active" | "law";

export interface ScoreChipData {
  dimension: string;
  value: string;
  score: number;
}

export interface BillListItem {
  id: number;
  congress: number;
  billType: string;
  billNumber: number;
  title: string | null;
  stage: string;
  summaryTldr: string | null;
  latestActionDate: string | null;
  policyArea: string | null;
  sponsorName: string | null;
  sponsorParty: string | null;
  sponsorState: string | null;
  /** The visitor's selections satisfy every condition of a target group. */
  tgMatch: boolean;
  maxScore: number | null;
  matched: boolean;
  chips: ScoreChipData[];
}

const TAB_FILTERS: Record<Tab, string> = {
  all: "TRUE",
  active: "b.stage NOT IN ('enacted', 'vetoed', 'failed')",
  law: "b.stage = 'enacted'",
};

/**
 * The feed query. With selections, bills rank lexicographically:
 * target-group match, then strongest score among the selections, then
 * breadth (sum), then recency — the ordering documented on the
 * methodology page. Without selections, plain recency.
 *
 * Target groups with zero conditions ("applies to everyone") never
 * count as a match — they would boost every bill for every visitor.
 */
export async function listBills(
  selections: Selection[],
  tab: Tab,
  limit = 50,
): Promise<BillListItem[]> {
  const tabFilter = TAB_FILTERS[tab];
  let sql: string;
  let params: unknown[];

  if (selections.length === 0) {
    sql = `
      SELECT b.id, b.congress, b.bill_type, b.bill_number, b.title, b.stage,
             b.summary_tldr, b.latest_action_date::text AS latest_action_date,
             b.policy_area,
             sp.full_name AS sponsor_name, sp.party AS sponsor_party,
             sp.state AS sponsor_state,
             FALSE AS tg_match, NULL::int AS max_score, FALSE AS matched
      FROM bills b
      LEFT JOIN LATERAL (
        SELECT l.full_name, l.party, l.state
        FROM bill_sponsors bs JOIN legislators l ON l.id = bs.legislator_id
        WHERE bs.bill_id = b.id AND bs.role = 'sponsor'
        LIMIT 1
      ) sp ON TRUE
      WHERE ${tabFilter}
      ORDER BY b.latest_action_date DESC NULLS LAST, b.id DESC
      LIMIT $1`;
    params = [limit];
  } else {
    const valuesSql = selections
      .map((_, i) => `($${i * 2 + 1}::text, $${i * 2 + 2}::text)`)
      .join(", ");
    params = selections.flatMap((s) => [s.dimension, s.value]);
    params.push(limit);
    sql = `
      WITH selections(dimension_key, value_key) AS (VALUES ${valuesSql}),
      scored AS (
        SELECT s.bill_id, MAX(s.score)::int AS max_score,
               SUM(s.score)::int AS sum_score
        FROM bill_impact_scores s
        JOIN selections sel
          ON sel.dimension_key = s.dimension_key
         AND sel.value_key = s.value_key
        WHERE s.score > 0
        GROUP BY s.bill_id
      ),
      tg AS (
        SELECT DISTINCT g.bill_id
        FROM bill_target_groups g
        WHERE EXISTS (
                SELECT 1 FROM bill_target_group_conditions c
                WHERE c.group_id = g.id)
          AND NOT EXISTS (
                SELECT 1 FROM bill_target_group_conditions c
                WHERE c.group_id = g.id
                  AND NOT EXISTS (
                        SELECT 1 FROM selections sel
                        WHERE sel.dimension_key = c.dimension_key
                          AND sel.value_key = c.value_key))
      )
      SELECT b.id, b.congress, b.bill_type, b.bill_number, b.title, b.stage,
             b.summary_tldr, b.latest_action_date::text AS latest_action_date,
             b.policy_area,
             sp.full_name AS sponsor_name, sp.party AS sponsor_party,
             sp.state AS sponsor_state,
             (t.bill_id IS NOT NULL) AS tg_match,
             sc.max_score,
             (t.bill_id IS NOT NULL OR sc.bill_id IS NOT NULL) AS matched
      FROM bills b
      LEFT JOIN scored sc ON sc.bill_id = b.id
      LEFT JOIN tg t ON t.bill_id = b.id
      LEFT JOIN LATERAL (
        SELECT l.full_name, l.party, l.state
        FROM bill_sponsors bs JOIN legislators l ON l.id = bs.legislator_id
        WHERE bs.bill_id = b.id AND bs.role = 'sponsor'
        LIMIT 1
      ) sp ON TRUE
      WHERE ${tabFilter}
      ORDER BY (t.bill_id IS NOT NULL OR sc.bill_id IS NOT NULL) DESC,
               (t.bill_id IS NOT NULL) DESC,
               sc.max_score DESC NULLS LAST,
               sc.sum_score DESC NULLS LAST,
               b.latest_action_date DESC NULLS LAST, b.id DESC
      LIMIT $${params.length}`;
  }

  const { rows } = await pool.query(sql, params);
  const bills: BillListItem[] = rows.map((r) => ({
    id: r.id,
    congress: r.congress,
    billType: r.bill_type,
    billNumber: r.bill_number,
    title: r.title,
    stage: r.stage,
    summaryTldr: r.summary_tldr,
    latestActionDate: r.latest_action_date,
    policyArea: r.policy_area,
    sponsorName: r.sponsor_name,
    sponsorParty: r.sponsor_party,
    sponsorState: r.sponsor_state,
    tgMatch: r.tg_match,
    maxScore: r.max_score,
    matched: r.matched,
    chips: [],
  }));

  await attachChips(bills, selections);
  return bills;
}

/** Card chips: matched selections when filtering, else top scores. */
async function attachChips(
  bills: BillListItem[],
  selections: Selection[],
): Promise<void> {
  if (bills.length === 0) return;
  const ids = bills.map((b) => b.id);
  const { rows } = await pool.query(
    `SELECT bill_id, dimension_key, value_key, score
     FROM bill_impact_scores
     WHERE bill_id = ANY($1) AND score > 0
     ORDER BY score DESC, dimension_key, value_key`,
    [ids],
  );
  const selected = new Set(selections.map((s) => `${s.dimension}:${s.value}`));
  const byBill = new Map<number, ScoreChipData[]>();
  for (const r of rows) {
    const chip = {
      dimension: r.dimension_key,
      value: r.value_key,
      score: r.score,
    };
    const list = byBill.get(r.bill_id) ?? [];
    list.push(chip);
    byBill.set(r.bill_id, list);
  }
  for (const bill of bills) {
    const all = byBill.get(bill.id) ?? [];
    const chips =
      selections.length > 0
        ? all.filter((c) => selected.has(`${c.dimension}:${c.value}`))
        : all;
    bill.chips = chips.slice(0, 4);
  }
}

// ---------------------------------------------------------------------------
// Bill detail
// ---------------------------------------------------------------------------
export interface ScoreEntry {
  dimension: string;
  value: string;
  score: number;
  reason: string;
  agreement: number | null;
}

export interface TargetGroup {
  id: number;
  reason: string;
  agreement: number | null;
  conditions: { dimension: string; value: string }[];
  criteria: string[];
}

export interface BillDetail {
  id: number;
  congress: number;
  billType: string;
  billNumber: number;
  title: string | null;
  stage: string;
  policyArea: string | null;
  introducedDate: string | null;
  sourceUrl: string | null;
  latestActionText: string | null;
  latestActionDate: string | null;
  summaryTldr: string | null;
  summaryOverview: string | null;
  textVersionType: string | null;
  llmModel: string | null;
  llmSamples: number | null;
  llmProcessedAt: string | null;
  llmPromptVersion: string | null;
  sponsorName: string | null;
  sponsorParty: string | null;
  sponsorState: string | null;
  cosponsorCount: number;
  committees: string[];
  actions: { date: string; text: string }[];
  scores: ScoreEntry[];
  targetGroups: TargetGroup[];
}

export async function getBillDetail(
  congress: number,
  billType: string,
  billNumber: number,
): Promise<BillDetail | null> {
  const { rows } = await pool.query(
    `SELECT b.*, b.introduced_date::text AS introduced_date_text,
            b.latest_action_date::text AS latest_action_date_text,
            b.llm_processed_at::text AS llm_processed_at_text
     FROM bills b
     WHERE b.congress = $1 AND b.bill_type = $2 AND b.bill_number = $3`,
    [congress, billType, billNumber],
  );
  if (rows.length === 0) return null;
  const b = rows[0];

  const [sponsor, cosponsors, committees, actions, scores, groups] =
    await Promise.all([
      pool.query(
        `SELECT l.full_name, l.party, l.state
         FROM bill_sponsors bs JOIN legislators l ON l.id = bs.legislator_id
         WHERE bs.bill_id = $1 AND bs.role = 'sponsor' LIMIT 1`,
        [b.id],
      ),
      pool.query(
        `SELECT count(*)::int AS n FROM bill_sponsors
         WHERE bill_id = $1 AND role = 'cosponsor'`,
        [b.id],
      ),
      pool.query(
        `SELECT committee_name FROM bill_committees
         WHERE bill_id = $1 ORDER BY committee_name`,
        [b.id],
      ),
      pool.query(
        `SELECT DISTINCT action_date::text AS date, action_text AS text
         FROM bill_status_history WHERE bill_id = $1
         ORDER BY date DESC, text`,
        [b.id],
      ),
      pool.query(
        `SELECT dimension_key, value_key, score, reason, agreement::float
         FROM bill_impact_scores
         WHERE bill_id = $1 AND score > 0
         ORDER BY score DESC, dimension_key, value_key`,
        [b.id],
      ),
      pool.query(
        `SELECT g.id, g.reason, g.agreement::float,
                COALESCE(json_agg(DISTINCT jsonb_build_object(
                  'dimension', c.dimension_key, 'value', c.value_key))
                  FILTER (WHERE c.group_id IS NOT NULL), '[]') AS conditions,
                COALESCE(json_agg(DISTINCT cr.criterion_text)
                  FILTER (WHERE cr.criterion_text IS NOT NULL), '[]') AS criteria
         FROM bill_target_groups g
         LEFT JOIN bill_target_group_conditions c ON c.group_id = g.id
         LEFT JOIN bill_target_group_criteria cr ON cr.group_id = g.id
         WHERE g.bill_id = $1
         GROUP BY g.id
         ORDER BY g.agreement DESC NULLS LAST, g.id`,
        [b.id],
      ),
    ]);

  return {
    id: b.id,
    congress: b.congress,
    billType: b.bill_type,
    billNumber: b.bill_number,
    title: b.title,
    stage: b.stage,
    policyArea: b.policy_area,
    introducedDate: b.introduced_date_text,
    sourceUrl: b.source_url,
    latestActionText: b.latest_action_text,
    latestActionDate: b.latest_action_date_text,
    summaryTldr: b.summary_tldr,
    summaryOverview: b.summary_overview,
    textVersionType: b.text_version_type,
    llmModel: b.llm_model,
    llmSamples: b.llm_samples,
    llmProcessedAt: b.llm_processed_at_text,
    llmPromptVersion: b.llm_prompt_version,
    sponsorName: sponsor.rows[0]?.full_name ?? null,
    sponsorParty: sponsor.rows[0]?.party ?? null,
    sponsorState: sponsor.rows[0]?.state ?? null,
    cosponsorCount: cosponsors.rows[0]?.n ?? 0,
    committees: committees.rows.map((r) => r.committee_name),
    actions: actions.rows,
    scores: scores.rows.map((r) => ({
      dimension: r.dimension_key,
      value: r.value_key,
      score: r.score,
      reason: r.reason,
      agreement: r.agreement,
    })),
    targetGroups: groups.rows.map((r) => ({
      id: r.id,
      reason: r.reason,
      agreement: r.agreement,
      conditions: r.conditions,
      criteria: r.criteria,
    })),
  };
}

/** All bills, for the sitemap. */
export async function listAllBillPaths(): Promise<
  { congress: number; billType: string; billNumber: number }[]
> {
  const { rows } = await pool.query(
    `SELECT congress, bill_type, bill_number FROM bills
     ORDER BY congress, bill_type, bill_number`,
  );
  return rows.map((r) => ({
    congress: r.congress,
    billType: r.bill_type,
    billNumber: r.bill_number,
  }));
}
