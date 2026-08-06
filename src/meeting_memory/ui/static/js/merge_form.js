export function objectStatement(objects, objectId) {
  return objects.find((value) => value.id === objectId)?.statement || "";
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
