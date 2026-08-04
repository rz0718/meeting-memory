// Shared chrome: toasts, the modal, and the Notion-style side peek.
//
// The toast reports what was written and links to the resulting record. There
// is deliberately no undo action on it: reversing an audited decision is a new
// audited decision, not a client-side rollback.

import { clear, el, icon, mount } from "./dom.js";

const toasts = () => document.getElementById("toasts");

export function toast(message, { kind = "neutral", link = null, timeout = 9000 } = {}) {
  const node = el("div", { class: `toast${kind === "neutral" ? "" : ` toast--${kind}`}` }, [
    icon(kind === "danger" ? "alert" : "check"),
    el("div", { class: "toast__body" }, [
      el("div", { text: message }),
      link
        ? el("div", { class: "secondary" }, [
            el("a", {
              href: "#",
              text: link.label,
              onClick: (event) => {
                event.preventDefault();
                link.action();
              },
            }),
          ])
        : null,
    ]),
    el("button", {
      class: "toast__close",
      title: "Dismiss",
      onClick: () => node.remove(),
    }, [icon("close")]),
  ]);
  toasts().appendChild(node);
  if (timeout) window.setTimeout(() => node.remove(), timeout);
  return node;
}

export function reportError(error) {
  const detail = error && error.type ? `${error.type}: ${error.message}` : String(error);
  return toast(detail, { kind: "danger", timeout: 0 });
}

// -- modal ------------------------------------------------------------------

let modalCleanup = null;

export function openModal({ title, body, actions = [] }) {
  const root = document.getElementById("modal");
  document.getElementById("modal-title").textContent = title;
  mount(document.getElementById("modal-body"), body);
  mount(document.getElementById("modal-foot"), actions);
  root.classList.add("is-open");
  modalCleanup = () => root.classList.remove("is-open");
  return { close: closeModal };
}

export function closeModal() {
  if (modalCleanup) modalCleanup();
  modalCleanup = null;
}

export function modalBody() {
  return document.getElementById("modal-body");
}

export function modalFoot() {
  return document.getElementById("modal-foot");
}

// -- side peek --------------------------------------------------------------

export function openPeek(title, body) {
  const peek = document.getElementById("peek");
  document.getElementById("peek-title").textContent = title;
  mount(document.getElementById("peek-body"), body);
  peek.classList.add("is-open");
  peek.setAttribute("aria-hidden", "false");
  document.getElementById("scrim").classList.add("is-open");
}

export function closePeek() {
  const peek = document.getElementById("peek");
  peek.classList.remove("is-open", "is-full");
  peek.setAttribute("aria-hidden", "true");
  document.getElementById("scrim").classList.remove("is-open");
  clear(document.getElementById("peek-body"));
}

export function peekIsOpen() {
  return document.getElementById("peek").classList.contains("is-open");
}

export function busy(label) {
  return el("div", { class: "secondary", style: "display:flex;gap:8px;align-items:center;padding:12px 0" }, [
    el("span", { class: "spinner" }),
    el("span", { text: label }),
  ]);
}

export function empty(message) {
  return el("div", { class: "empty", text: message });
}
