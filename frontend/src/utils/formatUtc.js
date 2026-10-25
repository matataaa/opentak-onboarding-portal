// The API sends naive UTC timestamps; add the missing zone so the browser shows local time.
export function formatUtc(iso) {
  if (!iso) return '-';
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/.test(iso);
  const d = new Date(hasZone ? iso : `${iso}Z`);
  if (Number.isNaN(d.getTime())) return '-';
  return d.toLocaleString();
}
