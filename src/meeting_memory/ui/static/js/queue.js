// Tab 2 — Review Queue.
//
// This is ReviewTriage as a screen, with the same gates: show evidence, show
// the suggestion, accept or override, deterministic dry run, confirm, apply.
// The UI's contribution is side-by-side evidence and an editable form; the
// transaction model is unchanged. There is no bulk accept, and no path from
// this form to disk that skips an inspected dry run.

import { api, streamSuggestions } from "./api.js";
import { badge, el, icon, mount, property, statusCue } from "./dom.js";
import {
  candidateProperties,
  comparePanel,
  priorityCue,
  resolutionPanel,
  reviewHeader,
  suggestionComment,
} from "./reviewview.js";
import { busy, empty, openModal, closeModal, reportError, toast } from "./ui.js";
import { setReviewCounts } from "./store.js";

const ACTIONS = [
  "refine",
  "replace",
  "reconfirm",
  "create-separate",
  "keep-existing",
  "merge-duplicate",
];
const STATUSES = ["proposed", "approved", "unclear", "deprecated"];
const CONFIDENCES = ["high", "medium", "low"];
const CATEGORIES = [
  "decisions",
  "policies",
  "processes",
  "projects",
  "systems",
  "metrics",
  "people-and-ownership",
];
// Stale candidate evidence may only be promoted; for the two actions that
// promote nothing the override is invalid and the checkbox is not rendered.
const STALE_ELIGIBLE = new Set(["replace", "refine", "reconfirm", "create-separate"]);
const METADATA_ACTIONS = new Set(["replace", "refine", "create-separate"]);

const state = {
  filters: {
    status: "pending",
    priority: "all",
    reason: "",
    category: "",
    existing_id: "",
    source: "",
  },
  list: null,
  selectedId: null,
  detail: null,
  form: null,
  preview: null,
  context: null,
};

export const queueView = {
  id: "queue",
  label: "Review queue",
  icon: "queue",
  title: "Review queue",

  count() {
    return state.list ? state.list.count : null;
  },

  async render(ctx) {
    state.context = ctx;
    ctx.content.style.padding = "0";
    mount(ctx.filters, filterBar(ctx));
    mount(ctx.content, busy("Loading the review queue…"));
    try {
      state.list = await api.reviews(state.filters);
      setReviewCounts(
        state.list.counts_by_priority,
        state.list.reviews.filter((row) => row.status === "pending").length
      );
      if (!state.list.reviews.some((row) => row.id === state.selectedId)) {
        state.selectedId = state.list.reviews.length ? state.list.reviews[0].id : null;
      }
      const queue = el("div", { class: "queue" });
      const detail = el("div", { class: "detail" });
      mount(ctx.content, el("div", { class: "split" }, [queue, detail]));
      renderQueue(queue, detail);
      await renderDetail(detail);
    } catch (error) {
      reportError(error);
      mount(ctx.content, empty(`Could not load the queue: ${error.message}`));
    }
  },
};

export async function selectReview(reviewId) {
  state.selectedId = reviewId;
  if (state.context) await queueView.render(state.context);
}

// -- filters ----------------------------------------------------------------

function filterBar(ctx) {
  const change = (key) => (event) => {
    state.filters[key] = event.target.value;
    queueView.render(ctx);
  };
  const choice = (label, key, options) =>
    el("span", { class: "chip" }, [
      el("span", { class: "chip__label", text: label }),
      el(
        "select",
        { onChange: change(key) },
        options.map(([value, text]) =>
          el("option", { value, text, selected: state.filters[key] === value })
        )
      ),
    ]);

  const sourceInput = el("input", {
    placeholder: "source contains…",
    value: state.filters.source,
    onChange: change("source"),
  });
  const existingInput = el("input", {
    placeholder: "existing object ID…",
    value: state.filters.existing_id,
    onChange: change("existing_id"),
  });

  return [
    choice("Status", "status", [
      ["pending", "pending"],
      ["resolved", "resolved"],
      ["rejected", "rejected"],
      ["all", "all"],
    ]),
    choice("Priority", "priority", [
      ["all", "all"],
      ["conflict", "conflict"],
      ["linked", "linked"],
      ["unlinked", "unlinked"],
    ]),
    choice("Reason", "reason", [
      ["", "any"],
      ["conflicting_evidence", "conflicting evidence"],
      ["ambiguous_match", "ambiguous match"],
    ]),
    choice("Category", "category", [["", "any"], ...CATEGORIES.map((v) => [v, v])]),
    el("span", { class: "chip" }, [
      el("span", { class: "chip__label", text: "Existing" }),
      existingInput,
    ]),
    el("span", { class: "chip" }, [
      el("span", { class: "chip__label", text: "Source" }),
      sourceInput,
    ]),
    el("button", { class: "chip", onClick: () => openBatchDialog(ctx) }, [
      icon("spark"),
      el("span", { text: "Generate suggestions for all pending" }),
    ]),
  ];
}

// -- queue list -------------------------------------------------------------

function renderQueue(queue, detail) {
  const groups = [
    ["conflict", "Conflict"],
    ["linked", "Linked"],
    ["unlinked", "Unlinked"],
  ];
  const nodes = [];
  for (const [priority, label] of groups) {
    const rows = state.list.reviews.filter((row) => row.priority === priority);
    if (!rows.length) continue;
    nodes.push(
      el("div", { class: "queue__group" }, [
        el("div", { class: "queue__group-label" }, [
          priorityCue(priority),
          el("span", { class: "nav-item__count", text: String(rows.length) }),
        ]),
        ...rows.map((row) => queueItem(row, detail)),
      ])
    );
  }
  mount(queue, nodes.length ? nodes : [empty("No review cases match these filters.")]);
}

function queueItem(row, detail) {
  return el(
    "button",
    {
      class: `queue__item${row.id === state.selectedId ? " is-active" : ""}`,
      dataset: { reviewId: row.id },
      onClick: async () => {
        state.selectedId = row.id;
        state.preview = null;
        renderQueue(document.querySelector(".queue"), detail);
        await renderDetail(detail);
      },
    },
    [
      el("span", { class: "queue__item-title", text: row.title }),
      el("span", { class: "queue__item-meta" }, [
        el("span", { text: row.category }),
        row.suggestion
          ? badge(row.suggestion.suggested_action || "human required")
          : badge("no suggestion"),
        row.resolution ? badge(row.resolution.action) : null,
      ].filter(Boolean)),
    ]
  );
}

// -- detail -----------------------------------------------------------------

async function renderDetail(detail) {
  if (!state.selectedId) {
    mount(detail, empty("Select a review case."));
    return;
  }
  mount(detail, busy("Loading review case…"));
  try {
    state.detail = await api.review(state.selectedId);
  } catch (error) {
    reportError(error);
    mount(detail, empty(`Could not load ${state.selectedId}: ${error.message}`));
    return;
  }
  const value = state.detail;
  state.form = value.status === "pending" ? newForm(value) : null;
  state.preview = null;

  mount(
    detail,
    el("div", {}, [
      reviewHeader(value),
      value.blocked.canonical_drift ? refreshPanel(value, detail) : null,
      comparePanel(value),
      el("h2", { class: "section", text: "Candidate" }),
      candidateProperties(value),
      el("h2", { class: "section", text: "AI suggestion" }),
      suggestionComment(value.suggestion),
      value.status === "pending" && !value.suggestion
        ? el("div", { class: "actionbar", style: "border:none;padding-top:0" }, [
            el("button", {
              class: "btn",
              onClick: () => generateOne(value.id, detail),
            }, [icon("spark"), el("span", { text: "Generate a suggestion" })]),
          ])
        : null,
      value.duplicate_previews.length ? duplicatePanel(value) : null,
      value.status === "pending" && !value.blocked.canonical_drift
        ? actionPanel(value)
        : null,
      resolutionPanel(value),
    ])
  );
}

// -- canonical drift --------------------------------------------------------

function refreshPanel(value, detail) {
  const drift = value.blocked.canonical_drift;
  const output = el("div", {});
  const applyButton = el("button", { class: "btn btn--primary", disabled: true }, [
    icon("refresh"),
    el("span", { text: "Apply refresh" }),
  ]);

  const previewButton = el("button", { class: "btn" }, [
    icon("doc"),
    el("span", { text: "Preview refresh" }),
  ]);
  previewButton.addEventListener("click", async () => {
    mount(output, busy("Running refresh dry run…"));
    try {
      const result = await api.refresh(value.id, {
        existing_id: drift.existing_id,
        dry_run: true,
      });
      mount(output, el("pre", { class: "preview", text: JSON.stringify(result.preview, null, 2) }));
      applyButton.disabled = false;
    } catch (error) {
      mount(output, el("div", { class: "callout callout--danger" }, [
        icon("alert"),
        el("div", { class: "callout__body", text: `${error.type}: ${error.message}` }),
      ]));
    }
  });

  applyButton.addEventListener("click", async () => {
    applyButton.disabled = true;
    mount(output, busy("Rebasing the review, then regenerating its suggestion…"));
    try {
      const applied = await api.refresh(value.id, {
        existing_id: drift.existing_id,
        dry_run: false,
      });
      let regenerated = true;
      try {
        await api.suggestOne(value.id, {});
      } catch (error) {
        regenerated = false;
        toast(
          `Refreshed, but the suggestion could not be regenerated: ${error.message}`,
          { kind: "danger", timeout: 0 }
        );
      }
      toast(
        `Refreshed ${applied.applied.review_id} against ${applied.applied.existing_id}` +
          (regenerated ? " and regenerated its suggestion." : "."),
        { kind: "good" }
      );
      await renderDetail(detail);
    } catch (error) {
      applyButton.disabled = false;
      reportError(error);
    }
  });

  return el("div", { class: "callout callout--warning" }, [
    icon("alert"),
    el("div", { class: "callout__body" }, [
      el("div", { class: "callout__title", text: "Canonical drift — resolution is refused" }),
      el("div", { text: drift.reason }),
      el("div", { class: "compare", style: "margin-top:8px" }, [
        el("div", { class: "compare__col" }, [
          el("div", { class: "compare__head", text: "Snapshot in this review" }),
          el("div", { class: "compare__text", text: drift.review_statement }),
        ]),
        el("div", { class: "compare__col" }, [
          el("div", { class: "compare__head", text: "Canonical object now" }),
          el("div", { class: "compare__text", text: drift.current_statement }),
        ]),
      ]),
      el("div", { class: "secondary", style: "margin-top:8px" }, [
        el("span", { text: "There is no override for this. Rebase the review onto " }),
        el("span", { class: "mono", text: drift.existing_id }),
        el("span", { text: ", then decide against the current text." }),
      ]),
      el("div", { class: "actionbar", style: "border:none" }, [previewButton, applyButton]),
      output,
    ]),
  ]);
}

// -- duplicates -------------------------------------------------------------

function duplicatePanel(value) {
  return el("div", {}, [
    el("h2", { class: "section", text: "Possible duplicate reviews" }),
    el(
      "div",
      { class: "rows" },
      value.duplicate_previews.map((duplicate) =>
        el("div", { class: "row" }, [
          priorityCue(duplicate.priority),
          el("span", { class: "row__title", text: duplicate.title }),
          el("span", { class: "row__id", text: duplicate.id }),
          el("button", {
            class: "row__open",
            style: "opacity:1",
            text: "Merge into this",
            onClick: () => {
              state.form.action = "merge-duplicate";
              state.form.duplicate_of = duplicate.id;
              state.form.touched = true;
              redrawActions();
            },
          }),
        ])
      )
    ),
    el(
      "div",
      { class: "secondary" },
      value.duplicate_previews.map((duplicate) =>
        el("div", { text: `${duplicate.id}: ${duplicate.candidate_statement}` })
      )
    ),
  ]);
}

// -- the action form --------------------------------------------------------

function newForm(value) {
  const metadata = value.candidate_metadata || {};
  const suggested = value.suggestion ? value.suggestion.recommendation.suggested_action : null;
  return {
    action: suggested || "",
    suggestedAction: suggested,
    suggestionId: value.suggestion ? value.suggestion.id : null,
    touched: false,
    existing_id: value.possible_existing_ids.length === 1 ? value.possible_existing_ids[0] : "",
    duplicate_of: "",
    new_id: "",
    note: value.suggestion ? value.suggestion.recommendation.proposed_note : "",
    noteFromSuggestion: Boolean(value.suggestion),
    noteEdited: false,
    noteAcknowledged: false,
    allow_stale_evidence: false,
    defaults: {
      title: metadata.title || "",
      status: metadata.status || "",
      owner: metadata.owner || "",
      confidence: metadata.confidence || "",
      effective_date: metadata.effective_date || "",
    },
    values: {
      title: metadata.title || "",
      status: metadata.status || "",
      owner: metadata.owner || "",
      confidence: metadata.confidence || "",
      effective_date: metadata.effective_date || "",
    },
  };
}

// Accepting means the AI's exact action with every field untouched. Anything
// else is an override, and the same suggestion_id still travels with it so the
// audit records suggested-versus-final.
function isAccept(form) {
  return Boolean(
    form.suggestionId &&
      form.suggestedAction &&
      !form.touched &&
      form.action === form.suggestedAction
  );
}

function resolveBody(form, dryRun) {
  if (isAccept(form)) {
    return {
      accept_suggestion: true,
      suggestion_id: form.suggestionId,
      note: form.note,
      dry_run: dryRun,
    };
  }
  const body = {
    action: form.action,
    suggestion_id: form.suggestionId,
    note: form.note,
    dry_run: dryRun,
  };
  if (["replace", "refine", "reconfirm", "keep-existing"].includes(form.action)) {
    if (form.existing_id) body.existing_id = form.existing_id;
  }
  if (form.action === "merge-duplicate") body.duplicate_of = form.duplicate_of;
  if (form.action === "create-separate" && form.new_id) body.new_id = form.new_id;
  if (METADATA_ACTIONS.has(form.action)) {
    for (const key of ["title", "status", "owner", "confidence", "effective_date"]) {
      const value = form.values[key];
      if (value === form.defaults[key]) continue;
      if (value) body[key] = value;
      else if (key === "owner") body.clear_owner = true;
      else if (key === "effective_date") body.clear_effective_date = true;
    }
  }
  if (STALE_ELIGIBLE.has(form.action) && form.allow_stale_evidence) {
    body.allow_stale_evidence = true;
  }
  return body;
}

function blockingReasons(value, form) {
  const reasons = [];
  if (!form.action) reasons.push("Choose an action.");
  if (!form.note.trim()) reasons.push("A resolution note is required.");
  if (form.noteFromSuggestion && !form.noteEdited && !form.noteAcknowledged) {
    reasons.push('Edit the note, or tick "I reviewed this note".');
  }
  if (
    ["replace", "refine", "reconfirm"].includes(form.action) &&
    !form.existing_id &&
    !isAccept(form)
  ) {
    reasons.push("Select the target object.");
  }
  if (form.action === "merge-duplicate" && !form.duplicate_of && !isAccept(form)) {
    reasons.push("Select the duplicate review to retain.");
  }
  if (isAccept(form) && value.blocked.accept_disabled) {
    reasons.push(value.blocked.accept_disabled_reason);
  }
  return reasons;
}

let actionsHost = null;

function redrawActions() {
  if (actionsHost && state.detail && state.form) {
    mount(actionsHost, actionForm(state.detail, state.form));
  }
}

// Typing in a text field must not rebuild the form -- that would steal focus --
// so the three things that depend on every keystroke are refreshed in place.
function refreshGates() {
  if (!state.detail || !state.form) return;
  const reasons = blockingReasons(state.detail, state.form);
  const host = document.getElementById("disposition-host");
  if (host) mount(host, dispositionNode(state.form));
  const button = document.getElementById("preview-button");
  if (button) button.disabled = reasons.length > 0;
  const list = document.getElementById("blocking-reasons");
  if (list) {
    mount(list, reasons.map((reason) => el("li", { text: reason })));
    list.hidden = reasons.length === 0;
  }
}

function actionPanel(value) {
  actionsHost = el("div", {});
  mount(actionsHost, actionForm(value, state.form));
  return el("div", {}, [el("h2", { class: "section", text: "Decision" }), actionsHost]);
}

function actionForm(value, form) {
  const touch = () => {
    form.touched = true;
  };

  const radios = el(
    "div",
    { class: "radio-grid" },
    ACTIONS.map((action) => {
      const disabled =
        action === "merge-duplicate" && !value.possible_duplicate_ids.length;
      return el("label", { class: `radio${disabled ? " is-disabled" : ""}` }, [
        el("input", {
          type: "radio",
          name: "action",
          value: action,
          checked: form.action === action,
          disabled,
          onChange: () => {
            if (action !== form.suggestedAction || form.action !== action) touch();
            form.action = action;
            redrawActions();
          },
        }),
        el("span", { text: action }),
        action === form.suggestedAction ? badge("AI") : null,
        action === "merge-duplicate" && value.possible_duplicate_ids.length
          ? badge(`${value.possible_duplicate_ids.length} possible`)
          : null,
      ].filter(Boolean));
    })
  );

  const props = [];

  if (["replace", "refine", "reconfirm", "keep-existing"].includes(form.action)) {
    const options = value.target_options;
    const needsChoice = options.length > 1;
    props.push(
      ...property(
        "Target object",
        el("div", {}, [
          el(
            "select",
            {
              onChange: (event) => {
                form.existing_id = event.target.value;
                touch();
                redrawActions();
              },
            },
            [
              needsChoice || !options.length
                ? el("option", { value: "", text: "— choose —", selected: !form.existing_id })
                : null,
              ...options.map((option) =>
                el("option", {
                  value: option.id,
                  selected: form.existing_id === option.id,
                  text: `${option.title || option.id} — ${option.id}`,
                })
              ),
            ].filter(Boolean)
          ),
          needsChoice && !form.existing_id
            ? el("div", {
                class: "secondary",
                text: "This review names several possible objects; preview is blocked until one is chosen.",
              })
            : null,
        ])
      )
    );
  }

  if (form.action === "merge-duplicate") {
    props.push(
      ...property(
        "Duplicate to retain",
        el(
          "select",
          {
            onChange: (event) => {
              form.duplicate_of = event.target.value;
              touch();
              redrawActions();
            },
          },
          [
            el("option", { value: "", text: "— choose —", selected: !form.duplicate_of }),
            ...value.possible_duplicate_ids.map((id) =>
              el("option", { value: id, text: id, selected: form.duplicate_of === id })
            ),
          ]
        )
      )
    );
  }

  if (form.action === "create-separate") {
    props.push(
      ...property(
        "New object ID",
        el("input", {
          placeholder: "blank for the deterministic default",
          value: form.new_id,
          onInput: (event) => {
            form.new_id = event.target.value;
            touch();
            refreshGates();
          },
        })
      )
    );
  }

  if (METADATA_ACTIONS.has(form.action)) {
    const field = (label, key, node) => property(label, node);
    props.push(
      ...field(
        "Title",
        "title",
        el("input", {
          value: form.values.title,
          onInput: (event) => {
            form.values.title = event.target.value;
            touch();
            refreshGates();
          },
        })
      ),
      ...field(
        "Status",
        "status",
        el(
          "select",
          {
            onChange: (event) => {
              form.values.status = event.target.value;
              touch();
              refreshGates();
            },
          },
          [["", "— unchanged —"], ...STATUSES.map((v) => [v, v])].map(([v, t]) =>
            el("option", { value: v, text: t, selected: form.values.status === v })
          )
        )
      ),
      ...field(
        "Owner",
        "owner",
        el("input", {
          value: form.values.owner,
          placeholder: "blank clears the owner",
          onInput: (event) => {
            form.values.owner = event.target.value;
            touch();
            refreshGates();
          },
        })
      ),
      ...field(
        "Confidence",
        "confidence",
        el(
          "select",
          {
            onChange: (event) => {
              form.values.confidence = event.target.value;
              touch();
              refreshGates();
            },
          },
          [["", "— unchanged —"], ...CONFIDENCES.map((v) => [v, v])].map(([v, t]) =>
            el("option", { value: v, text: t, selected: form.values.confidence === v })
          )
        )
      ),
      ...field(
        "Effective date",
        "effective_date",
        el("input", {
          type: "date",
          value: form.values.effective_date,
          onChange: (event) => {
            form.values.effective_date = event.target.value;
            touch();
            refreshGates();
          },
        })
      )
    );
  }

  const noteInput = el("textarea", {
    value: form.note,
    placeholder: "What you decided and why…",
    onInput: (event) => {
      form.note = event.target.value;
      form.noteEdited = true;
      touch();
      refreshGates();
    },
  });
  props.push(
    ...property(
      "Note",
      el("div", {}, [
        noteInput,
        form.noteFromSuggestion && !form.noteEdited
          ? el("label", { class: "checkline" }, [
              el("input", {
                type: "checkbox",
                checked: form.noteAcknowledged,
                onChange: (event) => {
                  form.noteAcknowledged = event.target.checked;
                  refreshGates();
                },
              }),
              el("span", {
                text:
                  "I reviewed this note (it is the AI's proposed_note, unedited). " +
                  "Ticking this does not change accept versus override.",
              }),
            ])
          : null,
      ])
    )
  );

  const staleBox =
    STALE_ELIGIBLE.has(form.action) && value.blocked.candidate_evidence_drifted
      ? el("label", { class: "checkline" }, [
          el("input", {
            type: "checkbox",
            checked: form.allow_stale_evidence,
            onChange: (event) => {
              form.allow_stale_evidence = event.target.checked;
              touch();
              redrawActions();
            },
          }),
          el("span", {
            text:
              "Allow stale evidence — promote candidate evidence whose source drifted. " +
              "This override is permanently recorded on the resolution.",
          }),
        ])
      : null;

  const reasons = blockingReasons(value, form);
  const previewButton = el(
    "button",
    { class: "btn", id: "preview-button", disabled: reasons.length > 0 },
    [icon("doc"), el("span", { text: "Preview decision" })]
  );
  previewButton.addEventListener("click", () => runPreview(value, form));

  const deferButton = el("button", { class: "btn" }, [
    icon("chevronRight"),
    el("span", { text: "Defer" }),
  ]);
  deferButton.addEventListener("click", () => deferCurrent());

  const badgeHost = el("span", { id: "disposition-host" }, dispositionNode(form));
  const reasonList = el(
    "ul",
    { class: "list-tight muted", id: "blocking-reasons" },
    reasons.map((reason) => el("li", { text: reason }))
  );
  reasonList.hidden = reasons.length === 0;

  const previewHost = el("div", {});
  if (state.preview) mount(previewHost, previewBlock(value, form, state.preview));

  return el("div", {}, [
    value.blocked.accept_disabled
      ? el("div", { class: "callout callout--warning" }, [
          icon("alert"),
          el("div", { class: "callout__body" }, [
            el("div", { class: "callout__title", text: "Accept is unavailable" }),
            el("div", { text: value.blocked.accept_disabled_reason }),
            el("div", {
              text: "Choose an action yourself, or defer this case.",
              class: "secondary",
            }),
          ]),
        ])
      : null,
    value.blocked.candidate_evidence_unreadable
      ? el("div", { class: "callout callout--danger" }, [
          icon("alert"),
          el("div", { class: "callout__body" }, [
            el("div", { class: "callout__title", text: "Candidate evidence is unreadable" }),
            el("div", { text: "The excerpt could not be read from its source file." }),
          ]),
        ])
      : null,
    radios,
    el("div", { class: "props" }, props),
    staleBox,
    el("div", { class: "actionbar" }, [
      el("span", { class: "secondary", text: "Will record:" }),
      badgeHost,
      el("span", { class: "topbar__spacer" }),
      deferButton,
      previewButton,
    ]),
    reasonList,
    previewHost,
  ]);
}

function dispositionNode(form) {
  if (!form.suggestionId) {
    return [badge("human · no suggestion used")];
  }
  return isAccept(form)
    ? [badge("accepted · hybrid", "accepted")]
    : [badge("overridden · hybrid", "overridden")];
}

// -- preview and apply ------------------------------------------------------

async function runPreview(value, form) {
  state.preview = { loading: true };
  redrawActions();
  try {
    const result = await api.resolve(value.id, resolveBody(form, true));
    state.preview = result;
  } catch (error) {
    state.preview = { error };
    reportError(error);
  }
  redrawActions();
}

function previewBlock(value, form, preview) {
  if (preview.loading) return busy("Running the deterministic dry run…");
  if (preview.error) {
    return el("div", { class: "callout callout--danger" }, [
      icon("alert"),
      el("div", { class: "callout__body" }, [
        el("div", { class: "callout__title", text: "The resolver refused this decision" }),
        el("div", { text: `${preview.error.type}: ${preview.error.message}` }),
      ]),
    ]);
  }
  const result = preview.preview;
  const applyButton = el("button", { class: "btn btn--primary" }, [
    icon("check"),
    el("span", { text: `Apply — ${result.action} → ${result.destination_status}` }),
  ]);
  applyButton.addEventListener("click", () => applyDecision(value, form, applyButton));

  const changes = (result.object_changes || []).map((change) =>
    el("div", { class: "compare" }, [
      el("div", { class: "compare__col" }, [
        el("div", { class: "compare__head", text: "Before" }),
        change.before
          ? el("div", { class: "compare__text", text: change.before.statement })
          : el("div", { class: "muted", text: "New object" }),
        change.before
          ? el("div", { class: "secondary", text: `status ${change.before.status}` })
          : null,
      ]),
      el("div", { class: "compare__col" }, [
        el("div", { class: "compare__head", text: `After — ${change.after.id}` }),
        el("div", { class: "compare__text", text: change.after.statement }),
        el("div", { class: "secondary", text: `status ${change.after.status}` }),
      ]),
    ])
  );

  return el("div", {}, [
    el("h2", { class: "section", text: "Deterministic dry run" }),
    el("div", { class: "props" }, [
      ...property("Destination status", result.destination_status),
      ...property("Action", result.action),
      ...property(
        "Affected objects",
        el("span", {
          class: "mono",
          text: result.affected_object_ids.join(", ") || "none",
        })
      ),
      ...property(
        "Will record",
        badge(
          `${preview.will_record.disposition} · ${preview.will_record.mode}`,
          preview.will_record.disposition === "accepted" ? "accepted" : "overridden"
        )
      ),
      ...property(
        "Changed paths",
        el(
          "div",
          { class: "mono secondary" },
          result.changed_paths.map((path) => el("div", { text: path }))
        )
      ),
    ]),
    ...changes,
    el("div", { class: "actionbar" }, [
      el("span", { class: "secondary", text: "Apply re-sends these exact arguments; the resolver re-validates them under the lock." }),
      el("span", { class: "topbar__spacer" }),
      applyButton,
    ]),
  ]);
}

async function applyDecision(value, form, button) {
  button.disabled = true;
  try {
    const result = await api.resolve(value.id, resolveBody(form, false));
    const applied = result.applied;
    toast(
      `Applied ${applied.review_id} as ${applied.action} → ${applied.destination_status}, ` +
        `recorded ${result.will_record.disposition} (${result.will_record.mode}).`,
      {
        kind: "good",
        link: result.resolution
          ? {
              label: `Open audit record: ${result.resolution.file_path}`,
              action: () => window.open(`#${result.resolution.file_path}`, "_self"),
            }
          : null,
      }
    );
    advanceToNext();
    if (state.context) await queueView.render(state.context);
  } catch (error) {
    button.disabled = false;
    reportError(error);
  }
}

function advanceToNext() {
  if (!state.list) return;
  const ids = state.list.reviews.map((row) => row.id);
  const index = ids.indexOf(state.selectedId);
  state.selectedId = ids[index + 1] || ids[index - 1] || null;
  state.preview = null;
}

function deferCurrent() {
  const id = state.selectedId;
  advanceToNext();
  toast(`Deferred ${id}; no files changed.`);
  if (state.context) queueView.render(state.context);
}

// -- suggestion generation --------------------------------------------------

async function generateOne(reviewId, detail) {
  const node = toast("Generating a suggestion…", { timeout: 0 });
  try {
    await api.suggestOne(reviewId, {});
    node.remove();
    toast("Suggestion generated.", { kind: "good" });
    await renderDetail(detail);
  } catch (error) {
    node.remove();
    reportError(error);
  }
}

function openBatchDialog(ctx) {
  const progress = el("div", { class: "progress-list" });
  const summary = el("div", { class: "secondary" });
  const force = el("input", { type: "checkbox" });
  const startButton = el("button", { class: "btn btn--primary" }, [
    icon("spark"),
    el("span", { text: "Generate" }),
  ]);

  startButton.addEventListener("click", async () => {
    startButton.disabled = true;
    mount(progress);
    mount(summary, busy("Streaming per-item progress…"));
    try {
      await streamSuggestions(
        {
          priority: state.filters.priority,
          reason: state.filters.reason || null,
          category: state.filters.category || null,
          existing_id: state.filters.existing_id || null,
          source: state.filters.source || null,
          force: force.checked,
        },
        (event) => {
          if (event.event === "batch") {
            mount(summary, el("div", { text: `${event.total} pending reviews · ${event.model}` }));
            return;
          }
          if (event.event === "start") {
            progress.appendChild(
              el("div", { dataset: { reviewId: event.review_id } }, [
                el("span", { class: "spinner" }),
                el("span", { class: "mono", text: event.review_id }),
              ])
            );
            progress.scrollTop = progress.scrollHeight;
            return;
          }
          if (event.event === "item") {
            const row = progress.querySelector(
              `[data-review-id="${CSS.escape(event.review_id)}"]`
            );
            const cue =
              event.outcome === "failed"
                ? statusCue("critical", "failed")
                : event.outcome === "reused"
                ? statusCue("muted", "reused")
                : statusCue("good", "created");
            const text = event.error
              ? event.error
              : `${event.suggested_action || "human required"}`;
            const node = el("div", {}, [
              cue,
              el("span", { class: "mono", text: event.review_id }),
              el("span", { class: "secondary", text }),
            ]);
            if (row) row.replaceWith(node);
            else progress.appendChild(node);
            return;
          }
          if (event.event === "complete") {
            const manifest = event.manifest;
            mount(
              summary,
              el("div", {}, [
                el("div", {
                  text:
                    `${Object.keys(manifest.suggestions_created).length} created, ` +
                    `${Object.keys(manifest.suggestions_reused).length} reused, ` +
                    `${manifest.failures.length} failed.`,
                }),
                el("div", { class: "mono secondary", text: event.manifest_path }),
              ])
            );
            return;
          }
          if (event.event === "error") {
            mount(summary, el("div", { class: "callout callout--danger" }, [
              icon("alert"),
              el("div", { class: "callout__body", text: event.error }),
            ]));
          }
        }
      );
    } catch (error) {
      reportError(error);
      mount(summary, el("div", { class: "callout callout--danger" }, [
        icon("alert"),
        el("div", { class: "callout__body", text: `${error.type}: ${error.message}` }),
      ]));
    }
    startButton.disabled = false;
  });

  openModal({
    title: "Generate suggestions for all pending",
    body: el("div", {}, [
      el("div", {
        class: "secondary",
        text:
          "Runs the same generator the CLI does over the currently filtered pending " +
          "set. Failures are isolated per review and recorded in the review-run " +
          "manifest; nothing is resolved here.",
      }),
      el("label", { class: "checkline" }, [
        force,
        el("span", {
          text: "Force a new artifact even when the exact inputs already match one",
        }),
      ]),
      summary,
      progress,
    ]),
    actions: [
      el("button", {
        class: "btn",
        text: "Close",
        onClick: () => {
          closeModal();
          queueView.render(ctx);
        },
      }),
      startButton,
    ],
  });
}

// -- keyboard ---------------------------------------------------------------

export function handleQueueKey(event) {
  if (!state.list || !state.list.reviews.length) return false;
  const ids = state.list.reviews.map((row) => row.id);
  const index = ids.indexOf(state.selectedId);
  const detail = document.querySelector(".detail");
  const move = async (next) => {
    state.selectedId = ids[next];
    state.preview = null;
    renderQueue(document.querySelector(".queue"), detail);
    await renderDetail(detail);
  };
  if (event.key === "j" && index < ids.length - 1) {
    move(index + 1);
    return true;
  }
  if (event.key === "k" && index > 0) {
    move(index - 1);
    return true;
  }
  if (event.key === "d") {
    deferCurrent();
    return true;
  }
  if (event.key === "a" && state.form && state.detail) {
    if (state.form.suggestedAction) {
      state.form.action = state.form.suggestedAction;
      state.form.touched = false;
      redrawActions();
      runPreview(state.detail, state.form);
    }
    return true;
  }
  if (event.key === "o") {
    const first = document.querySelector('.radio input[type="radio"]:not(:disabled)');
    if (first) first.focus();
    return true;
  }
  if (event.key === "Enter" && state.preview && state.preview.preview) {
    const applyButton = Array.from(document.querySelectorAll(".btn--primary")).find(
      (button) => button.textContent.startsWith("Apply")
    );
    if (applyButton) applyButton.click();
    return true;
  }
  return false;
}
