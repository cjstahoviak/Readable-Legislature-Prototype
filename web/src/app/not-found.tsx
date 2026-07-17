import Link from "next/link";

export default function NotFound() {
  return (
    <div className="py-24 text-center">
      <h1 className="text-2xl font-bold">Not found</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        That bill isn&apos;t in our database (yet).
      </p>
      <Link href="/" className="mt-4 inline-block text-sm underline">
        ← All bills
      </Link>
    </div>
  );
}
