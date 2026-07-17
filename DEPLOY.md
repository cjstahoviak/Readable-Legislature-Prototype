# Deploying the preview

Puts the current 10-bill snapshot online so we can iterate on look,
feel, and features before spending ingestion/scoring budget on more
data. Stack: [Neon](https://neon.tech) managed Postgres + [Vercel](https://vercel.com)
Next.js hosting — both free tiers, total cost $0. The site ships with
search-engine indexing switched off until `NEXT_PUBLIC_NOINDEX` is
removed at launch.

## 1. Create the database (Neon)

1. Create a Neon project (any current Postgres version; pick the
   region nearest your visitors — ideally the same one you'll pick
   for Vercel).
2. From the project's connection widget, note **both** connection
   strings:
   - the **direct** string (host like `ep-….aws.neon.tech`) — for
     admin work like the restore below and for running pipelines;
   - the **pooled** string (same host with `-pooler`) — for the app.
3. Restore the committed snapshot (schema + the 10 analyzed bills):

   ```bash
   psql "<direct-connection-string>" -f db/seed/example-bills.sql
   ```

   - Keep the query parameters Neon puts on the string
     (`sslmode=require`, …) — the connection is refused without TLS.
   - Needs a reasonably current `psql` (the dump uses `\restrict`,
     understood by client versions released after mid-2025). If your
     local `psql` is older or missing, use the postgres image:

     ```bash
     docker run --rm -i postgres:17 \
       psql "<direct-connection-string>" < db/seed/example-bills.sql
     ```
   - `NOTICE: … does not exist, skipping` messages are normal: the
     dump drops objects before creating them so it can be re-run.
4. Verify:

   ```bash
   psql "<direct-connection-string>" -c "SELECT count(*) FROM bills"
   # expect: 10
   ```

## 2. Deploy the site (Vercel)

1. Import the GitHub repository (Add New → Project).
2. Set **Root Directory to `web`** — the repo root is Python; the
   Next.js app lives in `web/`. The framework preset auto-detects
   Next.js; leave build settings at their defaults.
3. Environment variables (apply to Production and Preview):

   | Name | Value |
   |---|---|
   | `DATABASE_URL` | the **pooled** Neon string, query params included |
   | `NEXT_PUBLIC_SITE_URL` | `https://<project>.vercel.app` (update when a real domain exists) |
   | `NEXT_PUBLIC_NOINDEX` | `1` — keeps the preview out of search engines |

4. Deploy. Vercel builds production from the repo's production branch
   (default `main`); either merge the working branch into `main`
   first, or point Settings → Git → Production Branch at the working
   branch.

## 3. Verify the live site

- The feed lists all 10 bills; picking "About you" filters reorders
  them, with matched bills above the "Other recent bills" divider.
- A bill page shows the summary, target groups, score explanations,
  history, and the model/date transparency footer.
- `/methodology` renders the scoring scale and ranking rules.
- `/robots.txt` shows `Disallow: /` — the noindex gate is active.

## Updating the preview's data

The site reads whatever is in Neon, so the pipelines can write to it
from any machine: put the **direct** Neon string in `.env` as
`DATABASE_URL` and run the usual jobs.

```bash
python -m pipelines.ingest --congress 119 --bills hr-1234   # free (Congress.gov)
python -m pipelines.score_pending --max-bills 5             # spends Claude API budget
```

Ingesting without scoring costs nothing, but those bills appear as
"not yet analyzed" until scored. Bill pages are cached for 5 minutes
(ISR), so data changes appear within that window; the feed is
uncached and updates immediately.

To refresh the committed snapshot after updating a database:

```bash
pg_dump "<database-url>" --no-owner --no-privileges --clean --if-exists \
  > db/seed/example-bills.sql
```

## At launch

- Remove `NEXT_PUBLIC_NOINDEX` and redeploy — robots.txt flips to
  allow-all and starts advertising the sitemap.
- Point `NEXT_PUBLIC_SITE_URL` at the final domain and add the domain
  in Vercel.
- Revisit the Phase 3 items deferred for the preview: scheduled
  ingestion/scoring, the tiered backfill, and its budget.
