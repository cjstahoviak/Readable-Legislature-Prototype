import { Pool } from "pg";

// One pool per process; the globalThis stash survives Next.js dev-mode
// module reloads that would otherwise leak connections.
const globalForPg = globalThis as unknown as { pgPool?: Pool };

export const pool =
  globalForPg.pgPool ??
  new Pool({
    connectionString: process.env.DATABASE_URL,
    max: 5, // Neon's pooler multiplexes; keep the local footprint small
  });

if (process.env.NODE_ENV !== "production") {
  globalForPg.pgPool = pool;
}
