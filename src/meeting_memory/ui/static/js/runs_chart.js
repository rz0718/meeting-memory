// Pure data and geometry helpers for the run-history chart. Keeping these
// separate from DOM construction makes ordering and scale behavior testable
// without a browser or a charting dependency.

const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

function compactDate(isoDate) {
  const [, month, day] = String(isoDate).split("-").map(Number);
  if (!month || !day || !MONTHS[month - 1]) return String(isoDate || "—");
  return `${MONTHS[month - 1]} ${String(day).padStart(2, "0")}`;
}

export function chartSeries(runs, limit = 12) {
  return (runs || [])
    .slice(0, limit)
    .reverse()
    .map((run) => {
      const date = String(run.started_at || "").slice(0, 10);
      return {
        date,
        dateLabel: compactDate(date),
        count: Number(run.counts?.objects_created || 0),
      };
    });
}

export function yTicks(values, targetTickCount = 4) {
  const maximum = Math.max(0, ...(values || []).map((value) => Number(value) || 0));
  if (maximum === 0) return [0, 1];

  const roughStep = maximum / Math.max(1, targetTickCount);
  const magnitude = 10 ** Math.floor(Math.log10(roughStep));
  const normalized = roughStep / magnitude;
  const niceMultiplier = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  const step = Math.max(1, niceMultiplier * magnitude);
  const ceiling = Math.ceil(maximum / step) * step;
  const ticks = [];
  for (let value = 0; value <= ceiling; value += step) ticks.push(value);
  return ticks;
}

export function pointX(index, count, left, width) {
  if (count <= 1) return left + width / 2;
  return left + (index / (count - 1)) * width;
}
