export function objectStatement(objects, objectId) {
  return objects.find((value) => value.id === objectId)?.statement || "";
}

export function filterSurvivorCandidates(candidates, query, limit = 300) {
  const needle = query.trim().toLowerCase();
  return candidates
    .filter((candidate) => {
      const id = String(candidate.id || "").toLowerCase();
      const title = String(candidate.title || "").toLowerCase();
      return !needle || id.includes(needle) || title.includes(needle);
    })
    .slice(0, limit);
}

export function mergeRequestBody({
  loserId,
  survivorId,
  note,
  statement,
  allowCrossCategory,
  allowConflictingNumbers,
}) {
  return {
    loser_id: loserId,
    survivor_id: survivorId,
    note: note.trim(),
    statement: statement.trim(),
    allow_cross_category: Boolean(allowCrossCategory),
    allow_conflicting_numbers: Boolean(allowConflictingNumbers),
  };
}

export function shouldRestoreApply(applyVersion, draftVersion) {
  return applyVersion === draftVersion;
}
