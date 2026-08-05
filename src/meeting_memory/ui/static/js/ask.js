// Tab 3 — Ask.
//
// The backend already guarantees traceability: a citation that is not in the
// context packet is rejected before the answer is returned. So every chip here
// resolves to a real object and a real file, and a chip that does not is
// rendered as the retrieval bug it would be rather than as a quiet dash.
//
// Citations are document-level, not per-claim. Nothing in this file inserts
// footnote markers into the answer text: the model does not say which sentence
// rests on which source, and guessing would be invented precision. Citations
// live in a "grounded in" strip under the answer, and the evidence they name is
// expanded in the rail beside it.

import { api } from "./api.js";
import { badge, callout, el, icon, mount, property, statusCue } from "./dom.js";
import { evidenceBlock } from "./evidence.js";
import { openObjectPeek } from "./objects.js";
import { openScopeEditor } from "./projects.js";
import { openReviewPeek } from "./reviewpeek.js";
import { store } from "./store.js";
import { busy, empty, reportError, toast } from "./ui.js";

const state = {
  query: "",
  // The saved project scope this tab is asking under, or null for all durable
  // knowledge. A scope only ever narrows: nothing here falls back to unscoped
  // retrieval when it comes back empty.
  project: null,
  projects: [],
  projectsLoaded: false,
  options: {
    limit: 8,
    max_chars: 30000,
    max_evidence_per_object: 3,
    include_review_items: true,
    include_manual_notes: false,
    include_evidence_excerpts: true,
  },
  context: null,
  result: null,
  // The digest retrieval first returned, kept so it can be compared with the
  // one the answer was actually produced from.
  retrievedSha: null,
  phase: "idle", // idle | retrieving | answering | answered | retrieved
  error: null,
};

export const askView = {
  id: "ask",
  label: "Ask",
  icon: "spark",
  title: "Ask",

  count() {
    return state.context ? state.context.retrieval.objects_selected : null;
  },

  async render(ctx) {
    if (!state.projectsLoaded) await loadProjects();
    mount(ctx.filters, filterBar(ctx));
    mount(ctx.content, page(ctx));
  },
};

async function loadProjects() {
  try {
    state.projects = (await api.projects()).projects;
    state.projectsLoaded = true;
  } catch (error) {
    reportError(error);
  }
}

function currentProject() {
  return state.projects.find((value) => value.name === state.project) || null;
}

// A result carries the scope it ran under. Changing the scope therefore
// discards it: a receipt from one project above an answer from another is the
// exact confusion the receipt exists to prevent.
function setProject(name, ctx) {
  state.project = name;
  state.context = null;
  state.result = null;
  state.retrievedSha = null;
  state.phase = "idle";
  rerender(ctx);
}

function rerender(ctx) {
  askView.render(ctx);
}

// -- retrieval options ------------------------------------------------------

function numberChip(label, key, ctx, { min = 0, title = null } = {}) {
  return el("span", { class: "chip", title }, [
    el("span", { class: "chip__label", text: label }),
    el("input", {
      type: "number",
      min: String(min),
      value: String(state.options[key]),
      style: "width:76px",
      onChange: (event) => {
        const value = Number(event.target.value);
        if (Number.isFinite(value) && value >= min) state.options[key] = value;
        rerender(ctx);
      },
    }),
  ]);
}

function toggleChip(label, key, ctx, title) {
  return el(
    "button",
    {
      class: "chip",
      title,
      onClick: () => {
        state.options[key] = !state.options[key];
        rerender(ctx);
      },
    },
    [
      icon(state.options[key] ? "check" : "dot"),
      el("span", { text: label }),
    ]
  );
}

// The scope control: a picker over saved projects, plus the two ways to change
// one. "New" and "Edit" are buttons rather than options inside the picker --
// a sentinel option value would collide with a project that happened to be
// named the same thing.
function scopeChip(ctx) {
  const editing = (project) => ({
    project,
    onSaved: async (saved) => {
      await loadProjects();
      setProject(saved.name, ctx);
    },
  });
  const select = el(
    "select",
    {
      title:
        "Bound retrieval to one project's meetings and Slack channels. A "
        + "scope only narrows; it never falls back to searching everything.",
      onChange: (event) => setProject(event.target.value || null, ctx),
    },
    [
      el("option", { value: "", text: "All knowledge", selected: !state.project }),
      ...state.projects.map((value) =>
        el("option", {
          value: value.name,
          text: `${value.name} · ${value.objects} objects`,
          selected: value.name === state.project,
        })
      ),
    ]
  );
  const project = currentProject();
  return el("span", { class: "chip" }, [
    el("span", { class: "chip__label", text: "Scope" }),
    select,
    project
      ? el("button", {
          class: "chip__action",
          title: `Edit the sources in ${project.name}`,
          onClick: () => openScopeEditor(editing(project)),
        }, [icon("panel")])
      : null,
    el("button", {
      class: "chip__action",
      title: "Build a new project scope from the sources the index cites",
      onClick: () => openScopeEditor(editing(null)),
    }, [icon("plus")]),
  ]);
}

function filterBar(ctx) {
  return [
    scopeChip(ctx),
    numberChip("Objects", "limit", ctx, {
      min: 1,
      title: "Maximum knowledge objects selected into the context packet.",
    }),
    numberChip("Max chars", "max_chars", ctx, {
      min: 1,
      title:
        "Character budget. When the packet exceeds it, the builder drops "
        + "material in a fixed order and records every drop as an omission.",
    }),
    numberChip("Evidence each", "max_evidence_per_object", ctx, {
      title: "Evidence references carried per object.",
    }),
    toggleChip(
      "Pending review items",
      "include_review_items",
      ctx,
      "Include connected pending review items as clearly proposed material."
    ),
    toggleChip(
      "Manual notes",
      "include_manual_notes",
      ctx,
      "Include human-authored manual notes in the context."
    ),
    toggleChip(
      "Send excerpts to model",
      "include_evidence_excerpts",
      ctx,
      "The source lines behind each evidence reference are listed below either "
        + "way. This controls whether they are also written into the packet the "
        + "model reads. Off, the model sees only the statements, so detail the "
        + "statement paraphrased away — links, identifiers, exact numbers — "
        + "reaches you but not the model."
    ),
  ];
}

// -- asking -----------------------------------------------------------------

function requestBody() {
  return { query: state.query, project: state.project, ...state.options };
}

async function retrieveOnly(ctx) {
  if (!state.query.trim()) return;
  state.phase = "retrieving";
  state.error = null;
  state.result = null;
  rerender(ctx);
  try {
    state.context = await api.askContext(requestBody());
    state.retrievedSha = state.context.packet_sha256;
    state.phase = "retrieved";
  } catch (error) {
    state.error = error;
    state.phase = "idle";
    reportError(error);
  }
  rerender(ctx);
}

async function ask(ctx) {
  if (!state.query.trim()) return;
  state.phase = "retrieving";
  state.error = null;
  state.result = null;
  rerender(ctx);
  try {
    // Retrieval is deterministic and fast; the provider call is neither. The
    // evidence is rendered as soon as it exists so a slow or failing model
    // still leaves the reader with the objects, excerpts, and omissions.
    state.context = await api.askContext(requestBody());
    state.retrievedSha = state.context.packet_sha256;
    state.phase = "answering";
    rerender(ctx);
    state.result = await api.askAnswer(requestBody());
    // Show the context the answer was produced from, not the earlier read.
    state.context = state.result.context;
    state.phase = "answered";
  } catch (error) {
    state.error = error;
    state.phase = state.context ? "retrieved" : "idle";
    reportError(error);
  }
  rerender(ctx);
}

// -- page -------------------------------------------------------------------

function page(ctx) {
  const session = store.session || {};
  const box = el("textarea", {
    class: "ask__input",
    placeholder: "Ask a question of the durable knowledge…",
    value: state.query,
    onInput: (event) => {
      state.query = event.target.value;
    },
    onKeydown: (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        ask(ctx);
      }
    },
  });

  const running = state.phase === "retrieving" || state.phase === "answering";
  const askButton = el(
    "button",
    {
      class: "btn btn--primary",
      disabled: running || !session.ask_model_configured,
      onClick: () => ask(ctx),
      title: session.ask_model_configured
        ? "Retrieve, then answer (⌘⏎)"
        : "No ask model is configured",
    },
    [icon("spark"), el("span", { text: "Ask" })]
  );

  return el("div", {}, [
    el("div", { class: "ask__form" }, [
      box,
      el("div", { class: "ask__form-foot" }, [
        el("span", {
          class: "muted",
          text: session.ask_model_configured
            ? `${session.ask_model} · ⌘⏎ to ask`
            : "",
        }),
        el(
          "button",
          {
            class: "btn",
            disabled: running,
            onClick: () => retrieveOnly(ctx),
            title:
              "Deterministic retrieval only. No provider call, so this works "
              + "with no model configured.",
          },
          [icon("search"), el("span", { text: "Retrieve only" })]
        ),
        askButton,
      ]),
    ]),
    session.ask_model_configured
      ? null
      : callout(
          "warning",
          "No ask model is configured",
          "Start the UI with --ask-model, or set DAILY_KNOWLEDGE_ASK_MODEL, "
            + "OPENROUTER_MODEL, or ask_model. Retrieval still works without one.",
        ),
    state.phase === "retrieving" ? busy("Retrieving durable knowledge…") : null,
    state.context ? scopeReceipt(ctx) : null,
    state.context ? retrievalReceipt() : null,
    state.context ? body(ctx) : state.phase === "idle" && !state.error
      ? empty("Answers are grounded in the durable knowledge this repository holds.")
      : null,
  ]);
}

// Without this line a scoped answer and an unscoped one look identical, and a
// narrow-scope answer read as a global one is worse than no answer.
function scopeReceipt(ctx) {
  const scope = state.context.scope;
  if (!scope) return null;
  return el("div", { class: "ask__scope" }, [
    icon("layers"),
    el("span", { class: "ask__scope-name", text: scope.name }),
    el("span", {
      text: `${scope.objects} of ${scope.objects_total} objects · `
        + `${scope.sources_matched} of ${scope.sources_total} sources`,
    }),
    el("span", {
      class: "muted",
      // The options this context was retrieved with, not the ones the filter
      // bar currently shows — those may have moved since.
      title: state.context.options.include_review_items
        ? "Pending review items are filtered by the same sources, so a scoped "
          + "packet carries only in-scope proposals."
        : "Pending review items are switched off for this question entirely.",
      text: state.context.options.include_review_items
        ? "review items scoped"
        : "review items off",
    }),
    scope.straddling.length
      ? el("span", {
          class: "status status--warning",
          title:
            "These objects also cite evidence outside the scope. Only in-scope "
            + "evidence was retrieved, but each statement was synthesized from "
            + "all of it — a scope bounds retrieval, not provenance.",
          text:
            scope.straddling.length === 1
              ? "1 object cites evidence outside the scope"
              : `${scope.straddling.length} objects cite evidence outside the scope`,
        })
      : null,
    el("button", {
      class: "row__open",
      style: "opacity:1",
      text: "Edit",
      onClick: () =>
        openScopeEditor({
          project: currentProject(),
          onSaved: async (saved) => {
            await loadProjects();
            setProject(saved.name, ctx);
          },
        }),
    }),
  ]);
}

function retrievalReceipt() {
  const retrieval = state.context.retrieval;
  const budget = retrieval.max_chars
    ? `${retrieval.chars.toLocaleString()} of ${retrieval.max_chars.toLocaleString()} chars`
    : `${retrieval.chars.toLocaleString()} chars`;
  return el("div", { class: "ask__receipt secondary" }, [
    el("span", { text: `${retrieval.objects_selected} objects` }),
    el("span", { text: `${retrieval.categories} categories` }),
    el("span", { text: `${retrieval.reviews_selected} pending review items` }),
    el("span", { text: budget }),
    retrieval.omissions
      ? statusCue("warning", `${retrieval.omissions} omissions`)
      : statusCue("good", "nothing omitted"),
  ]);
}

function body(ctx) {
  if (!state.context.objects.length) {
    return el("div", {}, [
      state.context.scope
        ? emptyScopeCallout(ctx)
        : callout(
            "neutral",
            "No durable knowledge covers this question",
            "Deterministic retrieval selected no objects, so no provider was "
              + "called. Try different wording, or widen the object limit.",
            "search"
          ),
      omissionsBlock(),
      contextBlock(),
    ]);
  }
  return el("div", { class: "ask__split" }, [
    el("div", { class: "ask__main" }, [
      state.phase === "answering"
        ? busy("Answering from the retrieved context…")
        : null,
      state.result ? answerPanel(ctx) : null,
      state.result ? null : el("div", { class: "secondary", style: "margin-top:10px" }, [
        el("span", { text: "Retrieved context only — no answer requested." }),
      ]),
      consideredBlock(ctx),
      omissionsBlock(),
      contextBlock(),
    ]),
    el("div", { class: "ask__rail" }, [
      el("div", { class: "compare__head", text: "Evidence" }),
      ...railCards(),
    ]),
  ]);
}

// An empty scope is an error state, not a reason to search everything. Leaving
// it is an explicit act by the reader, and it says so.
function emptyScopeCallout(ctx) {
  const scope = state.context.scope;
  const scopeIsEmpty = scope.objects === 0;
  return callout(
    scopeIsEmpty ? "warning" : "neutral",
    scopeIsEmpty
      ? `No knowledge objects are in scope for “${scope.name}”`
      : `Nothing in “${scope.name}” answers this question`,
    el("div", {}, [
      el("div", {
        text: scopeIsEmpty
          ? `This project matches ${scope.sources_matched} of `
            + `${scope.sources_total} sources and admits none of the `
            + `${scope.objects_total} knowledge objects. No provider was called.`
          : `The project admits ${scope.objects} of ${scope.objects_total} `
            + "knowledge objects, and deterministic retrieval selected none of "
            + "them for this question. No provider was called.",
      }),
      el("div", { class: "props props--tight", style: "margin-top:8px" }, [
        ...property("Meetings", scope.meeting_names.join(", ") || null),
        ...property("Slack", scope.slack_names.join(", ") || null),
      ]),
      el("div", { class: "actionbar" }, [
        el(
          "button",
          {
            class: "btn",
            onClick: () =>
              openScopeEditor({
                project: currentProject(),
                onSaved: async (saved) => {
                  await loadProjects();
                  setProject(saved.name, ctx);
                },
              }),
          },
          [icon("panel"), el("span", { text: "Edit this scope" })]
        ),
        el(
          "button",
          {
            class: "btn",
            title:
              "Leave the scope deliberately. Nothing here widens a scope on "
              + "your behalf.",
            onClick: () => setProject(null, ctx),
          },
          [icon("search"), el("span", { text: "Ask across all knowledge" })]
        ),
      ]),
    ]),
    "layers"
  );
}

// -- answer -----------------------------------------------------------------

const CONFIDENCE_ROLE = { high: "good", medium: "warning", low: "critical" };

function answerPanel(ctx) {
  const answer = state.result.answer;
  const drifted =
    state.retrievedSha !== null
    && state.retrievedSha !== state.result.context.packet_sha256;

  return el("div", {}, [
    el("div", { class: "ask__answer-head" }, [
      statusCue(
        CONFIDENCE_ROLE[answer.confidence] || "muted",
        `${answer.confidence} confidence`
      ),
      answer.model
        ? el("span", { class: "muted mono", text: answer.model })
        : badge("no model — insufficient context"),
    ]),
    drifted
      ? callout(
          "warning",
          "The repository changed while this question ran",
          "The context shown below is the one the answer was produced from, "
            + "not the one retrieval first returned."
        )
      : null,
    el("div", { class: "ask__answer", text: answer.answer }),
    answer.open_conflicts.length
      ? callout(
          "warning",
          `${answer.open_conflicts.length} open conflict${
            answer.open_conflicts.length === 1 ? "" : "s"
          }`,
          el(
            "ul",
            { class: "list-tight" },
            answer.open_conflicts.map((value) => el("li", { text: value }))
          )
        )
      : null,
    groundedStrip(),
    state.context.reviews.length ? reviewsCallout() : null,
    el("div", { class: "actionbar" }, [
      el(
        "button",
        {
          class: "btn",
          onClick: () => saveAnswer(ctx),
          title:
            "Write this exact answer, with its retrieval receipt, under "
            + "outputs/Knowledge-Answers/.",
        },
        [icon("doc"), el("span", { text: "Save answer" })]
      ),
    ]),
  ]);
}

function groundedStrip() {
  const objects = state.result.cited_objects;
  const evidence = state.result.cited_evidence;
  if (!objects.length && !evidence.length) {
    return el("div", { class: "secondary", style: "margin-top:10px" }, [
      el("span", { text: "The answer cites no knowledge object or source." }),
    ]);
  }
  return el("div", {}, [
    el("div", { class: "compare__head", style: "margin-top:16px", text: "Grounded in" }),
    el("div", { class: "chips" }, [
      ...objects.map((value) =>
        citationChip({
          label: value.title || value.id,
          hint: value.id,
          iconName: "doc",
          present: value.present,
          target: value.id,
        })
      ),
      ...evidence.map((value) =>
        citationChip({
          label: value.source,
          hint: value.object_ids.join(", "),
          iconName: "layers",
          present: value.present,
          mono: true,
          target: value.object_ids[0],
        })
      ),
    ]),
  ]);
}

function citationChip({ label, hint, iconName, present, mono, target }) {
  if (!present) {
    // The answer validator makes this unreachable for a validated answer, so
    // it is a retrieval failure and is shown as one rather than hidden.
    return el("span", { class: "cite cite--broken", title: "not present in the context packet" }, [
      icon("alert"),
      el("span", { class: mono ? "mono" : "", text: label }),
      el("span", { class: "muted", text: "not in context" }),
    ]);
  }
  return el(
    "button",
    {
      class: "cite",
      title: hint,
      onClick: () => focusCard(target),
      onMouseenter: () => highlightCard(target, true),
      onMouseleave: () => highlightCard(target, false),
    },
    [icon(iconName), el("span", { class: mono ? "mono" : "", text: label })]
  );
}

async function saveAnswer(ctx) {
  try {
    const saved = await api.askSave(state.result.answer_token);
    toast(`Answer saved to ${saved.saved_path}`, { kind: "good" });
  } catch (error) {
    reportError(error);
  }
}

// -- evidence rail ----------------------------------------------------------

function cardId(objectId) {
  return `ask-card-${objectId}`;
}

function focusCard(objectId) {
  const node = objectId && document.getElementById(cardId(objectId));
  if (!node) return;
  node.scrollIntoView({ behavior: "smooth", block: "center" });
  highlightCard(objectId, true);
  window.setTimeout(() => highlightCard(objectId, false), 1200);
}

function highlightCard(objectId, on) {
  const node = objectId && document.getElementById(cardId(objectId));
  if (node) node.classList.toggle("is-linked", Boolean(on));
}

function railCards() {
  const cited = state.result
    ? state.context.objects.filter((value) => value.cited)
    : state.context.objects;
  if (!cited.length) {
    return [
      el("div", { class: "secondary muted", style: "padding:8px 0" }, [
        el("span", {
          text: state.result
            ? "The answer cited none of the retrieved objects."
            : "No answer requested yet.",
        }),
      ]),
    ];
  }
  return cited.map(objectCard);
}

function objectCard(object) {
  const partial = object.evidence_in_context < object.evidence_total;
  return el("div", { class: "ask__card", id: cardId(object.id) }, [
    el("div", { class: "ask__card-head" }, [
      el("span", { class: "ask__card-title", text: object.title }),
      el("button", {
        class: "row__open",
        style: "opacity:1",
        text: "Open",
        onClick: () => openObjectPeek(object.id),
      }),
    ]),
    el("div", { class: "mono muted", style: "font-size:12px", text: object.id }),
    el("div", { class: "ask__card-statement", text: object.statement }),
    el("div", { class: "props props--tight" }, [
      ...property("Category", object.category),
      ...property("Status", object.status),
      ...property("Owner", object.owner),
      ...property("Confidence", object.confidence),
      ...property("Last confirmed", object.last_confirmed),
    ]),
    partial
      ? el("div", { class: "secondary muted", style: "font-size:13px" }, [
          el("span", {
            text:
              `${object.evidence_in_context} of ${object.evidence_total} `
              + "evidence items were in the context the model read.",
          }),
        ])
      : null,
    // The scope pruned this object's out-of-scope evidence, but its statement
    // was written from all of it. Saying so is the honest limit of a scope.
    object.evidence_out_of_scope
      ? el("div", { class: "secondary ask__straddle" }, [
          statusCue(
            "warning",
            `${object.evidence_total} of ${object.evidence_all_sources} `
              + "evidence items in scope"
          ),
          el("span", {
            class: "muted",
            style: "font-size:12px",
            text: "statement synthesized from all of it",
          }),
        ])
      : null,
    // Cited evidence opens by default: it is what the answer rests on.
    ...object.evidence.map((entry) => evidenceBlock(entry, { open: Boolean(entry.cited) })),
  ]);
}

// -- inspection blocks ------------------------------------------------------

function toggleBlock(label, count, build, { open = false } = {}) {
  const panel = el("div", { style: "padding:2px 0 8px" }, [build()]);
  panel.hidden = !open;
  const head = el(
    "button",
    {
      class: "group-toggle",
      "aria-expanded": open ? "true" : "false",
      onClick: () => {
        panel.hidden = !panel.hidden;
        head.setAttribute("aria-expanded", panel.hidden ? "false" : "true");
      },
    },
    [
      icon("chevron"),
      el("span", { text: label }),
      el("span", { class: "group-toggle__count", text: String(count) }),
    ]
  );
  return el("section", {}, [head, panel]);
}

function consideredBlock(ctx) {
  // The single most useful inspection affordance in the tab: it answers "was
  // the right object even in the room?", which separates a retrieval failure
  // from a reasoning failure. Nothing else in this UI exposes it.
  const uncited = state.result
    ? state.context.objects.filter((value) => !value.cited)
    : [];
  if (!state.result || !uncited.length) return null;
  return toggleBlock("Considered but not cited", uncited.length, () =>
    el(
      "div",
      { class: "rows" },
      uncited.map((object) =>
        el(
          "div",
          { class: "row", onClick: () => openObjectPeek(object.id) },
          [
            el("span", { class: "row__label", text: object.category }),
            el("span", { class: "row__title", text: object.title }),
            el("span", {
              class: "muted",
              style: "font-size:12px",
              text: object.score === null ? "one-hop" : `score ${object.score}`,
            }),
            el("span", { class: "row__id", text: object.selection_reason }),
          ]
        )
      )
    )
  );
}

function omissionsBlock() {
  // build_context_packet degrades silently under the character budget and
  // records each drop. That list is the quiet failure mode of `ask`, so it is
  // a warning here rather than a footnote.
  const omissions = state.context.omissions;
  if (!omissions.length) return null;
  return callout(
    "warning",
    `${omissions.length} omission${omissions.length === 1 ? "" : "s"} to fit the character budget`,
    el("ul", { class: "list-tight" }, omissions.map((value) => el("li", { text: value })))
  );
}

function reviewsCallout() {
  return callout(
    "info",
    `${state.context.reviews.length} pending review item${
      state.context.reviews.length === 1 ? "" : "s"
    } in this context — proposed, not canonical`,
    el(
      "div",
      {},
      state.context.reviews.map((value) =>
        el("div", { class: "secondary" }, [
          el("a", {
            href: "#",
            text: value.title,
            onClick: (event) => {
              event.preventDefault();
              openReviewPeek(value.id);
            },
          }),
          el("span", { text: ` · ${value.reason}` }),
        ])
      )
    ),
    "queue"
  );
}

function contextBlock() {
  return toggleBlock(
    "The exact context sent to the model",
    `${state.context.retrieval.chars.toLocaleString()} chars`,
    () =>
      el("div", {}, [
        el("div", { class: "muted", style: "font-size:12px;margin-bottom:6px" }, [
          el("span", { class: "mono", text: `sha256 ${state.context.packet_sha256}` }),
        ]),
        el("pre", { class: "preview", text: state.context.markdown }),
      ])
  );
}
