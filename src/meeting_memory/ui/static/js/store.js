// Session-wide state. Deliberately small: no optimistic writes live here, so a
// row only changes after the server has confirmed the commit.

import { api } from "./api.js";

export const store = {
  session: null,
  knowledge: [],
  basket: null,
  reviewCounts: { conflict: 0, linked: 0, unlinked: 0 },
  pendingReviewCount: 0,
};

const listeners = new Set();

export function onChange(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function emit() {
  for (const listener of listeners) listener(store);
}

export async function refreshSession() {
  store.session = await api.session();
  store.basket = store.session.removal_basket;
  emit();
  return store.session;
}

export async function refreshKnowledge() {
  store.knowledge = (await api.knowledgeIndex()).objects;
  emit();
  return store.knowledge;
}

export async function refreshBasket() {
  store.basket = await api.basket();
  emit();
  return store.basket;
}

export function setReviewCounts(counts, pending) {
  store.reviewCounts = counts;
  store.pendingReviewCount = pending;
  emit();
}
