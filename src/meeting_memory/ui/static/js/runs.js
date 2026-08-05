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
import { chartSeries, pointX, yTicks } from "./runs_chart.js";
import { busy, empty, reportError } from "./ui.js";
import { emit } from "./store.js";

const state = {
  runs: null,
  runId: null,
  detail: null,
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

  const utcNote = "Run dates use the manifest's UTC date; the timestamps on the "
    + "page are shown in " + timezoneSuffix() + ".";
  return el("span", { class: "chip", title: utcNote }, [
    el("span", { class: "chip__label", text: "Run date" }),
    runSelect,
  ]);
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

const SVG_NS = "http://www.w3.org/2000/svg";

function svgElement(tag, attributes = {}, children = []) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attributes)) {
    node.setAttribute(name, String(value));
  }
  for (const child of [].concat(children)) node.appendChild(child);
  return node;
}

function drawKnowledgeChart(svg, series, width) {
  const height = 260;
  const margins = { top: 30, right: 18, bottom: 50, left: 46 };
  const plotWidth = width - margins.left - margins.right;
  const plotHeight = height - margins.top - margins.bottom;
  const plotBottom = margins.top + plotHeight;
  const ticks = yTicks(series.map((entry) => entry.count));
  const maximum = ticks[ticks.length - 1];
  const points = series.map((entry, index) => ({
    ...entry,
    x: pointX(index, series.length, margins.left, plotWidth),
    y: margins.top + (1 - entry.count / maximum) * plotHeight,
  }));

  svg.replaceChildren();
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.appendChild(
    svgElement("title", {}, [
      document.createTextNode("Knowledge objects created in each of the last 12 runs"),
    ])
  );

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
    const label = `${point.date}: ${point.count} knowledge object${
      point.count === 1 ? "" : "s"
    } created`;
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
    count.textContent = String(point.count);
    svg.appendChild(count);

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

function knowledgeChart(runs) {
  const series = chartSeries(runs);
  const body = series.length
    ? (() => {
        const svg = svgElement("svg", {
          class: "knowledge-chart__svg",
          height: 260,
          role: "img",
          "aria-label": "Knowledge objects created per run",
          preserveAspectRatio: "none",
        });
        drawKnowledgeChart(svg, series, 960);

        const redraw = (measuredWidth) => {
          if (measuredWidth > 0) {
            drawKnowledgeChart(svg, series, Math.max(320, measuredWidth));
          }
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
        return svg;
      })()
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
          text: "Knowledge objects created",
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
    counts.candidates_rejected
      ? el("div", {
          class: "source-summary__note",
          text: `${counts.candidates_rejected} candidates rejected during this run.`,
        })
      : null,
  ]);
}

function runPage(ctx) {
  const summary = state.detail.summary;
  const counts = summary.counts;
  const unchanged =
    counts.sources_skipped ?? counts.sources_examined - counts.sources_processed;
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
    sourceSummary(counts, unchanged),
    knowledgeChart(state.runs.runs),
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
