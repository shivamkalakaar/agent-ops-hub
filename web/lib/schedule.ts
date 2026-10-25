// When scheduled LinkedIn posts go out: the weekday cron in web/vercel.json (app/api/cron/publish).

/** The next weekday cron slot (Mon-Fri, 15:30 UTC: 8:30 am PDT / 7:30 am PST) at least 10 minutes from now. */
export function nextSlot(now = new Date()): Date {
  const t = new Date(now.getTime() + 10 * 60_000);
  const slot = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), t.getUTCDate(), 15, 30));
  if (slot <= t) slot.setUTCDate(slot.getUTCDate() + 1);
  while (slot.getUTCDay() === 0 || slot.getUTCDay() === 6) slot.setUTCDate(slot.getUTCDate() + 1);
  return slot;
}
