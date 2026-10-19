export const usd = (n: number) =>
  n === 0 ? "$0" : n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;

export const ms = (n: number | null) =>
  n == null ? "–" : n < 1000 ? `${Math.round(n)} ms` : `${(n / 1000).toFixed(1)} s`;

export const when = (iso: string) =>
  new Date(iso).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

export function percentile(values: number[], p: number) {
  if (!values.length) return null;
  const s = [...values].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.floor((p / 100) * s.length))];
}

export const MONTHLY_CAP = Number(process.env.NEXT_PUBLIC_MONTHLY_CAP_USD ?? 3);
