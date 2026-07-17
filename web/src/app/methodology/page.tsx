import type { Metadata } from "next";
import { dimensions, scale } from "@/lib/taxonomy";

export const metadata: Metadata = {
  title: "Methodology",
  description:
    "How Readable Legislature summarizes bills, scores demographic relevance, ranks results, and what it deliberately does not do.",
};

export default function MethodologyPage() {
  return (
    <article className="prose-sm mx-auto max-w-3xl space-y-8">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">How this works</h1>
        <p className="mt-2 text-sm leading-relaxed">
          Readable Legislature parses bills in the U.S. Congress into
          plain-language summaries and per-demographic relevance scores,
          so you can see which bills touch your life. One principle
          shapes everything here: <strong>relevance, not verdicts</strong>.
          We tell you a bill matters to people like you; whether it is
          good or bad for you is your judgment to make, not ours.
        </p>
      </div>

      <section>
        <h2 className="text-lg font-semibold">Where the data comes from</h2>
        <p className="mt-2 text-sm leading-relaxed">
          Bill text, sponsors, committees, and status history come from
          the official{" "}
          <a href="https://api.congress.gov" className="underline">
            Congress.gov API
          </a>
          , maintained by the Library of Congress. We sync updated bills
          daily. Every bill page links back to its canonical page on
          congress.gov, and shows which text version was analyzed.
        </p>
      </section>

      <section>
        <h2 className="text-lg font-semibold">How relevance scores work</h2>
        <p className="mt-2 text-sm leading-relaxed">
          A language model (Claude, by Anthropic) reads each bill&apos;s
          full text and scores its relevance to {dimensions.length}{" "}
          demographic dimensions — things like age bracket, occupation,
          health coverage, and veteran status. Each group gets a score on
          a three-point scale:
        </p>
        <ul className="mt-3 space-y-2 text-sm">
          {scale.map((level) => (
            <li key={level.value}>
              <strong>
                {level.value} — {level.label}:
              </strong>{" "}
              {level.definition}
            </li>
          ))}
        </ul>
        <p className="mt-3 text-sm leading-relaxed">
          Scores rise only when a bill&apos;s effect differs <em>because
          of</em> a trait — a bill that affects everyone equally scores 0
          on every group, even though it affects you. Most groups score 0
          on most bills; that is by design, and it is what makes the
          nonzero scores meaningful. Alongside the scores, the model
          extracts each bill&apos;s explicitly targeted populations
          (e.g. &ldquo;veterans <em>with</em> a service-connected
          disability&rdquo;) and writes the plain-language summaries you
          see on each page.
        </p>
      </section>

      <section id="ranking">
        <h2 className="text-lg font-semibold">How ranking works</h2>
        <p className="mt-2 text-sm leading-relaxed">
          When you make selections in the &ldquo;About you&rdquo; panel,
          bills are ordered by four rules, applied in order:
        </p>
        <ol className="mt-3 list-decimal space-y-1 pl-6 text-sm">
          <li>
            Bills that <strong>explicitly target people matching your
            selections</strong> (every condition of one of the bill&apos;s
            target groups is satisfied) come first.
          </li>
          <li>
            Then by the <strong>strongest single score</strong> among
            your selections.
          </li>
          <li>
            Then by <strong>breadth</strong> — how many of your
            selections the bill touches.
          </li>
          <li>Then by <strong>recency</strong> of legislative action.</li>
        </ol>
        <p className="mt-3 text-sm leading-relaxed">
          That&apos;s the whole algorithm. There are no engagement
          signals, no personalization models, and no hidden weights.
          Bills that don&apos;t match your selections are still shown
          below the divider, ordered by recency.
        </p>
      </section>

      <section>
        <h2 className="text-lg font-semibold">
          What we deliberately don&apos;t do
        </h2>
        <ul className="mt-2 list-disc space-y-1 pl-6 text-sm">
          <li>
            <strong>No verdicts.</strong> Scores and reasons never say
            whether an effect is good or bad for a group — only that the
            group is affected. Direction is the reader&apos;s call.
          </li>
          <li>
            <strong>No accounts and no tracking of your selections.</strong>{" "}
            The &ldquo;About you&rdquo; panel writes to your browser and
            the page address only. We never store your demographics on a
            server.
          </li>
          <li>
            <strong>No editorial curation.</strong> Every bill in the
            corpus is treated identically by the same pipeline.
          </li>
        </ul>
      </section>

      <section>
        <h2 className="text-lg font-semibold">Taxonomy choices</h2>
        <p className="mt-2 text-sm leading-relaxed">
          The {dimensions.length} dimensions are oriented around{" "}
          <em>policy impact</em> — the hooks federal statutes actually
          pivot on, like income brackets, health-coverage sources, and
          occupational sectors — rather than social-identity categories
          for their own sake. Notably, the taxonomy has no race or
          ethnicity dimension: most legislation reaches people through
          economic and programmatic hooks, and where a bill explicitly
          targets a protected class, that targeting is preserved verbatim
          in the bill&apos;s &ldquo;who this affects&rdquo; criteria. We
          measure how often that happens and will revisit the taxonomy if
          the data says we should. Geographic (state-level) relevance is
          planned but not yet included.
        </p>
      </section>

      <section>
        <h2 className="text-lg font-semibold">Limitations, honestly</h2>
        <ul className="mt-2 list-disc space-y-1 pl-6 text-sm">
          <li>
            Summaries and scores are AI-generated and can be wrong.
            Where you see an &ldquo;agreement&rdquo; percentage, the bill
            was analyzed multiple times and that share of runs agreed —
            lower numbers mean a more borderline judgment.
          </li>
          <li>
            Scores describe a bill&apos;s <em>text</em>, which is not the
            same as its odds of becoming law. Most bills never leave
            committee.
          </li>
          <li>
            Every bill page names the model and date of its analysis, and
            links to the official text so you can check our work.
          </li>
        </ul>
      </section>
    </article>
  );
}
