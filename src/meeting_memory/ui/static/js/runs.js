// Tab 1 — Knowledge Updates.
//
// Purpose: a knowledge inbox for verifying what the pipeline decided on its
// own. Objects are grouped by manifest bucket ("what changed") in the order
// the run payload emits them — Created, then Refined, then Reconfirmed —
// which matches how the page is read: newest knowledge first, then what
// changed, then what merely held. Reconfirmed is collapsed by default because
// it carries no new information and would otherwise bury the run's actual
// news under its own restatements.

import { api } from "./api.js";
import { el, icon, mount, statusCue, timezoneSuffix } from "./dom.js";
import {
  attributedSourceCount,
  defaultSelectionId,
  filterGroups,
  groupsFor,
  inboxGroups,
  objectCount,
  openReviewIds,
  reviewCandidateCount,
} from "./knowledge_inbox.js";
import { objectView } from "./objects.js";
import { openQueueCase } from "./reviewpeek.js";
import {
  chartSeries,
  fullDateLabel,
  pointX,
  runOptionLabel,
  yTicks,
} from "./runs_chart.js";
import { busy, empty, reportError } from "./ui.js";
import { bindRunDetailRefresh, emit } from "./store.js";

const state = {
  runs: null,
  runId: null,
  detail: null,
  filters: { search: "", category: "" },
  groupBy: "change",
  selectedId: null,
  // Keyed by "<grouping>:<group key>", so collapsing Reconfirmed in change
  // grouping cannot silently collapse a source group, and each group falls back
  // to its own `collapsed` default until the reader touches it.
  expanded: {},
  subsections: {},
  runDetailsOpen: false,
};

// A run with one source has nothing to regroup, so the control is withheld --
// but state.groupBy is never rewritten here, so a reader who chose source
// grouping gets it back on the next multi-source run.
function effectiveGroupBy() {
  return state.groupBy === "source" && attributedSourceCount(state.detail) >= 2
    ? "source"
    : "change";
}

function currentGroups() {
  return filterGroups(groupsFor(state.detail, effectiveGroupBy()), state.filters);
}

let currentCtx = null;
let chartNode = null;

export const runsView = {
  id: "runs",
  label: "Updates",
  icon: "layers",
  title: "Knowledge Updates",

  count() {
    return state.detail ? objectCount(inboxGroups(state.detail)) : null;
  },

  async render(ctx) {
    currentCtx = ctx;
    ctx.content.style.padding = "0";
    mount(ctx.content, busy("Loading run manifests…"));
    try {
      state.runs = await api.runs();
      if (!state.runs.runs.length) {
        mount(ctx.filters, filterBar(ctx));
        mount(ctx.content, empty("No run manifests are available."));
        return;
      }
      if (!state.runs.runs.some((run) => run.run_id === state.runId)) {
        state.runId = state.runs.runs[0].run_id;
      }
      state.detail = await api.run(state.runId);
      chartNode = null;
      state.runDetailsOpen = false;
      state.selectedId = defaultSelectionId(currentGroups());
      mount(ctx.filters, filterBar(ctx));
      mount(ctx.content, page());
      renderList();
      renderReader();
      emit();
    } catch (error) {
      reportError(error);
      mount(ctx.content, empty(`Could not load runs: ${error.message}`));
    }
  },
};

// Re-fetch the run the view is showing, without the full reload render() does:
// a merge or removal only changes which rows are still present, so the run
// selection, the filters, and the open state of Run details all survive it. The
// reader keeps its place unless the object it was showing is the one that went.
async function refreshRunDetail() {
  // The peek that starts a merge can be opened from another tab, and the content
  // node belongs to whichever view is mounted in it, so only touch the DOM while
  // this view still owns it. Nothing goes stale by skipping: render() re-fetches
  // on every activation anyway.
  if (!currentCtx || !state.detail || !document.querySelector(".inbox-shell")) {
    return;
  }
  let detail;
  try {
    detail = await api.run(state.runId);
  } catch (error) {
    reportError(error);
    return;
  }
  state.detail = detail;
  chartNode = null;
  const groups = currentGroups();
  const readable = groups.some((group) =>
    group.rows.some((row) => row.id === state.selectedId && row.present)
  );
  if (!readable) state.selectedId = defaultSelectionId(groups);
  mount(currentCtx.content, page());
  renderList();
  renderReader();
  emit();
}

bindRunDetailRefresh(refreshRunDetail);

// -- filter bar ---------------------------------------------------------------

function availableCategories() {
  const found = new Set();
  for (const group of inboxGroups(state.detail)) {
    for (const row of group.rows) {
      if (row.category) found.add(row.category);
    }
  }
  return [...found].sort();
}

function onFilterChange() {
  renderList();
  renderReader();
}

function clearFilters() {
  state.filters = { search: "", category: "" };
  if (currentCtx) mount(currentCtx.filters, filterBar(currentCtx));
  onFilterChange();
}

function filterBar(ctx) {
  const runSelect = el(
    "select",
    {
      onChange: (event) => {
        state.runId = event.target.value;
        runsView.render(ctx);
      },
    },
    state.runs.runs.map((run) =>
      el("option", {
        value: run.run_id,
        selected: run.run_id === state.runId,
        text: runOptionLabel(run, state.runs.runs),
      })
    )
  );

  const utcNote = "Run dates and times in this list use the manifest's UTC "
    + "clock, and a date that was rerun lists each attempt; the timestamps on "
    + "the page are shown in " + timezoneSuffix() + ".";
  const runChip = el("span", { class: "chip", title: utcNote }, [
    el("span", { class: "chip__label", text: "Run date" }),
    runSelect,
  ]);

  const searchInput = el("input", {
    placeholder: "Search title or statement…",
    value: state.filters.search,
    onInput: (event) => {
      state.filters.search = event.target.value;
      onFilterChange();
    },
  });
  const searchChip = el("span", { class: "chip" }, [
    el("span", { class: "chip__label", text: "Search" }),
    searchInput,
  ]);

  const categorySelect = el(
    "select",
    {
      onChange: (event) => {
        state.filters.category = event.target.value;
        onFilterChange();
      },
    },
    [
      el("option", {
        value: "",
        text: "All categories",
        selected: state.filters.category === "",
      }),
      ...availableCategories().map((category) =>
        el("option", {
          value: category,
          text: category,
          selected: state.filters.category === category,
        })
      ),
    ]
  );
  const categoryChip = el("span", { class: "chip" }, [
    el("span", { class: "chip__label", text: "Category" }),
    categorySelect,
  ]);

  // Withheld on a single-source run, where regrouping is a no-op and the
  // control would be chrome that does nothing.
  const groupChip =
    attributedSourceCount(state.detail) >= 2
      ? el("span", { class: "chip" }, [
          el("span", { class: "chip__label", text: "Group by" }),
          el("span", { class: "segmented" }, [
            segmentButton(ctx, "change", "Change"),
            segmentButton(ctx, "source", "Source"),
          ]),
        ])
      : null;

  return [runChip, searchChip, categoryChip, groupChip].filter(Boolean);
}

// Regrouping reads the run detail already in memory, exactly as filtering does:
// no api.run, no api.runs, no runsView.render.
function segmentButton(ctx, value, label) {
  const active = state.groupBy === value;
  return el("button", {
    class: `segmented__option${active ? " is-active" : ""}`,
    "aria-pressed": active ? "true" : "false",
    text: label,
    onClick: () => {
      if (state.groupBy === value) return;
      state.groupBy = value;
      if (ctx) mount(ctx.filters, filterBar(ctx));
      onFilterChange();
    },
  });
}

// -- page shell -----------------------------------------------------------

function page() {
  return [
    el("div", { class: "inbox-shell" }, [
      inboxHeader(),
      el("div", { class: "run-details-mount" }, [buildRunDetailsSection()]),
      el("div", { class: "split" }, [
        el("div", { class: "queue inbox__list" }),
        el("div", { class: "inbox__reader-pane" }, [
          el(
            "button",
            { class: "inbox__back", onClick: () => setMobileReaderOpen(false) },
            [el("span", { text: "← Back to knowledge list" })]
          ),
          el("div", { class: "detail inbox__reader" }),
        ]),
      ]),
    ]),
  ];
}

function setMobileReaderOpen(open) {
  const split = document.querySelector(".split");
  if (split) split.classList.toggle("is-reading", open);
}

function inboxHeader() {
  const summary = state.detail.summary;
  const counts = summary.counts;
  const runStatus =
    summary.status === "success"
      ? statusCue("good", "success")
      : summary.status === "partial_failure"
      ? statusCue("warning", "partial failure")
      : statusCue("critical", "failed");
  const total = objectCount(inboxGroups(state.detail));
  const reviewCount = reviewCandidateCount(state.detail);

  return el("div", { class: "inbox__header" }, [
    el("div", { class: "page-title", text: fullDateLabel(summary.started_at) }),
    el("div", { class: "page-sub" }, [
      runStatus,
      el("span", {
        text: `  ·  meeting dates ${summary.target_dates[0] || "—"}–${
          summary.target_dates[summary.target_dates.length - 1] || "—"
        }`,
      }),
    ]),
    el(
      "div",
      { class: "inbox__header-meta" },
      [
        el("span", {
          class: "inbox__object-count",
          text: `${total} knowledge object${total === 1 ? "" : "s"}`,
        }),
        reviewCount ? reviewLink(reviewCount, openReviewIds(state.detail)) : null,
        counts.errors
          ? el(
              "button",
              { class: "inbox__error-warning", onClick: openRunDetails },
              [
                icon("alert"),
                el("span", {
                  text: `${counts.errors} run error${counts.errors === 1 ? "" : "s"}`,
                }),
              ]
            )
          : null,
        runDetailsToggle(),
      ].filter(Boolean)
    ),
  ]);
}

// Lives in the header rather than at the foot of the page: the run stats
// describe the run the whole view is already scoped to, so they belong next to
// the run identity instead of a screen below the split.
function runDetailsToggle() {
  return el(
    "button",
    {
      class: "inbox__run-details-toggle",
      "aria-expanded": state.runDetailsOpen ? "true" : "false",
      onClick: () => setRunDetailsOpen(!state.runDetailsOpen),
    },
    [icon("chevron"), el("span", { text: "Run details" })]
  );
}

// "N sent to review" is the run's record of what it queued, not a live count:
// those cases may since have been resolved, rejected, or deleted outright. The
// queue lists pending cases, so linking into it when none are still pending
// lands on an empty page. Say how many can still be worked, and only offer the
// link when one can.
function reviewLink(total, openIds) {
  const label = el("span", { text: `${total} sent to review` });
  const note = (text) => el("span", { class: "inbox__review-note", text });

  if (!openIds.length) {
    return el("span", { class: "inbox__review-closed" }, [
      label,
      note("none still open"),
    ]);
  }
  return el(
    "a",
    {
      href: "#",
      class: "inbox__review-link",
      onClick: (event) => {
        event.preventDefault();
        openQueueCase(openIds[0]);
      },
    },
    [
      label,
      openIds.length < total ? note(`${openIds.length} still open`) : null,
      icon("chevronRight"),
    ].filter(Boolean)
  );
}

// -- knowledge list ---------------------------------------------------------

function renderList() {
  const node = document.querySelector(".inbox__list");
  if (!node) return;
  const groups = currentGroups();
  if (!groups.some((group) => group.rows.some((row) => row.id === state.selectedId))) {
    state.selectedId = defaultSelectionId(groups);
  }
  mount(node, groups.length ? groups.map((group) => listGroupNode(group)) : [listEmptyState()]);
}

function listEmptyState() {
  if (state.filters.search || state.filters.category) {
    return el("div", { class: "empty" }, [
      el("div", { text: "No knowledge objects match these filters." }),
      el("button", { class: "btn", text: "Clear filters", onClick: clearFilters }),
    ]);
  }
  return el("div", { class: "empty", text: "This run has no ordinary knowledge objects." });
}

function listGroupNode(group) {
  const expansionKey = `${effectiveGroupBy()}:${group.key}`;
  const open = state.expanded[expansionKey] ?? !group.collapsed;
  const live = group.rows.filter((row) => row.present);
  const removed = group.rows.filter((row) => !row.present);
  // Only split a group that has actually lost something. On the ordinary path
  // a "Live" heading above every group is noise, and the group's own count
  // already says all there is to say.
  const rows = el(
    "div",
    {},
    removed.length
      ? [
          subsectionNode(
            group,
            "live",
            "Live",
            live.map((row) => listItemNode(row, group))
          ),
          subsectionNode(
            group,
            "removed",
            "Removed",
            removed.map((row) => tombstoneNode(row))
          ),
        ]
      : live.map((row) => listItemNode(row, group))
  );
  rows.hidden = !open;

  const toggle = el(
    "button",
    {
      class: "queue__group-label group-toggle",
      "aria-expanded": open ? "true" : "false",
      onClick: () => {
        state.expanded[expansionKey] = rows.hidden;
        rows.hidden = !rows.hidden;
        toggle.setAttribute("aria-expanded", rows.hidden ? "false" : "true");
      },
    },
    [
      icon("chevron"),
      el("span", { text: group.label }),
      // A run spans several meeting dates, so two days of the same recurring
      // meeting would otherwise render as two identical headings.
      group.date
        ? el("span", { class: "group-toggle__date", text: group.date })
        : null,
      el("span", { class: "group-toggle__count", text: String(group.rows.length) }),
    ].filter(Boolean)
  );
  const node = el("div", { class: "queue__group" }, [toggle, rows]);
  if (effectiveGroupBy() === "source") node.dataset.sourceGroup = group.key;
  return node;
}

// Labelled halves let the group's own count decompose in place -- Created 15 is
// Live 1 plus Removed 14 -- instead of a sentence explaining the arithmetic, and
// they use the same "Label N" shape as the group heading above them. Live opens
// by default; Removed stays shut until asked for.
function subsectionNode(group, kind, label, children) {
  const key = `${effectiveGroupBy()}:${group.key}:${kind}`;
  const open = state.subsections[key] ?? kind === "live";
  const body = el("div", {}, children);
  body.hidden = !open;

  const toggle = el(
    "button",
    {
      class: `inbox__subgroup inbox__subgroup--${kind}`,
      "aria-expanded": open ? "true" : "false",
      onClick: () => {
        state.subsections[key] = body.hidden;
        body.hidden = !body.hidden;
        toggle.setAttribute("aria-expanded", body.hidden ? "false" : "true");
      },
    },
    [
      icon("chevron"),
      el("span", { text: label }),
      el("span", { class: "group-toggle__count", text: String(children.length) }),
    ]
  );
  return el("div", {}, [toggle, body]);
}

function listItemNode(row, group) {
  // In source grouping the heading names the source, so the outcome has to
  // travel with the row; sourceGroups carries the payload's own bucket label.
  const outcome = row.outcomeLabel ?? group.label;
  // Attribution stays visible in change grouping too, so reading where a row
  // came from never requires switching modes.
  const sourceLine =
    effectiveGroupBy() === "change" && row.source
      ? el("div", { class: "inbox__item-source", text: sourceLabel(row.source) })
      : null;
  return el(
    "button",
    {
      class: `queue__item inbox__item${row.id === state.selectedId ? " is-active" : ""}`,
      onClick: () => selectItem(row.id),
    },
    [
      el("div", { class: "queue__item-meta" }, [
        el("span", { text: row.category || "—" }),
        el("span", { class: "inbox__item-outcome", text: outcome }),
      ]),
      el("div", { class: "queue__item-title", text: row.title }),
      el("div", { class: "inbox__item-preview", text: row.statement || "" }),
      sourceLine,
    ].filter(Boolean)
  );
}

function describedSource(source) {
  return (state.detail.sources || []).find((entry) => entry.source === source) || null;
}

function sourceLabel(source) {
  const described = describedSource(source);
  return described ? described.label : source;
}

// Removal takes the title, category, and statement with it and leaves only the
// ID, so the ordinary row shape renders a bare slug in the title slot and an
// empty preview beneath it -- three lines that look like a knowledge object you
// could open. Reader navigation already skips absent rows, so it never was one.
// All that is left to show is the ID, and the disclosure above already says
// these are removed.
function tombstoneNode(row) {
  return el("div", { class: "inbox__tombstone" }, [
    icon("trash"),
    el("span", { class: "inbox__tombstone-id", text: row.id, title: row.id }),
  ]);
}

// -- knowledge reader ---------------------------------------------------------

function flattenRows(groups) {
  return groups.flatMap((group) => group.rows.map((row) => ({ ...row, groupLabel: group.label })));
}

function selectItem(id) {
  if (!id) return;
  state.selectedId = id;
  setMobileReaderOpen(true);
  renderList();
  renderReader();
}

function renderReader() {
  const node = document.querySelector(".inbox__reader");
  if (!node) return;
  const groups = currentGroups();
  const flat = flattenRows(groups);
  const row = flat.find((entry) => entry.id === state.selectedId);
  if (!row) {
    mount(node, readerEmptyState());
    return;
  }
  paintReaderRow(node, row, flat.filter((entry) => entry.present));
}

function readerEmptyState() {
  const rawGroups = inboxGroups(state.detail);
  if (objectCount(rawGroups) === 0) {
    return el("div", {
      class: "empty",
      text: "This run did not create, refine, or reconfirm any knowledge objects.",
    });
  }
  const anyPresent = rawGroups.some((group) => group.rows.some((row) => row.present));
  if (!anyPresent) {
    return el("div", {
      class: "empty",
      text: "Every knowledge object from this run is no longer present in the canonical repository.",
    });
  }
  return el("div", { class: "empty" }, [
    el("div", { text: "No knowledge objects match these filters." }),
    el("button", { class: "btn", text: "Clear filters", onClick: clearFilters }),
  ]);
}

function readerNav(flat, index) {
  const prevDisabled = index <= 0;
  const nextDisabled = index < 0 || index >= flat.length - 1;
  return el("div", { class: "inbox__nav" }, [
    el(
      "button",
      { class: "btn", disabled: prevDisabled, onClick: () => selectItem(flat[index - 1].id) },
      [el("span", { text: "← Previous" })]
    ),
    el("span", {
      class: "inbox__nav-position",
      text: flat.length && index >= 0 ? `${index + 1} of ${flat.length}` : "",
    }),
    el(
      "button",
      { class: "btn", disabled: nextDisabled, onClick: () => selectItem(flat[index + 1].id) },
      [el("span", { text: "Next →" })]
    ),
  ]);
}

function refinementSection(row) {
  if (row.bucket !== "objects_refined") return null;
  const refinement = row.refinement || {};
  return el("div", {}, [
    el("h2", { class: "section", text: "Refinement" }),
    refinement.available
      ? el("div", { class: "compare" }, [
          el("div", { class: "compare__col" }, [
            el("div", { class: "compare__head", text: "Before" }),
            el("div", { class: "compare__text" }, diffSegments(refinement.diff.left)),
          ]),
          el("div", { class: "compare__col" }, [
            el("div", { class: "compare__head", text: "After" }),
            el("div", { class: "compare__text" }, diffSegments(refinement.diff.right)),
          ]),
        ])
      : el("div", { class: "callout" }, [
          icon("history"),
          el("div", { class: "callout__body" }, [
            el("div", { text: `Refined — prior text unavailable (${refinement.reason}).` }),
            refinement.entry
              ? el("div", {
                  class: "secondary",
                  text: `History: ${refinement.entry.date} — ${refinement.entry.text}`,
                })
              : null,
            refinement.after
              ? el("div", { class: "compare__text", text: refinement.after })
              : null,
          ]),
        ]),
  ]);
}

function paintReaderRow(node, row, presentFlat) {
  const index = presentFlat.findIndex((entry) => entry.id === row.id);
  mount(
    node,
    [
      readerNav(presentFlat, index),
      el("div", { class: "page-title", text: row.title || row.id }),
      el("div", { class: "page-sub" }, [
        el("span", { text: row.category || "—" }),
        el("span", { text: "  ·  " }),
        // groupLabel names the source once the list is grouped by source, so
        // the reader takes the outcome the row carries when it has one.
        el("span", { text: row.outcomeLabel ?? row.groupLabel }),
      ]),
      el("h2", { class: "section", text: "Statement" }),
      el("div", { class: "compare__text", text: row.statement || "" }),
      refinementSection(row),
      el("h2", { class: "section", text: "Evidence" }),
      busy("Loading evidence and details…"),
    ].filter(Boolean)
  );

  hydrateReader(row, node, presentFlat, index);
}

async function hydrateReader(row, node, presentFlat, index) {
  try {
    const object = await api.knowledge(row.id);
    if (state.selectedId !== row.id) return;
    mount(
      node,
      [readerNav(presentFlat, index), objectView(object), refinementSection(row)].filter(
        Boolean
      )
    );
  } catch (error) {
    if (state.selectedId !== row.id) return;
    mount(
      node,
      [
        readerNav(presentFlat, index),
        el("div", { class: "page-title", text: row.title || row.id }),
        el("h2", { class: "section", text: "Statement" }),
        el("div", { class: "compare__text", text: row.statement || "" }),
        refinementSection(row),
        el("div", { class: "callout callout--danger" }, [
          icon("alert"),
          el("div", { class: "callout__body" }, [
            el("div", { class: "callout__title", text: "Could not load evidence and details" }),
            el("div", { text: `${error.type}: ${error.message}` }),
          ]),
        ]),
        el("button", {
          class: "btn",
          text: "Retry",
          onClick: () => paintReaderRow(node, row, presentFlat),
        }),
      ].filter(Boolean)
    );
    reportError(error);
  }
}

function diffSegments(segments) {
  return (segments || []).map((segment) =>
    el("span", {
      class:
        segment.kind === "added" ? "diff-add" : segment.kind === "removed" ? "diff-del" : "",
      text: segment.text,
    })
  );
}

// -- run details --------------------------------------------------------------

function statTile(label, value, { role = null, iconName = null, note = null } = {}) {
  return el("div", { class: `stat${role ? ` stat--${role}` : ""}` }, [
    el("div", { class: "stat__label", text: label }),
    el("div", { class: "stat__value" }, [
      role ? icon(iconName || "alert") : null,
      el("span", { text: String(value) }),
    ]),
    note ? el("div", { class: "stat__note", text: note }) : null,
  ]);
}

function sourceSummary(counts, unchanged) {
  return el("div", { class: "source-summary" }, [
    el("div", { class: "source-summary__counts" }, [
      el("span", {}, [
        el("strong", { text: counts.sources_examined }),
        " sources examined",
      ]),
      el("span", {
        class: "source-summary__separator",
        "aria-hidden": "true",
        text: "·",
      }),
      el("span", {}, [el("strong", { text: counts.sources_processed }), " processed"]),
      el("span", {
        class: "source-summary__separator",
        "aria-hidden": "true",
        text: "·",
      }),
      el("span", {}, [el("strong", { text: unchanged }), " unchanged"]),
    ]),
    el("div", {
      class: "source-summary__note",
      text: "Unchanged sources required no new processing.",
    }),
    processedSourceList(),
    counts.candidates_rejected
      ? el("div", {
          class: "source-summary__note",
          text: `${counts.candidates_rejected} candidates rejected during this run.`,
        })
      : null,
    // Two files carrying one meeting is usually a double calendar sync, but a
    // reader looking for a meeting that never appears needs to be told where
    // it went rather than left to assume it was missed.
    counts.sources_deduplicated
      ? el("div", {
          class: "source-summary__note",
          text: `${counts.sources_deduplicated} sources withheld as duplicates of another source.`,
        })
      : null,
    // A removed or merged-away fact that sources keep restating is the signal
    // that the retirement may have been wrong, so the block is stated rather
    // than left silent.
    counts.candidates_suppressed
      ? el("div", {
          class: "source-summary__note",
          text: `${counts.candidates_suppressed} candidates suppressed by a previously retired object.`,
        })
      : null,
  ]);
}

// The run's sources are what a reader is looking at when the question "what
// came out of that meeting?" occurs to them, so each one opens its own group
// rather than only naming itself.
function processedSourceList() {
  const sources = state.detail.sources_processed || [];
  if (!sources.length) return null;
  return el("div", { class: "source-summary__sources" }, [
    el("div", { class: "source-summary__note", text: "Sources processed" }),
    el(
      "ul",
      { class: "list-tight" },
      sources.map((source) => {
        const described = describedSource(source);
        return el("li", {}, [
          el("button", {
            class: "source-summary__source",
            title: source,
            text: described && described.date
              ? `${described.label} · ${described.date}`
              : sourceLabel(source),
            onClick: () => openSourceGroup(source),
          }),
        ]);
      })
    ),
  ]);
}

function openSourceGroup(source) {
  state.groupBy = "source";
  state.expanded[`source:${source}`] = true;
  // Run details sits above the split, so leaving it open would push the group
  // the reader just asked for off screen.
  setRunDetailsOpen(false);
  if (currentCtx) mount(currentCtx.filters, filterBar(currentCtx));
  renderList();
  const group = currentGroups().find((entry) => entry.key === source);
  const first = group ? group.rows.find((row) => row.present) : null;
  if (first) selectItem(first.id);
  else renderReader();
  const node = document.querySelector(`[data-source-group="${CSS.escape(source)}"]`);
  if (node) node.scrollIntoView({ block: "start" });
}

function openRunDetails() {
  setRunDetailsOpen(true);
}

function setRunDetailsOpen(open) {
  state.runDetailsOpen = open;
  const toggle = document.querySelector(".inbox__run-details-toggle");
  if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
  renderRunDetails();
}

function renderRunDetails() {
  const node = document.querySelector(".run-details-mount");
  if (!node) return;
  mount(node, buildRunDetailsSection());
}

function buildRunDetailsSection() {
  if (!state.runDetailsOpen) return null;

  const counts = state.detail.summary.counts;
  const unchanged =
    counts.sources_skipped ?? counts.sources_examined - counts.sources_processed;
  if (!chartNode) chartNode = knowledgeChart(state.runs.runs);

  return el(
    "section",
    { class: "run-details" },
    [
      el("div", { class: "stat-row" }, [
        statTile("Created", counts.objects_created),
        statTile("Reconfirmed", counts.objects_reconfirmed),
        statTile("Refined", counts.objects_refined),
        statTile("Sent to review", counts.review_items_created, {
          role: "warning",
          iconName: "queue",
        }),
        statTile("Errors", counts.errors, {
          role: counts.errors ? "critical" : "good",
          iconName: counts.errors ? "alert" : "check",
        }),
      ]),
      sourceSummary(counts, unchanged),
      counts.errors
        ? el("div", { class: "callout callout--danger" }, [
            icon("alert"),
            el("div", { class: "callout__body" }, [
              el("div", { class: "callout__title", text: "Run errors" }),
              el(
                "ul",
                { class: "list-tight" },
                state.detail.errors.map((error) =>
                  el("li", { text: error.error || JSON.stringify(error) })
                )
              ),
            ]),
          ])
        : null,
      chartNode,
    ].filter(Boolean)
  );
}

const SVG_NS = "http://www.w3.org/2000/svg";

function svgElement(tag, attributes = {}, children = []) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attributes)) {
    node.setAttribute(name, String(value));
  }
  for (const child of [].concat(children)) node.appendChild(child);
  return node;
}

function drawLineChart(svg, series, width, config) {
  const { height, margins, valueKey, ariaTitle, pointLabel, showDates } = config;
  const plotWidth = width - margins.left - margins.right;
  const plotHeight = height - margins.top - margins.bottom;
  const plotBottom = margins.top + plotHeight;
  const ticks = yTicks(series.map((entry) => entry[valueKey]));
  const maximum = ticks[ticks.length - 1];
  const points = series.map((entry, index) => ({
    ...entry,
    value: entry[valueKey],
    x: pointX(index, series.length, margins.left, plotWidth),
    y: margins.top + (1 - entry[valueKey] / maximum) * plotHeight,
  }));

  svg.replaceChildren();
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.appendChild(svgElement("title", {}, [document.createTextNode(ariaTitle)]));

  for (const tick of ticks) {
    const y = margins.top + (1 - tick / maximum) * plotHeight;
    svg.appendChild(
      svgElement("line", {
        class: "knowledge-chart__grid",
        x1: margins.left,
        x2: width - margins.right,
        y1: y,
        y2: y,
      })
    );
    const label = svgElement("text", {
      class: "knowledge-chart__axis-label",
      x: margins.left - 10,
      y: y + 4,
      "text-anchor": "end",
    });
    label.textContent = String(tick);
    svg.appendChild(label);
  }

  if (points.length) {
    const line = points.map((point) => `${point.x},${point.y}`).join(" L");
    svg.appendChild(
      svgElement("path", {
        class: "knowledge-chart__area",
        d: `M${points[0].x},${plotBottom} L${line} L${points.at(-1).x},${plotBottom} Z`,
      })
    );
    svg.appendChild(
      svgElement("path", { class: "knowledge-chart__line", d: `M${line}` })
    );
  }

  points.forEach((point, index) => {
    const label = pointLabel(point);
    const circle = svgElement("circle", {
      class: "knowledge-chart__point",
      cx: point.x,
      cy: point.y,
      r: 4,
      tabindex: 0,
      role: "img",
      "aria-label": label,
    });
    circle.appendChild(svgElement("title", {}, [document.createTextNode(label)]));
    svg.appendChild(circle);

    const count = svgElement("text", {
      class: "knowledge-chart__count",
      x: point.x,
      y: Math.max(16, point.y - 10),
      "text-anchor": "middle",
    });
    count.textContent = String(point.value);
    svg.appendChild(count);

    if (!showDates) return;
    const showDate =
      width >= 720 ||
      points.length <= 6 ||
      index % 2 === 0 ||
      index === points.length - 1;
    if (showDate) {
      const date = svgElement("text", {
        class: "knowledge-chart__date",
        x: point.x,
        y: height - 18,
        "text-anchor": "middle",
      });
      date.textContent = point.dateLabel;
      svg.appendChild(date);
    }
  });
}

function miniChart(series, config) {
  const svg = svgElement("svg", {
    class: "knowledge-chart__svg",
    height: config.height,
    role: "img",
    "aria-label": config.ariaTitle,
    preserveAspectRatio: "none",
  });
  drawLineChart(svg, series, 960, config);

  const redraw = (measuredWidth) => {
    if (measuredWidth > 0) drawLineChart(svg, series, Math.max(320, measuredWidth), config);
  };
  if (typeof ResizeObserver !== "undefined") {
    const observer = new ResizeObserver((entries) => {
      const measuredWidth = Math.round(entries[0].contentRect.width);
      if (!svg.isConnected && measuredWidth === 0) {
        observer.disconnect();
        return;
      }
      redraw(measuredWidth);
    });
    observer.observe(svg);
  } else if (typeof requestAnimationFrame !== "undefined") {
    requestAnimationFrame(() => redraw(Math.round(svg.getBoundingClientRect().width)));
  }

  return el("div", { class: "knowledge-chart__mini" }, [
    el("div", { class: "knowledge-chart__subhead", text: config.subhead }),
    svg,
  ]);
}

function knowledgeChart(runs) {
  const series = chartSeries(runs);
  const body = series.length
    ? el("div", { class: "knowledge-chart__body" }, [
        miniChart(series, {
          height: 190,
          margins: { top: 24, right: 18, bottom: 16, left: 46 },
          valueKey: "count",
          subhead: "Knowledge objects created",
          ariaTitle: "Knowledge objects created per run",
          showDates: false,
          pointLabel: (point) =>
            `${point.date}: ${point.value} knowledge object${point.value === 1 ? "" : "s"} created`,
        }),
        miniChart(series, {
          height: 210,
          margins: { top: 24, right: 18, bottom: 44, left: 46 },
          valueKey: "sourcesProcessed",
          subhead: "Sources processed",
          ariaTitle: "Sources processed per run",
          showDates: true,
          pointLabel: (point) =>
            `${point.date}: ${point.value} source${point.value === 1 ? "" : "s"} processed`,
        }),
      ])
    : el("div", {
        class: "knowledge-chart__empty",
        text: "No run history is available yet.",
      });

  return el(
    "section",
    { class: "knowledge-chart", "aria-labelledby": "knowledge-chart-title" },
    [
      el("div", { class: "knowledge-chart__header" }, [
        el("div", {
          id: "knowledge-chart-title",
          class: "knowledge-chart__title",
          text: "Run history",
        }),
        el("div", {
          class: "knowledge-chart__subtitle",
          text: "Per run · last 12 runs · run dates in UTC",
        }),
      ]),
      body,
    ]
  );
}

// -- keyboard ---------------------------------------------------------------

export function handleRunsKey(event) {
  if (!state.detail) return false;
  const flat = flattenRows(currentGroups()).filter((row) => row.present);
  if (!flat.length) return false;
  const index = flat.findIndex((row) => row.id === state.selectedId);
  if (event.key === "j" && index < flat.length - 1) {
    selectItem(flat[index + 1].id);
    return true;
  }
  if (event.key === "k" && index > 0) {
    selectItem(flat[index - 1].id);
    return true;
  }
  return false;
}
