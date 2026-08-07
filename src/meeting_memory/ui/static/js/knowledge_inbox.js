export const ORDINARY_BUCKETS = [
  "objects_created",
  "objects_refined",
  "objects_reconfirmed",
];
export const COLLAPSED_BY_DEFAULT = new Set(["objects_reconfirmed"]);
export const UNATTRIBUTED = "__unattributed__";

export function inboxGroups(detail) {
  return detail.groups
    .filter((group) => ORDINARY_BUCKETS.includes(group.bucket))
    .map((group) => ({
      ...group,
      key: group.bucket,
      collapsed: COLLAPSED_BY_DEFAULT.has(group.bucket),
    }));
}

// The same rows, keyed by where they came from instead of what happened to
// them. Built by walking the bucket groups outermost, so each row keeps the
// payload's own outcome label rather than a second copy of BUCKET_LABELS
// maintained here, and bucket order survives inside every source.
export function sourceGroups(detail) {
  const described = new Map((detail.sources || []).map((entry) => [entry.source, entry]));
  const groups = new Map();
  for (const group of inboxGroups(detail)) {
    for (const row of group.rows) {
      const key = row.source || UNATTRIBUTED;
      if (!groups.has(key)) {
        const meta = described.get(key);
        groups.set(key, {
          key,
          bucket: key,
          label: key === UNATTRIBUTED ? "Unattributed" : (meta ? meta.label : key),
          date: meta ? meta.date : null,
          kind: meta ? meta.kind : null,
          // Collapsing a source group by default would hide part of the answer
          // this grouping exists to give.
          collapsed: false,
          rows: [],
        });
      }
      groups.get(key).rows.push({ ...row, outcomeLabel: group.label });
    }
  }
  const ordered = [...groups.values()].sort(compareSourceGroups);
  return ordered.map((group) => ({ ...group, count: group.rows.length }));
}

function compareSourceGroups(left, right) {
  if (left.key === UNATTRIBUTED) return 1;
  if (right.key === UNATTRIBUTED) return -1;
  const dates = (right.date || "").localeCompare(left.date || "");
  if (dates !== 0) return dates;
  return left.label.localeCompare(right.label, undefined, { sensitivity: "base" });
}

export function attributedSourceCount(detail) {
  const found = new Set();
  for (const group of inboxGroups(detail)) {
    for (const row of group.rows) {
      if (row.source) found.add(row.source);
    }
  }
  return found.size;
}

export function groupsFor(detail, groupBy) {
  return groupBy === "source" ? sourceGroups(detail) : inboxGroups(detail);
}

export function objectCount(groups) {
  return groups.reduce((total, group) => total + group.rows.length, 0);
}

export function reviewCandidateRows(detail) {
  const group = detail.groups.find(
    (entry) => entry.bucket === "review_items_created",
  );
  return group ? group.rows : [];
}

export function reviewCandidateCount(detail) {
  return reviewCandidateRows(detail).length;
}

// What the run queued is history; what can still be worked is not. A case that
// has been resolved, rejected, or deleted since is no longer in the queue, so
// only a pending one has anywhere to open.
export function openReviewIds(detail) {
  return reviewCandidateRows(detail)
    .filter((row) => row.present && row.status === "pending")
    .map((row) => row.id);
}

export function matchesFilter(row, { search = "", category = "" } = {}) {
  if (category && row.category !== category) return false;
  if (!search) return true;
  const needle = search.toLowerCase();
  const title = (row.title || "").toLowerCase();
  const statement = (row.statement || "").toLowerCase();
  return title.includes(needle) || statement.includes(needle);
}

export function filterGroups(groups, filters) {
  return groups
    .map((group) => ({
      ...group,
      rows: group.rows.filter((row) => matchesFilter(row, filters)),
    }))
    .filter((group) => group.rows.length > 0);
}

export function defaultSelectionId(groups) {
  for (const group of groups) {
    for (const row of group.rows) {
      if (row.present) return row.id;
    }
  }
  return null;
}
