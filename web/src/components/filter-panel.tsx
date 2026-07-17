"use client";

// The "About you" panel: one optional select per taxonomy dimension.
// Selections live in the URL (shareable, server-rendered) and are
// mirrored to localStorage so a returning visitor keeps their setup.
// Nothing is ever sent to or stored on the server.
import { useEffect, useRef } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  dimensions,
  moreDimensions,
  primaryDimensions,
  valueLabel,
  type TaxonomyDimension,
} from "@/lib/taxonomy";

const STORAGE_KEY = "rl-filters";
const dimensionIds = dimensions.map((d) => d.id);

function DimensionSelect({
  dim,
  value,
  onChange,
}: {
  dim: TaxonomyDimension;
  value: string;
  onChange: (dimId: string, valueId: string) => void;
}) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block font-medium">{dim.label}</span>
      <select
        value={value}
        onChange={(e) => onChange(dim.id, e.target.value)}
        className="w-full rounded-md border border-border bg-card px-2 py-1.5 text-sm"
      >
        <option value="">Any</option>
        {dim.values.map((v) => (
          <option key={v.id} value={v.id}>
            {v.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function FilterPanel() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const restored = useRef(false);

  const current = new Map<string, string>();
  for (const id of dimensionIds) {
    const v = searchParams.get(id);
    if (v) current.set(id, v);
  }

  // First visit to a bare URL: restore the visitor's saved selections.
  useEffect(() => {
    if (restored.current) return;
    restored.current = true;
    if (current.size > 0) return;
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved) {
      const params = new URLSearchParams(searchParams.toString());
      let any = false;
      for (const [k, v] of new URLSearchParams(saved)) {
        if (dimensionIds.includes(k)) {
          params.set(k, v);
          any = true;
        }
      }
      if (any) router.replace(`${pathname}?${params.toString()}`);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function apply(dimId: string, valueId: string) {
    const params = new URLSearchParams(searchParams.toString());
    if (valueId) params.set(dimId, valueId);
    else params.delete(dimId);
    const filtersOnly = new URLSearchParams();
    for (const id of dimensionIds) {
      const v = params.get(id);
      if (v) filtersOnly.set(id, v);
    }
    window.localStorage.setItem(STORAGE_KEY, filtersOnly.toString());
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  }

  function clearAll() {
    const params = new URLSearchParams(searchParams.toString());
    for (const id of dimensionIds) params.delete(id);
    window.localStorage.removeItem(STORAGE_KEY);
    router.replace(
      params.size ? `${pathname}?${params.toString()}` : pathname,
      { scroll: false },
    );
  }

  return (
    <section
      aria-label="About you"
      className="rounded-lg border border-border bg-card p-5 shadow-sm"
    >
      <div className="flex items-baseline justify-between gap-4">
        <h2 className="text-base font-semibold">About you</h2>
        {current.size > 0 && (
          <button
            onClick={clearAll}
            className="text-sm text-muted-foreground hover:text-foreground hover:underline"
          >
            Clear all ({current.size})
          </button>
        )}
      </div>
      <p className="mt-1 text-xs text-muted-foreground">
        Pick what applies to you and bills are ranked by relevance. Your
        selections stay in your browser and this page&apos;s address —
        they are never stored on our servers.
      </p>

      <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-3">
        {primaryDimensions.map((dim) => (
          <DimensionSelect
            key={dim.id}
            dim={dim}
            value={current.get(dim.id) ?? ""}
            onChange={apply}
          />
        ))}
      </div>

      <details className="mt-3">
        <summary className="cursor-pointer text-sm font-medium text-muted-foreground hover:text-foreground">
          More about you ({moreDimensions.length} more)
        </summary>
        <div className="mt-3 grid grid-cols-2 gap-3 md:grid-cols-3">
          {moreDimensions.map((dim) => (
            <DimensionSelect
              key={dim.id}
              dim={dim}
              value={current.get(dim.id) ?? ""}
              onChange={apply}
            />
          ))}
        </div>
      </details>

      {current.size > 0 && (
        <div className="mt-4 flex flex-wrap gap-1.5 border-t border-border pt-3">
          {[...current.entries()].map(([dimId, valId]) => (
            <button
              key={dimId}
              onClick={() => apply(dimId, "")}
              className="inline-flex items-center gap-1 rounded-full bg-secondary px-2.5 py-0.5 text-xs font-medium text-secondary-foreground hover:bg-accent"
              title="Remove"
            >
              {valueLabel(dimId, valId)}
              <span aria-hidden>×</span>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}
