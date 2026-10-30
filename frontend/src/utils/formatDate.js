import { useQuery } from '@tanstack/react-query';
import { settingsAPI } from '../services/api';

// Portal-wide date format (admin setting `date_format`):
//   eu  -> 31-10-2026 14:30
//   us  -> 10/31/2026 2:30 PM
//   iso -> 2026-10-31 14:30
const DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/;
const HAS_ZONE = /(Z|[+-]\d{2}:?\d{2})$/;

const pad = (n) => String(n).padStart(2, '0');

function joinDate(year, month, day, fmt) {
  if (fmt === 'us') return `${pad(month)}/${pad(day)}/${year}`;
  if (fmt === 'iso') return `${year}-${pad(month)}-${pad(day)}`;
  return `${pad(day)}-${pad(month)}-${year}`;
}

function joinTime(hours, minutes, fmt) {
  if (fmt === 'us') return `${hours % 12 || 12}:${pad(minutes)} ${hours < 12 ? 'AM' : 'PM'}`;
  return `${pad(hours)}:${pad(minutes)}`;
}

// `utc: true` treats timestamps without a zone as UTC (the API sends naive UTC for some fields).
function toDate(value, utc) {
  if (utc && typeof value === 'string' && !HAS_ZONE.test(value)) return new Date(`${value}Z`);
  return new Date(value);
}

// Returns '' for empty/invalid values so call sites can keep their own fallback.
export function formatDate(value, fmt) {
  if (!value) return '';
  const dateOnly = typeof value === 'string' && value.match(DATE_ONLY);
  if (dateOnly) return joinDate(dateOnly[1], Number(dateOnly[2]), Number(dateOnly[3]), fmt);
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '';
  return joinDate(d.getFullYear(), d.getMonth() + 1, d.getDate(), fmt);
}

export function formatDateTime(value, fmt, { utc = false } = {}) {
  if (!value) return '';
  if (typeof value === 'string' && DATE_ONLY.test(value)) return formatDate(value, fmt);
  const d = toDate(value, utc);
  if (Number.isNaN(d.getTime())) return '';
  return `${joinDate(d.getFullYear(), d.getMonth() + 1, d.getDate(), fmt)} ${joinTime(d.getHours(), d.getMinutes(), fmt)}`;
}

export function useDateFormat() {
  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: async () => {
      const response = await settingsAPI.get();
      return response.data;
    },
    staleTime: 5 * 60 * 1000,
  });
  return settings?.date_format || 'eu';
}
