// Read-only review side peek, opened from Tab 1 rows and from quick find.
// Decisions are never taken here: a pending case links across to the queue,
// where the dry-run gate lives.

import { api } from "./api.js";
import { el, icon } from "./dom.js";
import {
  candidateProperties,
  comparePanel,
  resolutionPanel,
  reviewHeader,
  suggestionComment,
} from "./reviewview.js";
import { busy, closePeek, openPeek, reportError } from "./ui.js";

let openQueueItem = null;

export function bindQueueNavigation(handler) {
  openQueueItem = handler;
}

export async function openReviewPeek(reviewId) {
  openPeek(reviewId, busy("Loading review case…"));
  try {
    const detail = await api.review(reviewId);
    openPeek(
      detail.title,
      el("div", {}, [
        reviewHeader(detail),
        detail.status === "pending" && openQueueItem
          ? el("div", { class: "actionbar", style: "border:none;margin:0;padding:0 0 8px" }, [
              el("button", {
                class: "btn btn--primary",
                onClick: () => {
                  closePeek();
                  openQueueItem(detail.id);
                },
              }, [icon("queue"), el("span", { text: "Work this case in the review queue" })]),
            ])
          : null,
        comparePanel(detail),
        el("h2", { class: "section", text: "Candidate" }),
        candidateProperties(detail),
        el("h2", { class: "section", text: "AI suggestion" }),
        suggestionComment(detail.suggestion),
        resolutionPanel(detail),
      ])
    );
  } catch (error) {
    reportError(error);
  }
}
