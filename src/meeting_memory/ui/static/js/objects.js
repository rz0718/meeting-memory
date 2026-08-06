// Knowledge-object side peek and the two audited Tab 1 actions.
//
// Merge runs the merge --into --reviewer --note --dry-run contract, with the
// cross-category and conflicting-number overrides as explicit checkboxes that
// are never applied silently. Removal is not one-click: flagging only adds an
// ID to the session basket, which is executed against a written ID inventory
// whose count and SHA-256 the apply call asserts.

import { api } from "./api.js";
import { calendarDate, callout, el, icon, instant, mount, property } from "./dom.js";
import { evidenceList } from "./evidence.js";
import {
  filterSurvivorCandidates,
  mergeRequestBody,
  moveActiveIndex,
  objectStatement,
  shouldRestoreApply,
} from "./merge_form.js";
import { busy, closeModal, openModal, openPeek, reportError, toast } from "./ui.js";
import { refreshBasket, refreshKnowledge, store } from "./store.js";

export async function openObjectPeek(objectId) {
  openPeek(objectId, busy("Loading knowledge object…"));
  try {
    const object = await api.knowledge(objectId);
    openPeek(object.title, objectView(object));
  } catch (error) {
    reportError(error);
  }
}

export function objectView(object, { actions = true } = {}) {
  const untrustworthyEvidence = (object.evidence || []).some(
    (entry) => entry.freshness_label === "drifted" || entry.freshness_label === "unavailable"
  );

  const status = el("div", { class: "props" }, [
    ...property("Status", object.status),
    ...property("Effective date", calendarDate(object.effective_date)),
    ...property("Last confirmed", calendarDate(object.last_confirmed)),
  ]);

  const props = el("div", { class: "props" }, [
    ...property("Category", object.category),
    ...property("Owner", object.owner),
    ...property("Confidence", object.confidence),
    ...property("Created", instant(object.created_at)),
    ...property("Updated", instant(object.updated_at)),
    ...property(
      "Canonical file",
      el("span", { class: "mono", text: object.file_path || "unknown" })
    ),
    ...property(
      "Related objects",
      object.related_objects.length
        ? el(
            "span",
            {},
            object.related_objects.map((id) =>
              el("a", {
                href: "#",
                class: "mono",
                text: `${id} `,
                onClick: (event) => {
                  event.preventDefault();
                  openObjectPeek(id);
                },
              })
            )
          )
        : null
    ),
  ]);

  const history = el("div", {}, [
    el("h2", { class: "section", text: "History" }),
    el(
      "ul",
      { class: "list-tight" },
      object.history.map((entry) =>
        el("li", { text: entry.date ? `${entry.date} — ${entry.text}` : entry.text })
      )
    ),
  ]);

  return el("div", {}, [
    object.pending_review_ids.length
      ? el("div", { class: "callout callout--info" }, [
          icon("queue"),
          el("div", { class: "callout__body" }, [
            el("div", {
              text: `${object.pending_review_ids.length} pending review case${
                object.pending_review_ids.length === 1 ? "" : "s"
              } names this object.`,
            }),
            el("div", { class: "mono secondary", text: object.pending_review_ids.join(", ") }),
          ]),
        ])
      : null,
    el("h2", { class: "section", text: "Statement" }),
    el("div", { class: "compare__text", text: object.statement }),
    el("h2", { class: "section", text: "Evidence" }),
    untrustworthyEvidence
      ? callout(
          "warning",
          "Source text has changed",
          "One or more cited excerpts have changed since extraction, or could not be read. " +
            "The excerpt below shows the source as it reads now, not as it read at extraction time."
        )
      : null,
    evidenceList(object.evidence),
    el("h2", { class: "section", text: "Status" }),
    status,
    props,
    history,
    actions
      ? el("div", { class: "actionbar" }, [
          el("button", {
            class: "btn",
            onClick: () => openMergeDialog(object.id),
          }, [icon("merge"), el("span", { text: "Merge into…" })]),
          el("button", {
            class: "btn btn--danger",
            onClick: () => flagForRemoval(object.id),
          }, [icon("trash"), el("span", { text: "Flag for removal" })]),
        ])
      : null,
  ]);
}

export async function flagForRemoval(objectId) {
  try {
    store.basket = await api.basketAdd(objectId);
    await refreshBasket();
    toast(`${objectId} added to the removal basket. Nothing has been removed yet.`, {
      link: { label: "Open removal basket", action: openBasketDialog },
    });
  } catch (error) {
    reportError(error);
  }
}

// -- merge ------------------------------------------------------------------

export async function openMergeDialog(loserId) {
  if (!store.knowledge.length) await refreshKnowledge();
  const candidates = store.knowledge.filter((value) => value.id !== loserId);
  const loser = store.knowledge.find((value) => value.id === loserId);

  if (!loser) {
    openModal({
      title: `Merge ${loserId}`,
      body: callout(
        "danger",
        "Knowledge object not found",
        `${loserId} is no longer available. Refresh the knowledge list before trying again.`
      ),
      actions: [el("button", { class: "btn", onClick: closeModal, text: "Close" })],
    });
    return;
  }

  const safeLoserId = String(loserId).replace(/[^A-Za-z0-9_-]/g, "-");
  const selectorId = `merge-survivor-${safeLoserId}`;
  const search = el("input", {
    role: "combobox",
    placeholder: "Search by knowledge ID or title…",
    autocomplete: "off",
    "aria-label": "Keep (survivor)",
    "aria-autocomplete": "list",
    "aria-controls": `${selectorId}-results`,
    "aria-expanded": "false",
  });
  const results = el("div", {
    id: `${selectorId}-results`,
    class: "merge-survivor__results",
    role: "listbox",
    hidden: true,
  });
  const searchView = el("div", { class: "merge-survivor__search" }, [search, results]);
  const selectedView = el("div", {
    class: "merge-survivor__selected",
    hidden: true,
  });
  const selector = el("div", { class: "merge-survivor" }, [searchView, selectedView]);
  const note = el("textarea", { placeholder: "Why these two records are one fact…" });
  const retiringStatement = objectStatement(store.knowledge, loserId);
  const survivorStatement = el("div", { class: "compare__text" });
  const finalStatement = el("textarea", {
    required: true,
    placeholder: "The canonical statement after this merge…",
  });
  const crossCategory = el("input", { type: "checkbox" });
  const conflictingNumbers = el("input", { type: "checkbox" });
  const output = el("div", {});

  const applyButton = el(
    "button",
    { class: "btn btn--primary", disabled: true },
    [icon("check"), el("span", { text: "Apply merge" })]
  );
  const previewButton = el("button", { class: "btn", disabled: true }, [
    icon("doc"),
    el("span", { text: "Preview merge" }),
  ]);
  const useSurvivor = el("button", {
    class: "btn",
    type: "button",
    text: "Use survivor",
    disabled: true,
  });
  const useRetiring = el("button", {
    class: "btn",
    type: "button",
    text: "Use retiring",
  });
  let survivorId = "";
  let matches = [];
  let activeIndex = -1;
  let resultsOpen = false;
  let draftVersion = 0;

  const invalidatePreview = () => {
    draftVersion += 1;
    applyButton.disabled = true;
    mount(output);
  };

  const updateSelectedSurvivor = ({ invalidate = true } = {}) => {
    const selectedStatement = objectStatement(store.knowledge, survivorId);
    survivorStatement.textContent = selectedStatement || "No survivor selected.";
    survivorStatement.classList.toggle("secondary", !selectedStatement);
    useSurvivor.disabled = !survivorId;
    previewButton.disabled = !survivorId;
    if (invalidate) {
      finalStatement.value = selectedStatement;
      finalStatement.setCustomValidity("");
      invalidatePreview();
    }
  };

  const setResultsOpen = (open) => {
    resultsOpen = Boolean(open) && !searchView.hidden;
    results.hidden = !resultsOpen;
    search.setAttribute("aria-expanded", String(resultsOpen));
    if (!resultsOpen) {
      activeIndex = -1;
      search.removeAttribute("aria-activedescendant");
    }
  };

  const setActiveIndex = (nextIndex) => {
    activeIndex = nextIndex >= 0 && nextIndex < matches.length ? nextIndex : -1;
    const options = results.querySelectorAll('[role="option"]');
    options.forEach((option, index) => {
      option.classList.toggle("is-active", index === activeIndex);
    });
    if (activeIndex < 0) {
      search.removeAttribute("aria-activedescendant");
      return;
    }
    const activeOption = options[activeIndex];
    search.setAttribute("aria-activedescendant", activeOption.id);
    activeOption.scrollIntoView({ block: "nearest" });
  };

  let selectSurvivor;
  const renderResults = () => {
    matches = filterSurvivorCandidates(candidates, search.value);
    activeIndex = -1;
    search.removeAttribute("aria-activedescendant");
    mount(
      results,
      matches.length
        ? matches.map((candidate, index) =>
            el("button", {
              id: `${selectorId}-option-${index}`,
              class: "merge-survivor__option",
              type: "button",
              role: "option",
              "aria-selected": candidate.id === survivorId,
              onMouseDown: (event) => event.preventDefault(),
              onClick: () => selectSurvivor(candidate.id),
            }, [
              el("span", {
                class: "merge-survivor__title",
                text: candidate.title || "Untitled",
              }),
              el("span", { class: "merge-survivor__id", text: candidate.id }),
            ])
          )
        : el("div", {
            class: "merge-survivor__empty",
            role: "status",
            text: candidates.length
              ? "No matching knowledge objects"
              : "No other knowledge objects available",
          })
    );
  };

  const showSearch = () => {
    search.value = "";
    searchView.hidden = false;
    selectedView.hidden = true;
    renderResults();
    search.focus();
    setResultsOpen(true);
  };

  selectSurvivor = (nextId) => {
    if (!nextId) return;
    const candidate = candidates.find((value) => value.id === nextId);
    if (!candidate) return;
    const changed = nextId !== survivorId;
    survivorId = nextId;
    mount(selectedView, [
      el("div", { class: "merge-survivor__selection" }, [
        el("span", {
          class: "merge-survivor__title",
          text: candidate.title || "Untitled",
        }),
        el("span", { class: "merge-survivor__id", text: candidate.id }),
      ]),
      el("button", {
        class: "btn",
        type: "button",
        text: "Change",
        onClick: showSearch,
      }),
    ]);
    setResultsOpen(false);
    searchView.hidden = true;
    selectedView.hidden = false;
    updateSelectedSurvivor({ invalidate: changed });
  };

  search.addEventListener("input", () => {
    renderResults();
    setResultsOpen(true);
  });
  search.addEventListener("focus", () => {
    renderResults();
    setResultsOpen(true);
  });
  search.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setResultsOpen(true);
      setActiveIndex(
        moveActiveIndex(activeIndex, matches.length, event.key === "ArrowDown" ? 1 : -1)
      );
      return;
    }
    if (event.key === "Enter" && resultsOpen) {
      const candidate =
        activeIndex >= 0 ? matches[activeIndex] : matches.length === 1 ? matches[0] : null;
      if (candidate) {
        event.preventDefault();
        selectSurvivor(candidate.id);
      }
      return;
    }
    if (event.key === "Escape" && resultsOpen) {
      event.preventDefault();
      setResultsOpen(false);
    }
  });
  selector.addEventListener("focusout", (event) => {
    if (!selector.contains(event.relatedTarget)) setResultsOpen(false);
  });
  note.addEventListener("input", invalidatePreview);
  finalStatement.addEventListener("input", () => {
    finalStatement.setCustomValidity("");
    invalidatePreview();
  });
  crossCategory.addEventListener("change", invalidatePreview);
  conflictingNumbers.addEventListener("change", invalidatePreview);
  useSurvivor.addEventListener("click", () => {
    finalStatement.value = objectStatement(store.knowledge, survivorId);
    finalStatement.setCustomValidity("");
    invalidatePreview();
  });
  useRetiring.addEventListener("click", () => {
    finalStatement.value = retiringStatement;
    finalStatement.setCustomValidity("");
    invalidatePreview();
  });
  renderResults();
  updateSelectedSurvivor({ invalidate: false });

  const body = (survivorId) => mergeRequestBody({
    loserId,
    survivorId,
    note: note.value,
    statement: finalStatement.value,
    allowCrossCategory: crossCategory.checked,
    allowConflictingNumbers: conflictingNumbers.checked,
  });

  previewButton.addEventListener("click", async () => {
    if (!survivorId) return;
    if (!finalStatement.value.trim()) {
      finalStatement.setCustomValidity("Final statement is required.");
      finalStatement.reportValidity();
      return;
    }
    invalidatePreview();
    const previewVersion = draftVersion;
    mount(output, busy("Running deterministic dry run…"));
    try {
      const result = await api.merge({ ...body(survivorId), dry_run: true });
      if (previewVersion !== draftVersion) return;
      mount(output, mergePreview(result.preview));
      applyButton.disabled = false;
    } catch (error) {
      if (previewVersion !== draftVersion) return;
      mount(output, el("div", { class: "callout callout--danger" }, [
        icon("alert"),
        el("div", { class: "callout__body", text: `${error.type}: ${error.message}` }),
      ]));
      reportError(error);
    }
  });

  applyButton.addEventListener("click", async () => {
    const applyVersion = draftVersion;
    applyButton.disabled = true;
    try {
      const result = await api.merge({ ...body(survivorId), dry_run: false });
      closeModal();
      await refreshKnowledge();
      toast(
        `Merged ${result.applied.loser_id} into ${result.applied.survivor_id}; ` +
          `${result.applied.evidence_added} evidence entries added.`,
        {
          kind: "good",
          link: {
            label: `Open ${result.applied.survivor_id}`,
            action: () => openObjectPeek(result.applied.survivor_id),
          },
        }
      );
    } catch (error) {
      if (shouldRestoreApply(applyVersion, draftVersion)) {
        applyButton.disabled = false;
      }
      reportError(error);
    }
  });

  openModal({
    title: `Merge ${loserId}`,
    body: el("div", {}, [
      el("div", { class: "secondary" }, [
        el("span", { text: "Retiring " }),
        el("span", { class: "mono", text: loserId }),
        el("span", { text: ` (${loser.category})` }),
      ]),
      el("div", { class: "props" }, [
        ...property("Keep (survivor)", selector),
        ...property(
          "Source statements",
          el("div", { class: "compare merge-statements" }, [
            el("div", { class: "compare__col" }, [
              el("div", { class: "compare__head", text: "Retiring" }),
              el("div", { class: "compare__text", text: retiringStatement }),
            ]),
            el("div", { class: "compare__col" }, [
              el("div", { class: "compare__head", text: "Selected survivor" }),
              survivorStatement,
            ]),
          ])
        ),
        ...property(
          "Final statement",
          el("div", {}, [
            finalStatement,
            el("div", { class: "merge-statement-actions" }, [useSurvivor, useRetiring]),
          ])
        ),
        ...property("Reviewer", store.session ? store.session.reviewer : ""),
        ...property("Note", note),
        ...property(
          "Overrides",
          el("div", {}, [
            el("label", { class: "checkline" }, [
              crossCategory,
              el("span", { text: "Allow cross-category merge (--allow-cross-category)" }),
            ]),
            el("label", { class: "checkline" }, [
              conflictingNumbers,
              el("span", {
                text:
                  "Allow conflicting numbers, signs, or units " +
                  "(--allow-conflicting-numbers)",
              }),
            ]),
          ])
        ),
      ]),
      output,
    ]),
    actions: [
      el("button", { class: "btn", onClick: closeModal, text: "Cancel" }),
      previewButton,
      applyButton,
    ],
  });
}

function mergePreview(preview) {
  return el("div", {}, [
    el("div", { class: "callout callout--info" }, [
      icon("doc"),
      el("div", { class: "callout__body" }, [
        el("div", {
          class: "callout__title",
          text: `Dry run: ${preview.loser_id} → ${preview.survivor_id}`,
        }),
        el("div", {
          text:
            `${preview.evidence_added} evidence entries added; ` +
            `${preview.retargeted_review_ids.length} reviews and ` +
            `${preview.retargeted_object_ids.length} related objects retargeted.`,
        }),
      ]),
    ]),
    el("div", { class: "compare merge-statements" }, [
      el("div", { class: "compare__col" }, [
        el("div", { class: "compare__head", text: "Survivor before" }),
        el("div", { class: "compare__text", text: preview.before?.statement || "" }),
      ]),
      el("div", { class: "compare__col" }, [
        el("div", { class: "compare__head", text: "Final statement" }),
        el("div", { class: "compare__text", text: preview.after?.statement || "" }),
      ]),
    ]),
    el("pre", { class: "preview", text: JSON.stringify(preview, null, 2) }),
  ]);
}

// -- removal basket ---------------------------------------------------------

export async function openBasketDialog() {
  const basket = await refreshBasket();
  const note = el("textarea", { placeholder: "Why these objects are being removed…" });
  const output = el("div", {});
  const applyButton = el("button", { class: "btn btn--danger", disabled: true }, [
    icon("trash"),
    el("span", { text: "Delete" }),
  ]);
  let approved = null;

  const previewButton = el("button", { class: "btn" }, [
    icon("doc"),
    el("span", { text: "Preview" }),
  ]);
  previewButton.addEventListener("click", async () => {
    applyButton.disabled = true;
    approved = null;
    mount(output, busy("Writing the approved ID inventory and dry-running removal…"));
    try {
      const result = await api.basketPreview({ note: note.value });
      approved = result.basket;
      mount(output, removalPreview(result));
      applyButton.disabled = false;
    } catch (error) {
      mount(output, el("div", { class: "callout callout--danger" }, [
        icon("alert"),
        el("div", { class: "callout__body", text: `${error.type}: ${error.message}` }),
      ]));
      reportError(error);
    }
  });

  applyButton.addEventListener("click", async () => {
    if (!approved) return;
    applyButton.disabled = true;
    try {
      const result = await api.basketApply({
        note: note.value,
        confirm_count: approved.inventory_count,
        inventory_sha256: approved.inventory_sha256,
      });
      closeModal();
      await refreshKnowledge();
      await refreshBasket();
      toast(
        `Removed ${result.applied.object_ids.length} canonical objects. ` +
          `Cleanup manifest: ${result.applied.manifest_path}`,
        { kind: "good" }
      );
    } catch (error) {
      applyButton.disabled = false;
      reportError(error);
    }
  });

  openModal({
    title: "Removal basket",
    body: el("div", {}, [
      basket.count
        ? el(
            "div",
            { class: "rows" },
            basket.objects.map((object) =>
              el("div", { class: "row" }, [
                el("span", { class: "row__title", text: object.title || object.id }),
                el("span", { class: "row__id", text: object.id }),
                el("button", {
                  class: "row__open",
                  text: "Remove from basket",
                  style: "opacity:1",
                  onClick: async () => {
                    await api.basketRemove(object.id);
                    closeModal();
                    openBasketDialog();
                  },
                }),
              ])
            )
          )
        : el("div", { class: "empty", text: "The basket is empty." }),
      el("div", { class: "props" }, [
        ...property("Reviewer", store.session ? store.session.reviewer : ""),
        ...property("Note", note),
      ]),
      el("div", { class: "callout callout--warning" }, [
        icon("alert"),
        el("div", { class: "callout__body" }, [
          el("div", { class: "callout__title", text: "Removal is permanent" }),
          el("div", {
            text:
              "Previewing writes the exact newline-delimited ID inventory this " +
              "removal will execute against. Applying asserts that file's count " +
              "and SHA-256, so only the bytes you approved can run.",
          }),
        ]),
      ]),
      output,
    ]),
    actions: [
      el("button", { class: "btn", onClick: closeModal, text: "Cancel" }),
      basket.count
        ? el("button", {
            class: "btn",
            text: "Put back",
            onClick: async () => {
              await api.basketClear();
              closeModal();
              await refreshBasket();
            },
          })
        : null,
      previewButton,
      applyButton,
    ].filter(Boolean),
  });
}

function removalPreview(result) {
  const basket = result.basket;
  return el("div", {}, [
    el("div", { class: "callout callout--danger" }, [
      icon("alert"),
      el("div", { class: "callout__body" }, [
        el("div", { class: "callout__title", text: "This apply will assert" }),
        el("div", { class: "props" }, [
          ...property("Inventory file", el("span", { class: "mono", text: basket.inventory_path })),
          ...property("--confirm-count", String(basket.inventory_count)),
          ...property(
            "--inventory-sha256",
            el("span", { class: "mono", text: basket.inventory_sha256 })
          ),
        ]),
      ]),
    ]),
    el("pre", { class: "preview", text: JSON.stringify(result.preview, null, 2) }),
  ]);
}

export function basketSummary(basket, onOpen) {
  if (!basket) return el("div", { class: "muted", text: "Removal basket: empty" });
  return el(
    "button",
    { class: "nav-item", onClick: onOpen, style: "width:100%" },
    [
      icon("trash"),
      el("span", { text: "Removal basket" }),
      el("span", { class: "nav-item__count", text: String(basket.count) }),
    ]
  );
}
