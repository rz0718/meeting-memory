// Tab 1 — Today's Knowledge.
//
// Purpose: verify what the pipeline decided on its own. These changes were
// applied without a human, so the tab is grouped by manifest bucket ("what
// changed") rather than by category, and refined/reconfirmed sit above created
// because they warrant more scrutiny.

import { api } from "./api.js";
import { badge, el, icon, instant, mount, statusCue, timezoneSuffix } from "./dom.js";
import { openObjectPeek } from "./objects.js";
import { openReviewPeek } from "./reviewpeek.js";
import { busy, empty, reportError } from "./ui.js";
import { emit } from "./store.js";

const state = {
  runs: null,
  runId: null,
  detail: null,
  start: "",
  end: "",
  expanded: {
    objects_refined: true,
    objects_reconfirmed: true,
    objects_created: true,
    review_items_created: true,
  },
};

export const runsView = {
  id: "runs",
  label: "Today's Knowledge",
  icon: "layers",
  title: "Today's Knowledge",

  count() {
    return state.detail ? state.detail.summary.counts.objects_created : null;
  },

  async render(ctx) {
    mount(ctx.content, busy("Loading run manifests…"));
    try {
      state.runs = await api.runs({ start: state.start, end: state.end });
      if (!state.runs.runs.length) {
        mount(ctx.filters, filterBar(ctx));
        mount(ctx.content, empty("No run manifests match this date range."));
        return;
      }
      if (!state.runs.runs.some((run) => run.run_id === state.runId)) {
        state.runId = state.runs.runs[0].run_id;
      }
      state.detail = await api.run(state.runId);
      mount(ctx.filters, filterBar(ctx));
      mount(ctx.content, runPage(ctx));
      emit();
    } catch (error) {
      reportError(error);
      mount(ctx.content, empty(`Could not load runs: ${error.message}`));
    }
  },
};

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
        text: run.started_at.slice(0, 10),
      })
    )
  );

  const start = el("input", {
    type: "date",
    value: state.start,
    onChange: (event) => {
      state.start = event.target.value;
      runsView.render(ctx);
    },
  });
  const end = el("input", {
    type: "date",
    value: state.end,
    onChange: (event) => {
      state.end = event.target.value;
      runsView.render(ctx);
    },
  });

  const utcNote = "Run dates and this filter use the manifest's UTC date; the "
    + "timestamps on the page are shown in " + timezoneSuffix() + ".";
  return [
    el("span", { class: "chip", title: utcNote }, [
      el("span", { class: "chip__label", text: "Run date" }),
      runSelect,
    ]),
    el("span", { class: "chip", title: utcNote }, [
      el("span", { class: "chip__label", text: "Inserted from (UTC)" }),
      start,
    ]),
    el("span", { class: "chip", title: utcNote }, [
      el("span", { class: "chip__label", text: "to" }),
      end,
    ]),
    state.start || state.end
      ? el("button", {
          class: "chip",
          text: "Clear range",
          onClick: () => {
            state.start = "";
            state.end = "";
            runsView.render(ctx);
          },
        })
      : null,
  ].filter(Boolean);
}

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

function sparkline(runs) {
  const values = runs
    .slice(0, 12)
    .reverse()
    .map((run) => run.counts.objects_created);
  if (values.length < 2) return null;
  const width = 220;
  const height = 34;
  const max = Math.max(...values, 1);
  const step = width / (values.length - 1);
  const points = values.map(
    (value, index) => `${index * step},${height - (value / max) * (height - 4) - 2}`
  );
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "sparkline");
  svg.setAttribute("width", String(width));
  svg.setAttribute("height", String(height));
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", `M${points.join(" L")}`);
  svg.appendChild(path);
  return el("div", {}, [
    svg,
    el("div", { class: "stat__note", text: "Objects created per run, last 12 runs" }),
  ]);
}

function runPage(ctx) {
  const summary = state.detail.summary;
  const counts = summary.counts;
  const unchanged = counts.sources_examined - counts.sources_processed;
  const runStatus =
    summary.status === "success"
      ? statusCue("good", "success")
      : summary.status === "partial_failure"
      ? statusCue("warning", "partial failure")
      : statusCue("critical", "failed");

  return el("div", {}, [
    el("div", { class: "page-title", text: summary.started_at.slice(0, 10) }),
    el("div", { class: "page-sub" }, [
      instant(summary.started_at),
      el("span", { text: " → " }),
      instant(summary.completed_at, { withDate: false }),
      el("span", { text: "  " }),
      runStatus,
      el("span", {
        text: `  ·  meeting dates ${summary.target_dates[0] || "—"}–${
          summary.target_dates[summary.target_dates.length - 1] || "—"
        }`,
      }),
    ]),
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
    el("div", { class: "secondary" }, [
      el("span", {
        text: `${counts.sources_processed} of ${counts.sources_examined} sources processed (${unchanged} unchanged)`,
      }),
      counts.candidates_rejected
        ? el("span", { text: `  ·  ${counts.candidates_rejected} candidates rejected` })
        : null,
    ]),
    sparkline(state.runs.runs),
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
    ...state.detail.groups.map((group) => groupBlock(group, ctx)),
  ]);
}

function groupBlock(group, ctx) {
  const open = state.expanded[group.bucket] !== false;
  const rows = el(
    "div",
    { class: "rows" },
    group.rows.length
      ? group.rows.map((row) =>
          group.bucket === "review_items_created" ? reviewRow(row) : objectRow(row, ctx)
        )
      : [el("div", { class: "empty", text: "Nothing in this bucket." })]
  );
  rows.hidden = !open;

  const toggle = el(
    "button",
    {
      class: "group-toggle",
      "aria-expanded": open ? "true" : "false",
      onClick: () => {
        state.expanded[group.bucket] = rows.hidden;
        rows.hidden = !rows.hidden;
        toggle.setAttribute("aria-expanded", rows.hidden ? "false" : "true");
      },
    },
    [
      icon("chevron"),
      el("span", { text: group.label }),
      el("span", { class: "group-toggle__count", text: String(group.count) }),
    ]
  );
  return el("section", {}, [toggle, rows]);
}

function objectRow(row, ctx) {
  const node = el(
    "div",
    {
      class: "row",
      onClick: () => (row.present ? openObjectPeek(row.id) : null),
    },
    [
      el("span", { class: "row__label", text: row.category || "missing" }),
      el("span", { class: "row__title", text: row.title || row.id }),
      el("span", { class: "row__id", text: row.id }),
      row.present ? null : statusCue("muted", "no longer present"),
      row.present
        ? el("button", {
            class: "row__open",
            text: "Open",
            onClick: (event) => {
              event.stopPropagation();
              openObjectPeek(row.id);
            },
          })
        : null,
    ].filter(Boolean)
  );

  if (row.bucket !== "objects_refined") return node;

  const refinement = row.refinement || {};
  const detail = el("div", { style: "padding:0 8px 10px" }, [
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
  return el("div", {}, [node, detail]);
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

function reviewRow(row) {
  return el(
    "div",
    { class: "row", onClick: () => (row.present ? openReviewPeek(row.id) : null) },
    [
      el("span", { class: "row__label", text: row.priority || "gone" }),
      el("span", { class: "row__title", text: row.title || row.id }),
      row.status
        ? badge(row.status, row.status === "pending" ? "overridden" : undefined)
        : null,
      el("span", { class: "row__id", text: row.id }),
      row.present
        ? el("button", {
            class: "row__open",
            text: "Open",
            onClick: (event) => {
              event.stopPropagation();
              openReviewPeek(row.id);
            },
          })
        : null,
    ].filter(Boolean)
  );
}
