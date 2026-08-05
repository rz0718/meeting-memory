export const ORDINARY_BUCKETS = [
  "objects_created",
  "objects_refined",
  "objects_reconfirmed",
];
export const COLLAPSED_BY_DEFAULT = new Set(["objects_reconfirmed"]);

export function inboxGroups(detail) {
  return detail.groups
    .filter((group) => ORDINARY_BUCKETS.includes(group.bucket))
    .map((group) => ({
      ...group,
      collapsed: COLLAPSED_BY_DEFAULT.has(group.bucket),
    }));
}

export function objectCount(groups) {
  return groups.reduce((total, group) => total + group.rows.length, 0);
}

export function reviewCandidateCount(detail) {
  const group = detail.groups.find(
    (entry) => entry.bucket === "review_items_created",
  );
  return group ? group.rows.length : 0;
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
