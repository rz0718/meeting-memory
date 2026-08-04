// Shared review rendering: the side-by-side compare, the AI suggestion as a
// comment thread, and the blocked-state callouts. Used read-only by the side
// peek and with the action form by the queue.

import { badge, calendarDate, el, icon, instant, property, statusCue } from "./dom.js";
import { evidenceList } from "./evidence.js";

export function priorityCue(priority) {
  if (priority === "conflict") return statusCue("critical", "conflict", "alert");
  if (priority === "linked") return statusCue("warning", "linked", "merge");
  return statusCue("muted", "unlinked", "dot");
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

export function reviewHeader(detail) {
  return el("div", {}, [
    el("div", { class: "page-title", text: detail.title }),
    el("div", { class: "page-sub" }, [
      priorityCue(detail.priority),
      el("span", { text: `  ·  ${detail.reason}  ·  ${detail.category}  ·  ` }),
      el("span", { text: "opened " }),
      instant(detail.created_at),
      el("span", { text: "  ·  " }),
      el("span", { class: "mono secondary", text: detail.id }),
    ]),
  ]);
}

export function comparePanel(detail) {
  return el("div", {}, [
    el("div", { class: "compare" }, [
      el("div", { class: "compare__col" }, [
        el("div", { class: "compare__head", text: "Existing" }),
        detail.existing_statement
          ? el("div", { class: "compare__text" }, diffSegments(detail.statement_diff.left))
          : el("div", { class: "muted", text: "No single existing statement was identified." }),
        el("div", { style: "margin-top:10px" }, [evidenceList(detail.existing_evidence)]),
      ]),
      el("div", { class: "compare__col" }, [
        el("div", { class: "compare__head", text: "Candidate" }),
        el("div", { class: "compare__text" }, diffSegments(detail.statement_diff.right)),
        el("div", { style: "margin-top:10px" }, [
          evidenceList(detail.candidate_evidence, { open: true }),
        ]),
      ]),
    ]),
    el("h2", { class: "section", text: "Why review is required" }),
    el("div", { text: detail.explanation }),
  ]);
}

export function candidateProperties(detail) {
  const metadata = detail.candidate_metadata;
  if (!metadata) {
    return el("div", { class: "muted", text: "This review carries no structured candidate." });
  }
  return el("div", { class: "props" }, [
    ...property("Candidate title", metadata.title),
    ...property("Status", metadata.status),
    ...property("Owner", metadata.owner),
    ...property("Confidence", metadata.confidence),
    ...property("Effective date", calendarDate(metadata.effective_date)),
    ...property("Reason for durability", metadata.reason_for_durability),
    ...property(
      "Sources",
      el("span", { class: "mono", text: detail.sources.join(", ") })
    ),
  ]);
}

export function suggestionComment(suggestion) {
  if (!suggestion) {
    return el("div", { class: "callout" }, [
      icon("spark"),
      el("div", { class: "callout__body" }, [
        el("div", { class: "callout__title", text: "No current AI suggestion" }),
        el("div", {
          text:
            "Generate one to enable accept, or choose an action yourself — the " +
            "resolver records either way.",
        }),
      ]),
    ]);
  }
  const recommendation = suggestion.recommendation;
  const toggle = (label, values) => {
    if (!values || !values.length) return null;
    const list = el(
      "ul",
      { class: "list-tight" },
      values.map((value) => el("li", { text: value }))
    );
    list.hidden = true;
    const head = el(
      "button",
      {
        class: "group-toggle",
        "aria-expanded": "false",
        style: "font-size:14px;font-weight:500",
        onClick: () => {
          list.hidden = !list.hidden;
          head.setAttribute("aria-expanded", list.hidden ? "false" : "true");
        },
      },
      [
        icon("chevron"),
        el("span", { text: label }),
        el("span", { class: "group-toggle__count", text: String(values.length) }),
      ]
    );
    head.firstChild.style.transform = "rotate(-90deg)";
    head.addEventListener("click", () => {
      head.firstChild.style.transform = list.hidden ? "rotate(-90deg)" : "";
    });
    return el("div", {}, [head, list]);
  };

  return el("div", { class: "comment" }, [
    el("div", { class: "comment__avatar" }, [icon("spark")]),
    el("div", { class: "comment__body" }, [
      el("div", { class: "comment__author" }, [
        el("span", { text: "AI reviewer" }),
        badge(recommendation.suggested_action || "human required"),
        badge(`confidence ${recommendation.confidence}`),
        suggestion.current
          ? statusCue("good", "current")
          : statusCue("critical", "stale"),
        recommendation.requires_human ? statusCue("warning", "requires human") : null,
        el("span", { class: "comment__meta" }, [
          el("span", { text: `${suggestion.model} · ` }),
          instant(suggestion.generated_at),
          el("span", { text: ` · ${suggestion.id}` }),
        ]),
      ].filter(Boolean)),
      el("div", { class: "comment__text", text: recommendation.rationale }),
      toggle("Material differences", recommendation.material_differences),
      toggle("Risks and blockers", recommendation.risks),
      toggle(
        "Evidence findings",
        (recommendation.evidence_findings || []).map(
          (finding) =>
            `${finding.source}:${finding.line_start}-${finding.line_end} — ${finding.finding}`
        )
      ),
      recommendation.proposed_knowledge
        ? el("div", { class: "props" }, [
            ...property("Proposed title", recommendation.proposed_knowledge.title),
            ...property("Proposed status", recommendation.proposed_knowledge.status),
            ...property("Proposed owner", recommendation.proposed_knowledge.owner),
            ...property("Proposed confidence", recommendation.proposed_knowledge.confidence),
            ...property(
              "Proposed effective date",
              recommendation.proposed_knowledge.effective_date
            ),
          ])
        : null,
      recommendation.existing_id
        ? el("div", { class: "secondary" }, [
            el("span", { text: "Target: " }),
            el("span", { class: "mono", text: recommendation.existing_id }),
          ])
        : null,
      recommendation.duplicate_of
        ? el("div", { class: "secondary" }, [
            el("span", { text: "Duplicate of: " }),
            el("span", { class: "mono", text: recommendation.duplicate_of }),
          ])
        : null,
      recommendation.new_id
        ? el("div", { class: "secondary" }, [
            el("span", { text: "Proposed new ID: " }),
            el("span", { class: "mono", text: recommendation.new_id }),
          ])
        : null,
    ]),
  ]);
}

export function resolutionPanel(detail) {
  const resolution = detail.resolution;
  if (!resolution) return null;
  return el("div", {}, [
    el("h2", { class: "section", text: "Resolution" }),
    el("div", { class: "props" }, [
      ...property("Action", resolution.action),
      ...property("Reviewer", resolution.reviewer),
      ...property("Resolved at", instant(resolution.resolved_at)),
      ...property(
        "Affected objects",
        el("span", { class: "mono", text: resolution.affected_object_ids.join(", ") })
      ),
      ...property("Suggested action", resolution.suggested_action),
      ...property(
        "Suggestion disposition",
        badge(
          resolution.suggestion_disposition,
          resolution.suggestion_disposition === "accepted"
            ? "accepted"
            : resolution.suggestion_disposition === "overridden"
            ? "overridden"
            : undefined
        )
      ),
      ...property("Resolution mode", resolution.resolution_mode),
      ...property(
        "Allowed stale evidence",
        resolution.allowed_stale_evidence ? "yes" : "no"
      ),
      ...property("Note", resolution.note),
    ]),
  ]);
}
