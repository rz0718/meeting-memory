// Application shell: sidebar, view tabs, theme, reviewer, quick find, and the
// keyboard map. Views own their own filter bar and content.

import { api } from "./api.js";
import { askView } from "./ask.js";
import { el, icon, mount, setDisplayTimezone, timezoneSuffix } from "./dom.js";
import { basketSummary, openBasketDialog, openObjectPeek } from "./objects.js";
import { bindQueueNavigation, openReviewPeek } from "./reviewpeek.js";
import { handleQueueKey, queueView, selectReview } from "./queue.js";
import { handleRunsKey, runsView } from "./runs.js";
import {
  onChange,
  refreshBasket,
  refreshKnowledge,
  refreshSession,
  store,
} from "./store.js";
import { closeModal, closePeek, openModal, peekIsOpen, reportError, toast } from "./ui.js";

const VIEWS = [runsView, queueView, askView];
let activeView = runsView;

const nodes = {};

function ctx() {
  return {
    content: nodes.content,
    filters: nodes.filters,
  };
}

async function activate(view) {
  activeView = view;
  nodes.content.style.padding = "";
  renderTabs();
  renderSidebar();
  nodes.title.textContent = view.title;
  await view.render(ctx());
  renderTabs();
  renderSidebar();
}

function renderTabs() {
  mount(
    nodes.tabs,
    VIEWS.map((view) => {
      const count = view.count ? view.count() : null;
      return el(
        "button",
        {
          class: `view-tab${view === activeView ? " is-active" : ""}`,
          onClick: () => activate(view),
        },
        [
          el("span", { text: view.label }),
          count === null || count === undefined
            ? null
            : el("span", { class: "view-tab__count", text: String(count) }),
        ].filter(Boolean)
      );
    })
  );
}

function renderSidebar() {
  const item = (view) =>
    el(
      "button",
      {
        class: `nav-item${view === activeView ? " is-active" : ""}`,
        onClick: () => activate(view),
      },
      [
        icon(view.icon),
        el("span", { text: view.label }),
        view.count && view.count() !== null
          ? el("span", { class: "nav-item__count", text: String(view.count()) })
          : null,
      ].filter(Boolean)
    );

  mount(nodes.nav, [
    ...VIEWS.map(item),
    el("div", { class: "sidebar__section", text: "Workspace" }),
    el(
      "button",
      {
        class: "nav-item",
        onClick: () => {
          queueViewResolved();
        },
      },
      [icon("history"), el("span", { text: "Resolved history" })]
    ),
    basketSummary(store.basket, openBasketDialog),
    el("button", { class: "nav-item", onClick: openSettings }, [
      icon("user"),
      el("span", { text: "Settings" }),
    ]),
  ]);

  const session = store.session;
  mount(
    nodes.foot,
    el("div", {}, [
      el("div", { text: session ? `Reviewer: ${session.reviewer}` : "" }),
      el("div", {
        class: "muted mono",
        style: "font-size:12px;word-break:break-all",
        text: session ? session.root : "",
      }),
      el("div", {
        class: "muted",
        style: "font-size:12px;margin-top:4px",
        title: "Stored timestamps are UTC; every time in this UI is converted to this zone.",
        text: session ? `Times in ${timezoneSuffix()}` : "",
      }),
    ])
  );
}

function queueViewResolved() {
  activate(queueView).then(() => {
    const select = nodes.filters.querySelector("select");
    if (select) {
      select.value = "resolved";
      select.dispatchEvent(new Event("change"));
    }
  });
}

// -- reviewer and settings --------------------------------------------------

function openSettings() {
  const session = store.session || {};
  const reviewerInput = el("input", { value: session.reviewer || "" });
  openModal({
    title: "Settings",
    body: el("div", { class: "props" }, [
      el("div", { class: "props__label", text: "Reviewer" }),
      el("div", { class: "props__value" }, [reviewerInput]),
      el("div", { class: "props__label", text: "Review model" }),
      el("div", { class: "props__value" }, [
        el("span", {
          class: session.review_model_configured ? "mono" : "muted",
          text: session.review_model || "not configured — suggestion generation is unavailable",
        }),
      ]),
      el("div", { class: "props__label", text: "Ask model" }),
      el("div", { class: "props__value" }, [
        el("span", {
          class: session.ask_model_configured ? "mono" : "muted",
          text: session.ask_model || "not configured — the Ask tab can retrieve but not answer",
        }),
      ]),
      el("div", { class: "props__label", text: "Time zone" }),
      el("div", { class: "props__value" }, [
        el("div", { text: timezoneSuffix() }),
        el("div", {
          class: "muted",
          style: "font-size:13px",
          text:
            "All timestamps are stored as UTC and displayed in this zone, set by "
            + "MEETING_TZ_UTC_OFFSET_HOURS on the server. Hover any time to see "
            + "the stored UTC value.",
        }),
      ]),
      el("div", { class: "props__label", text: "Repository" }),
      el("div", { class: "props__value" }, [
        el("span", { class: "mono", text: session.root || "" }),
      ]),
      el("div", { class: "props__label", text: "Meeting notes" }),
      el("div", { class: "props__value" }, [
        el("span", { class: "mono", text: session.meetings_dir || "" }),
      ]),
      el("div", { class: "props__label", text: "Theme" }),
      el("div", { class: "props__value" }, [
        el(
          "select",
          {
            onChange: (event) => setTheme(event.target.value),
          },
          ["system", "light", "dark"].map((value) =>
            el("option", {
              value,
              text: value,
              selected: (localStorage.getItem("mm-theme") || "system") === value,
            })
          )
        ),
      ]),
    ]),
    actions: [
      el("button", { class: "btn", text: "Close", onClick: closeModal }),
      el("button", { class: "btn btn--primary", text: "Save reviewer" , onClick: async () => {
        try {
          await api.setReviewer(reviewerInput.value);
          await refreshSession();
          renderSidebar();
          renderReviewerChip();
          closeModal();
          toast(`Reviewer set to ${store.session.reviewer}.`, { kind: "good" });
        } catch (error) {
          reportError(error);
        }
      }}),
    ],
  });
}

function renderReviewerChip() {
  mount(nodes.reviewerChip, [
    icon("user"),
    el("span", { text: store.session ? store.session.reviewer : "…" }),
  ]);
}

// -- theme ------------------------------------------------------------------

function setTheme(value) {
  localStorage.setItem("mm-theme", value);
  if (value === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", value);
  renderThemeButton();
}

function currentTheme() {
  return localStorage.getItem("mm-theme") || "system";
}

function renderThemeButton() {
  const dark =
    currentTheme() === "dark" ||
    (currentTheme() === "system" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches);
  mount(nodes.themeButton, [icon(dark ? "sun" : "moon")]);
}

// -- quick find -------------------------------------------------------------

const finder = { items: [], active: 0 };

async function openFinder() {
  const dialog = document.getElementById("finder");
  const input = document.getElementById("finder-input");
  dialog.classList.add("is-open");
  input.value = "";
  input.focus();
  if (!store.knowledge.length) await refreshKnowledge();
  const reviews = await api.reviews({ status: "all", limit: 400 });
  finder.items = [
    ...store.knowledge.map((value) => ({
      kind: "object",
      id: value.id,
      title: value.title,
      hint: value.category,
    })),
    ...reviews.reviews.map((value) => ({
      kind: "review",
      id: value.id,
      title: value.title,
      hint: `${value.priority} · ${value.status}`,
    })),
  ];
  renderFinder("");
}

function closeFinder() {
  document.getElementById("finder").classList.remove("is-open");
}

function renderFinder(needle) {
  const results = document.getElementById("finder-results");
  const folded = needle.trim().toLowerCase();
  const matches = finder.items
    .filter(
      (item) =>
        !folded ||
        item.id.toLowerCase().includes(folded) ||
        item.title.toLowerCase().includes(folded)
    )
    .slice(0, 60);
  finder.matches = matches;
  finder.active = Math.min(finder.active, Math.max(matches.length - 1, 0));
  mount(
    results,
    matches.length
      ? matches.map((item, index) =>
          el(
            "button",
            {
              class: `finder__item${index === finder.active ? " is-active" : ""}`,
              onClick: () => chooseFinderItem(item),
            },
            [
              icon(item.kind === "object" ? "doc" : "queue"),
              el("span", { text: item.title }),
              el("span", { class: "muted", text: item.hint }),
              el("span", { class: "finder__item-id", text: item.id }),
            ]
          )
        )
      : [el("div", { class: "empty", text: "Nothing matched." })]
  );
}

function chooseFinderItem(item) {
  closeFinder();
  if (item.kind === "object") openObjectPeek(item.id);
  else openReviewPeek(item.id);
}

// -- keyboard ---------------------------------------------------------------

function isTyping(event) {
  const tag = (event.target.tagName || "").toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select";
}

function bindKeys() {
  document.addEventListener("keydown", (event) => {
    const finderOpen = document.getElementById("finder").classList.contains("is-open");
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      finderOpen ? closeFinder() : openFinder();
      return;
    }
    if (finderOpen) {
      const matches = finder.matches || [];
      if (event.key === "ArrowDown") {
        event.preventDefault();
        finder.active = Math.min(finder.active + 1, matches.length - 1);
        renderFinder(document.getElementById("finder-input").value);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        finder.active = Math.max(finder.active - 1, 0);
        renderFinder(document.getElementById("finder-input").value);
      } else if (event.key === "Enter" && matches[finder.active]) {
        event.preventDefault();
        chooseFinderItem(matches[finder.active]);
      } else if (event.key === "Escape") {
        closeFinder();
      }
      return;
    }
    if (event.key === "Escape") {
      if (peekIsOpen()) closePeek();
      else closeModal();
      return;
    }
    if (isTyping(event)) return;
    if (event.key === "1") return void activate(runsView);
    if (event.key === "2") return void activate(queueView);
    if (event.key === "3") return void activate(askView);
    if (activeView === queueView && handleQueueKey(event)) event.preventDefault();
    if (activeView === runsView && handleRunsKey(event)) event.preventDefault();
  });
}

// -- boot -------------------------------------------------------------------

async function boot() {
  Object.assign(nodes, {
    app: document.getElementById("app"),
    nav: document.getElementById("sidebar-nav"),
    foot: document.getElementById("sidebar-foot"),
    tabs: document.getElementById("view-tabs"),
    filters: document.getElementById("filter-bar"),
    content: document.getElementById("content"),
    title: document.getElementById("page-title"),
    reviewerChip: document.getElementById("reviewer-chip"),
    themeButton: document.getElementById("toggle-theme"),
  });

  mount(document.getElementById("toggle-sidebar"), [icon("panel")]);
  mount(document.getElementById("open-finder"), [icon("search")]);
  mount(document.getElementById("peek-close"), [icon("close")]);
  mount(document.getElementById("peek-expand"), [icon("expand")]);

  document.getElementById("toggle-sidebar").addEventListener("click", () => {
    nodes.app.classList.toggle("sidebar-collapsed");
  });
  document.getElementById("open-finder").addEventListener("click", openFinder);
  document.getElementById("peek-close").addEventListener("click", closePeek);
  document.getElementById("scrim").addEventListener("click", closePeek);
  document.getElementById("peek-expand").addEventListener("click", () => {
    document.getElementById("peek").classList.toggle("is-full");
  });
  document.getElementById("finder-input").addEventListener("input", (event) => {
    finder.active = 0;
    renderFinder(event.target.value);
  });
  nodes.reviewerChip.addEventListener("click", openSettings);
  nodes.themeButton.addEventListener("click", () => {
    setTheme(currentTheme() === "dark" ? "light" : "dark");
  });

  setTheme(currentTheme());
  bindKeys();
  bindQueueNavigation((reviewId) => {
    activate(queueView).then(() => selectReview(reviewId));
  });
  // Counts in the tabs and sidebar follow the server's confirmed state, so a
  // view that reloads on its own (after an apply, a filter change, or a run
  // switch) refreshes the chrome without the shell polling for it.
  onChange(() => {
    renderTabs();
    renderSidebar();
    renderReviewerChip();
  });

  try {
    const session = await refreshSession();
    setDisplayTimezone(session.timezone);
    await refreshKnowledge();
    await refreshBasket();
  } catch (error) {
    reportError(error);
  }
  renderReviewerChip();
  await activate(runsView);
}

boot();
