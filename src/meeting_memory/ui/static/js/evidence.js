// Evidence excerpts as Notion toggle blocks, with real 1-based line numbers
// read from the source file. Raw meeting and Slack notes are shown here and
// are never editable or writable from this UI.

import { el, icon, statusCue } from "./dom.js";

export function freshnessCue(label) {
  if (label === "drifted") return statusCue("critical", "drifted");
  if (label === "unavailable") return statusCue("critical", "unavailable");
  if (label === "re-verified") return statusCue("warning", "re-verified");
  return statusCue("good", "current");
}

function excerptLines(excerpt) {
  const text = excerpt.text || "";
  const lines = text.split("\n");
  return el(
    "div",
    { class: "excerpt" },
    lines.map((line, index) =>
      el("div", { class: "excerpt__line" }, [
        el("span", { class: "excerpt__no", text: String(excerpt.line_start + index) }),
        el("span", { text: line }),
      ])
    )
  );
}

export function evidenceBlock(entry, { open = false } = {}) {
  const excerpt = entry.excerpt || {};
  const body = excerpt.error
    ? el("div", { class: "excerpt" }, [
        el("div", { class: "excerpt__line" }, [
          el("span", { class: "excerpt__no", text: "—" }),
          el("span", { text: `Unavailable: ${excerpt.error}` }),
        ]),
      ])
    : excerptLines(excerpt);
  body.hidden = !open;

  const head = el(
    "button",
    {
      class: "evidence__head",
      "aria-expanded": open ? "true" : "false",
      onClick: () => {
        body.hidden = !body.hidden;
        head.setAttribute("aria-expanded", body.hidden ? "false" : "true");
        head.firstChild.style.transform = body.hidden ? "rotate(-90deg)" : "";
      },
    },
    [
      icon("chevron"),
      el("span", {
        class: "evidence__path",
        text: `${entry.source}:${entry.line_start}-${entry.line_end}`,
      }),
      freshnessCue(entry.freshness_label),
    ]
  );
  if (!open) head.firstChild.style.transform = "rotate(-90deg)";
  return el("div", { class: "evidence" }, [head, body]);
}

export function evidenceList(entries, options) {
  if (!entries || !entries.length) {
    return el("div", { class: "secondary muted", text: "No evidence recorded." });
  }
  return el("div", {}, entries.map((entry) => evidenceBlock(entry, options)));
}
