// Typed access to the generated taxonomy. The JSON is produced from
// taxonomy.yaml (the single source of truth) by:
//   npm run taxonomy   (== python3 -m pipelines.export_taxonomy)
// Never hand-edit dimension or value data here or in the JSON.
import taxonomyData from "./taxonomy.generated.json";

export interface TaxonomyValue {
  id: string;
  label: string;
  description?: string;
  /** false for negative-space values the pipeline never scores. */
  scored: boolean;
}

export interface TaxonomyDimension {
  id: string;
  label: string;
  type: string;
  values: TaxonomyValue[];
}

export interface ScaleLevel {
  value: number;
  label: string;
  definition: string;
}

export const scale = taxonomyData.scale as ScaleLevel[];
export const dimensions = taxonomyData.dimensions as TaxonomyDimension[];

// The six dimensions shown by default in the filter panel; the rest sit
// behind the "More about you" expander (identity dimensions there
// deliberately — they are the most sensitive to ask about).
const PRIMARY_IDS = [
  "age",
  "income",
  "occupation",
  "employment_status",
  "health_coverage",
  "housing_status",
];

export const primaryDimensions = PRIMARY_IDS.map(
  (id) => dimensions.find((d) => d.id === id)!,
);
export const moreDimensions = dimensions.filter(
  (d) => !PRIMARY_IDS.includes(d.id),
);

const dimensionById = new Map(dimensions.map((d) => [d.id, d]));

export function dimensionLabel(id: string): string {
  return dimensionById.get(id)?.label ?? id;
}

export function valueLabel(dimensionId: string, valueId: string): string {
  const dim = dimensionById.get(dimensionId);
  return dim?.values.find((v) => v.id === valueId)?.label ?? valueId;
}

export function isValidSelection(
  dimensionId: string,
  valueId: string,
): boolean {
  const dim = dimensionById.get(dimensionId);
  return !!dim?.values.some((v) => v.id === valueId);
}

/** One (dimension, value) pair the visitor selected about themselves. */
export interface Selection {
  dimension: string;
  value: string;
}

/** Parse & validate filter selections from URL search params. */
export function selectionsFromParams(params: {
  [key: string]: string | string[] | undefined;
}): Selection[] {
  const selections: Selection[] = [];
  for (const dim of dimensions) {
    const raw = params[dim.id];
    const value = Array.isArray(raw) ? raw[0] : raw;
    if (value && isValidSelection(dim.id, value)) {
      selections.push({ dimension: dim.id, value });
    }
  }
  return selections;
}
