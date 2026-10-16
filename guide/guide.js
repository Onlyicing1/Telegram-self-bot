/**
 * DOM wiring for the LifeOS guide site.
 *
 * All search logic lives in `search.js` (pure, unit-tested); this file only
 * reads the shipped articles, calls those helpers and reflects the result
 * in the page. Without JavaScript every article stays visible and readable.
 */
import { buildIndex, searchEntries, statusTextFor, toPersianDigits } from "./search.js";

const articles = Array.from(document.querySelectorAll(".guide-article"));
const categoryCards = Array.from(document.querySelectorAll("[data-category-link]"));

const form = document.getElementById("search-form");
const input = document.getElementById("search-input");
const clearButton = document.getElementById("search-clear");
const region = document.getElementById("search-region");
const status = document.getElementById("search-status");
const resultsList = document.getElementById("search-results");
const emptyState = document.getElementById("no-results");
const resetButton = document.getElementById("no-results-reset");
const navToggle = document.getElementById("nav-toggle");
const nav = document.getElementById("category-nav");

const index = buildIndex(
  articles.map((article) => ({
    id: article.id,
    title: article.dataset.title || "",
    category: article.dataset.category || "",
    keywords: article.dataset.keywords || "",
    body: article.innerText || "",
    href: `#${article.id}`,
  })),
);

function resetView() {
  articles.forEach((article) => {
    article.hidden = false;
  });
  categoryCards.forEach((card) => {
    card.hidden = false;
  });
  resultsList.replaceChildren();
  region.hidden = true;
  emptyState.hidden = true;
  status.textContent = "";
}

function renderResults(matches) {
  resultsList.replaceChildren();

  matches.forEach((match) => {
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.href = match.href;
    link.textContent = match.title;

    const badge = document.createElement("span");
    badge.className = "result-badge";
    badge.textContent = `${match.category} — ${toPersianDigits(match.score)} امتیاز`;
    link.append(badge);

    item.append(link);
    resultsList.append(item);
  });
}

function runSearch(rawQuery) {
  const query = (rawQuery || "").trim();
  if (!query) {
    resetView();
    return [];
  }

  const matches = searchEntries(index, query);
  const matchedIds = new Set(matches.map((match) => match.id));

  articles.forEach((article) => {
    article.hidden = !matchedIds.has(article.id);
  });
  categoryCards.forEach((card) => {
    card.hidden = !matchedIds.has(card.dataset.categoryLink);
  });

  region.hidden = false;
  status.textContent = statusTextFor(matches.length);
  emptyState.hidden = matches.length !== 0;
  clearButton.hidden = false;
  renderResults(matches);
  return matches;
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  runSearch(input.value);
  if (input.value.trim()) region.scrollIntoView({ block: "start" });
});

input.addEventListener("input", () => {
  clearButton.hidden = input.value.trim() === "";
  runSearch(input.value);
});

function clearSearch({ focus = true } = {}) {
  input.value = "";
  clearButton.hidden = true;
  resetView();
  if (focus) input.focus();
}

clearButton.addEventListener("click", () => clearSearch());
resetButton.addEventListener("click", () => clearSearch());

document.addEventListener("keydown", (event) => {
  const target = event.target;
  const isTyping =
    target instanceof HTMLElement &&
    (target.tagName === "INPUT" ||
      target.tagName === "TEXTAREA" ||
      target.isContentEditable);

  if (event.key === "/" && !isTyping) {
    event.preventDefault();
    input.focus();
    return;
  }

  if (event.key === "Escape" && (isTyping || input.value !== "")) {
    clearSearch();
  }
});

navToggle.addEventListener("click", () => {
  const isOpen = nav.classList.toggle("is-open");
  navToggle.setAttribute("aria-expanded", String(isOpen));
});

nav.addEventListener("click", (event) => {
  if (event.target instanceof HTMLAnchorElement) {
    nav.classList.remove("is-open");
    navToggle.setAttribute("aria-expanded", "false");
  }
});

window.addEventListener("hashchange", () => {
  if (input.value.trim() !== "") clearSearch({ focus: false });
});

// A direct hit on `#cat-...` should always be able to show its article.
if (window.location.hash) {
  const target = document.getElementById(window.location.hash.slice(1));
  if (target instanceof HTMLElement) target.hidden = false;
}
