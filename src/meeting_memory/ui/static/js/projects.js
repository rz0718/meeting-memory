// The project scope editor.
//
// A project is built from the sources the index actually cites, never from a
// typed glob: the list below is the observed source universe, so a scope cannot
// name a file that does not exist. Selecting a meeting contributes its filename
// stem; selecting a Slack note contributes its channel ID. Those are the two
// kinds of selector the saved scope holds.
//
// Meeting selectors match by substring, which is what makes a project key like
// "CCI" useful and also what makes it dangerous to hide: a selection can admit
// sources nobody clicked. Every resolution here is computed by the server and
// shows both numbers — what matched, and how much of it arrived through fuzzy
// matching rather than a click.

import { api } from "./api.js";
import { badge, el, icon, mount, statusCue } from "./dom.js";
import { closeModal, modalBody, modalFoot, openModal, reportError, toast } from "./ui.js";

const editor = {
  name: "",
  meetings: [],
  slack: [],
  universe: null,
  resolution: null,
  existing: null, // the saved project being edited, if any
  pending: false,
  onSaved: null,
};

let previewTimer = null;

export async function openScopeEditor({ project = null, onSaved = null } = {}) {
  editor.name = project ? project.name : "";
  editor.meetings = project ? [...project.meeting_names] : [];
  editor.slack = project ? [...project.slack_names] : [];
  editor.existing = project;
  editor.resolution = null;
  editor.onSaved = onSaved;

  openModal({
    title: project ? `Edit project “${project.name}”` : "New project scope",
    body: el("div", { class: "secondary", text: "Loading the source universe…" }),
    actions: [],
  });
  try {
    editor.universe = (await api.projects()).universe;
  } catch (error) {
    reportError(error);
    closeModal();
    return;
  }
  render();
  schedulePreview();
}

function render() {
  mount(modalBody(), editorBody());
  mount(modalFoot(), editorActions());
}

// -- selection --------------------------------------------------------------

function fold(value) {
  return String(value).trim().toLowerCase();
}

function isSelected(entry) {
  const list = entry.kind === "slack" ? editor.slack : editor.meetings;
  return list.some((value) => fold(value) === fold(entry.selector));
}

function toggle(entry) {
  const key = entry.kind === "slack" ? "slack" : "meetings";
  const list = editor[key];
  const index = list.findIndex((value) => fold(value) === fold(entry.selector));
  if (index >= 0) list.splice(index, 1);
  else list.push(entry.selector);
  render();
  schedulePreview();
}

function removeSelector(kind, value) {
  const list = kind === "slack" ? editor.slack : editor.meetings;
  const index = list.findIndex((item) => fold(item) === fold(value));
  if (index >= 0) list.splice(index, 1);
  render();
  schedulePreview();
}

function addTypedSelector(kind, value) {
  const text = value.trim();
  if (!text) return;
  const list = kind === "slack" ? editor.slack : editor.meetings;
  if (!list.some((item) => fold(item) === fold(text))) list.push(text);
  render();
  schedulePreview();
}

// Resolution is a read, so it can follow every keystroke — but it is a request,
// so it does not follow each one immediately.
function schedulePreview() {
  window.clearTimeout(previewTimer);
  previewTimer = window.setTimeout(preview, 180);
}

async function preview() {
  editor.pending = true;
  try {
    editor.resolution = await api.projectPreview({
      name: editor.name,
      meeting_names: editor.meetings,
      slack_names: editor.slack,
    });
  } catch (error) {
    editor.resolution = null;
    reportError(error);
  }
  editor.pending = false;
  render();
}

// -- rendering --------------------------------------------------------------

function editorBody() {
  return el("div", { class: "scope-editor" }, [
    el("div", { class: "scope-editor__name" }, [
      el("label", { class: "props__label", text: "Project name" }),
      el("input", {
        value: editor.name,
        placeholder: "CCI",
        // The name is not part of the resolution, so typing it does not
        // re-resolve the selection.
        onInput: (event) => {
          editor.name = event.target.value;
        },
      }),
    ]),
    selectorStrip(),
    resolutionLine(),
    sourceList(),
  ]);
}

function selectorStrip() {
  const chips = [
    ...editor.meetings.map((value) => selectorChip("meeting", value)),
    ...editor.slack.map((value) => selectorChip("slack", value)),
  ];
  return el("div", { class: "scope-editor__selectors" }, [
    el("div", { class: "props__label", text: "Selectors" }),
    chips.length
      ? el("div", { class: "chips" }, chips)
      : el("div", { class: "muted", text: "Nothing selected yet." }),
    typedInput(),
  ]);
}

function selectorChip(kind, value) {
  const field = kind === "slack" ? "slack_names" : "meeting_names";
  const unmatched = (
    editor.resolution ? editor.resolution.unmatched_selectors[field] : []
  ).some((item) => fold(item) === fold(value));
  return el(
    "span",
    {
      class: `cite${unmatched ? " cite--broken" : ""}`,
      title: unmatched
        ? "matches no source in the index"
        : kind === "slack"
        ? "Slack channel — matched exactly"
        : "meeting filename — matched anywhere in the name",
    },
    [
      icon(kind === "slack" ? "layers" : "doc"),
      el("span", { class: kind === "slack" ? "mono" : "", text: value }),
      el("button", {
        class: "cite__remove",
        title: "Remove this selector",
        onClick: () => removeSelector(kind, value),
      }, [icon("close")]),
    ]
  );
}

function typedInput() {
  const input = el("input", {
    placeholder: "Add a name by hand (e.g. CCI)…",
    onKeydown: (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      addTypedSelector(select.value, input.value);
      input.value = "";
    },
  });
  const select = el("select", {}, [
    el("option", { value: "meeting", text: "Meeting" }),
    el("option", { value: "slack", text: "Slack channel" }),
  ]);
  return el("div", { class: "scope-editor__typed" }, [
    select,
    input,
    el("button", {
      class: "btn",
      text: "Add",
      onClick: () => {
        addTypedSelector(select.value, input.value);
        input.value = "";
      },
    }),
  ]);
}

function resolutionLine() {
  if (!editor.resolution) {
    return el("div", { class: "secondary muted", text: "Resolving…" });
  }
  const value = editor.resolution;
  const unmatched = [
    ...value.unmatched_selectors.meeting_names,
    ...value.unmatched_selectors.slack_names,
  ];
  return el("div", {}, [
    el("div", { class: "ask__receipt secondary" }, [
      el("span", {
        text: `${value.sources_matched} of ${value.sources_total} sources`,
      }),
      el("span", { text: `${value.objects} of ${value.objects_total} knowledge objects` }),
      el("span", { text: `${value.evidence_in_scope} evidence items` }),
      editor.pending ? el("span", { class: "spinner" }) : null,
    ]),
    value.sources_by_fuzzy_match
      ? el("div", { class: "secondary" }, [
          statusCue(
            "warning",
            `${value.sources_by_fuzzy_match} source${
              value.sources_by_fuzzy_match === 1 ? "" : "s"
            } matched by name, not by selection`
          ),
        ])
      : null,
    unmatched.length
      ? el("div", { class: "secondary" }, [
          statusCue("warning", `${unmatched.join(", ")} matches no source`),
        ])
      : null,
    value.objects === 0
      ? el("div", { class: "secondary" }, [
          statusCue("critical", "this scope retrieves nothing"),
        ])
      : null,
  ]);
}

function sourceList() {
  const sources = editor.universe.sources;
  if (!sources.length) {
    return el("div", { class: "empty", text: "No knowledge object cites a source yet." });
  }
  const matched = new Map(
    (editor.resolution ? editor.resolution.sources : []).map((entry) => [
      entry.source,
      entry,
    ])
  );
  const groups = new Map();
  for (const entry of sources) {
    const key = entry.date || "undated";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(entry);
  }
  return el(
    "div",
    { class: "scope-sources" },
    [...groups.entries()].map(([date, entries]) =>
      el("div", { class: "scope-sources__group" }, [
        el("div", { class: "scope-sources__date", text: date }),
        ...entries.map((entry) => sourceRow(entry, matched.get(entry.source))),
      ])
    )
  );
}

function sourceRow(entry, match) {
  const selected = isSelected(entry);
  // Admitted without being clicked: a fuzzy meeting selector pulled it in, so
  // the row says which selector did it. Unticking it here would be a lie —
  // the selector, not the source, is what the scope holds.
  const viaFuzzy = Boolean(match) && !selected;
  const box = el("input", {
    type: "checkbox",
    checked: selected,
    onChange: () => toggle(entry),
  });
  // In scope, but not because anyone picked it: a half-state, and drawn as one
  // rather than as an empty box beside a highlighted row.
  box.indeterminate = viaFuzzy;
  return el(
    "label",
    {
      class: `scope-source${match ? " is-matched" : ""}`,
      title: viaFuzzy
        ? `${entry.source} — in scope through another selector; tick to select `
          + "it in its own right"
        : entry.source,
    },
    [
      box,
      icon(entry.kind === "slack" ? "layers" : "doc"),
      el("span", {
        class: `scope-source__name${entry.kind === "slack" ? " mono" : ""}`,
        text: entry.name,
      }),
      el("span", {
        class: "muted",
        style: "font-size:12px",
        text: `${entry.objects} object${entry.objects === 1 ? "" : "s"}`,
      }),
      viaFuzzy ? badge(`via “${(match.matched_by || []).join(", ")}”`) : null,
    ]
  );
}

function editorActions() {
  const replacing = Boolean(editor.existing);
  return [
    el("button", { class: "btn", text: "Cancel", onClick: closeModal }),
    el(
      "button",
      {
        class: "btn btn--primary",
        text: replacing ? "Replace project" : "Save project",
        onClick: save,
      },
      []
    ),
  ];
}

async function save() {
  try {
    const saved = await api.projectSave({
      name: editor.name,
      meeting_names: editor.meetings,
      slack_names: editor.slack,
      replace: Boolean(editor.existing),
    });
    closeModal();
    toast(
      `Project ${saved.project.name} saved — ${saved.resolution.objects} objects in scope.`,
      { kind: "good" }
    );
    if (editor.onSaved) editor.onSaved(saved.project);
  } catch (error) {
    reportError(error);
  }
}
