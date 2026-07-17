import type { MetadataRoute } from "next";
import { listAllBillPaths } from "@/lib/queries";

export const dynamic = "force-dynamic";

const BASE = process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000";

export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const bills = await listAllBillPaths();
  return [
    { url: BASE, changeFrequency: "daily", priority: 1 },
    { url: `${BASE}/methodology`, changeFrequency: "monthly", priority: 0.5 },
    ...bills.map((b) => ({
      url: `${BASE}/bill/${b.congress}/${b.billType}/${b.billNumber}`,
      changeFrequency: "weekly" as const,
      priority: 0.7,
    })),
  ];
}
