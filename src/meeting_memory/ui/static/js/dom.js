// Tiny DOM helpers and the monochrome line-icon set.
//
// Everything is built with createElement rather than innerHTML so repository
// text — statements, notes, evidence lines — is inserted as text nodes and can
// never be interpreted as markup.

export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key === "html") node.innerHTML = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "value") node.value = value;
    else if (key === "checked" || key === "disabled" || key === "selected") {
      node[key] = Boolean(value);
    } else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(
      typeof child === "string" || typeof child === "number"
        ? document.createTextNode(String(child))
        : child
    );
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function mount(node, ...children) {
  clear(node);
  for (const child of children.flat()) {
    if (child) node.appendChild(child);
  }
  return node;
}

const PATHS = {
  chevron: "M4 6l4 4 4-4",
  chevronRight: "M6 4l4 4-4 4",
  check: "M3 8.5l3.2 3.2L13 5",
  alert: "M8 2.6L14.6 13H1.4L8 2.6z M8 6.6v3.1 M8 11.4v.1",
  close: "M4 4l8 8 M12 4l-8 8",
  plus: "M8 3v10 M3 8h10",
  trash: "M3 5h10 M6.5 5V3.5h3V5 M4.6 5l.6 8h5.6l.6-8",
  refresh: "M13 8a5 5 0 1 1-1.6-3.7 M13 2.5V5h-2.5",
  search: "M7.2 12a4.8 4.8 0 1 0 0-9.6 4.8 4.8 0 0 0 0 9.6z M10.8 10.8L14 14",
  user: "M8 8a2.6 2.6 0 1 0 0-5.2A2.6 2.6 0 0 0 8 8z M2.8 14c0-2.6 2.3-4.2 5.2-4.2s5.2 1.6 5.2 4.2",
  sun: "M8 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M8 1v1.6 M8 13.4V15 M1 8h1.6 M13.4 8H15 M3.1 3.1l1.1 1.1 M11.8 11.8l1.1 1.1 M12.9 3.1l-1.1 1.1 M4.2 11.8l-1.1 1.1",
  moon: "M13 9.6A5.6 5.6 0 0 1 6.4 3 5.6 5.6 0 1 0 13 9.6z",
  panel: "M2.5 3.5h11v9h-11z M6 3.5v9",
  expand: "M9.5 2.5H13.5V6.5 M6.5 13.5H2.5V9.5 M13.5 2.5L9 7 M2.5 13.5L7 9",
  spark: "M8 2l1.5 4.5L14 8l-4.5 1.5L8 14l-1.5-4.5L2 8l4.5-1.5z",
  doc: "M4 2h5l3 3v9H4z M9 2v3h3",
  merge: "M5 13V6.5a3 3 0 0 1 3-3h3 M8.5 1.5L11.5 3.5 8.5 5.5",
  history: "M8 4v4l2.6 1.6 M14 8A6 6 0 1 1 3.4 4.2 M3 2v2.6h2.6",
  layers: "M8 2l6 3-6 3-6-3z M2 8.5l6 3 6-3 M2 11.5l6 3 6-3",
  queue: "M2.5 4h11 M2.5 8h11 M2.5 12h7",
  dot: "M8 8m-2.4 0a2.4 2.4 0 1 0 4.8 0a2.4 2.4 0 1 0-4.8 0",
};

export function icon(name, className = "icon") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("class", className);
  svg.setAttribute("aria-hidden", "true");
  for (const segment of (PATHS[name] || PATHS.dot).split(" M")) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", segment.startsWith("M") ? segment : `M${segment}`);
    svg.appendChild(path);
  }
  return svg;
}

// Status cues always carry an icon and a word; colour alone never encodes them.
export function statusCue(role, label, iconName) {
  const names = { good: "check", warning: "alert", critical: "alert", muted: "dot" };
  return el("span", { class: `status status--${role}` }, [
    icon(iconName || names[role] || "dot"),
    el("span", { text: label }),
  ]);
}

export function badge(label, modifier) {
  return el("span", { class: `badge${modifier ? ` badge--${modifier}` : ""}`, text: label });
}

export function callout(kind, title, body, iconName) {
  return el("div", { class: `callout callout--${kind}` }, [
    icon(iconName || (kind === "danger" || kind === "warning" ? "alert" : "spark")),
    el("div", { class: "callout__body" }, [
      title ? el("div", { class: "callout__title", text: title }) : null,
      typeof body === "string" ? el("div", { text: body }) : body,
    ]),
  ]);
}

export function property(label, value) {
  return [
    el("div", { class: "props__label", text: label }),
    el("div", { class: "props__value" }, [
      typeof value === "string" || typeof value === "number"
        ? document.createTextNode(String(value))
        : value || el("span", { class: "muted", text: "None" }),
    ]),
  ];
}

export function diffSpans(segments) {
  return (segments || []).map((segment) =>
    el("span", {
      class:
        segment.kind === "added"
          ? "diff-add"
          : segment.kind === "removed"
          ? "diff-del"
          : "",
      text: segment.text,
    })
  );
}

// -- time -------------------------------------------------------------------
//
// Every timestamp in the repository is UTC. Rendering it in the browser's zone
// would show one reviewer 12:50 PM and another 8:50 PM for the same run, so all
// instants are rendered in the operating calendar's zone reported by the server
// and always carry its label. The exact stored UTC value stays available on
// hover, because that is the string that appears in the files themselves.

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

let displayZone = { label: "UTC", offsetMinutes: 0 };

export function setDisplayTimezone(zone) {
  if (!zone) return displayZone;
  displayZone = {
    label: zone.label || "UTC",
    offsetMinutes: Number(zone.offset_minutes) || 0,
  };
  return displayZone;
}

export function displayTimezone() {
  return displayZone;
}

export function timezoneSuffix() {
  const { label, offsetMinutes } = displayZone;
  const hours = offsetMinutes / 60;
  const offset = `UTC${hours >= 0 ? "+" : "-"}${Math.abs(hours)}`;
  return label === offset || label === "UTC" ? label : `${label} (${offset})`;
}

function shifted(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return new Date(parsed.getTime() + displayZone.offsetMinutes * 60000);
}

export function formatInstant(value, { withZone = true, withDate = true } = {}) {
  if (!value) return "";
  const moved = shifted(value);
  if (moved === null) return value;
  const hours = moved.getUTCHours();
  const minutes = String(moved.getUTCMinutes()).padStart(2, "0");
  const suffix = hours < 12 ? "AM" : "PM";
  const hour12 = hours % 12 === 0 ? 12 : hours % 12;
  const clock = `${hour12}:${minutes} ${suffix}`;
  const day = `${MONTHS[moved.getUTCMonth()]} ${String(moved.getUTCDate()).padStart(2, "0")}, ${moved.getUTCFullYear()}`;
  const text = withDate ? `${day}, ${clock}` : clock;
  return withZone ? `${text} ${displayZone.label}` : text;
}

export function localDate(value) {
  if (!value) return "";
  const moved = shifted(value);
  if (moved === null) return value;
  return moved.toISOString().slice(0, 10);
}

// A real <time> element: the label is readable, the machine value is exact, and
// hovering shows the UTC string as it is stored on disk.
export function instant(value, options = {}) {
  if (!value) return el("span", { class: "muted", text: "Unknown" });
  return el("time", {
    class: "instant",
    datetime: value,
    title: `${value} (stored UTC) · shown in ${timezoneSuffix()}`,
    text: formatInstant(value, options),
  });
}

// Calendar dates carry no time of day and are never shifted between zones.
export function calendarDate(value) {
  if (!value) return el("span", { class: "muted", text: "Unknown" });
  return el("span", {
    class: "instant",
    title: "calendar date; not a timestamp, so no time zone applies",
    text: value,
  });
}
