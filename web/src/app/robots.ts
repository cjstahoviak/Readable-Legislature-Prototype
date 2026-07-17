import type { MetadataRoute } from "next";

const BASE = process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000";

// Set NEXT_PUBLIC_NOINDEX=1 while iterating on a public preview to keep
// the half-finished site out of search engines; remove it at launch.
const NOINDEX = process.env.NEXT_PUBLIC_NOINDEX === "1";

export default function robots(): MetadataRoute.Robots {
  return {
    rules: NOINDEX
      ? { userAgent: "*", disallow: "/" }
      : { userAgent: "*", allow: "/" },
    sitemap: NOINDEX ? undefined : `${BASE}/sitemap.xml`,
  };
}
