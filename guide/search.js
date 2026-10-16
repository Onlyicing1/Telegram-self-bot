/**
 * Pure search helpers for the LifeOS guide site.
 *
 * This module never touches the DOM, so the shipped file is exercised
 * directly by `tests/test_69_guide_site.py` (through Node) — the search
 * behaviour that ships is the search behaviour that is tested.
 *
 * Persian/Arabic normalisation is intentionally aggressive: Arabic
 * yeh/kaf, alef variants, heh, diacritics, tatweel, zero-width joiners
 * and Persian/Arabic digits all fold to one canonical form so that a
 * query typed with an Arabic keyboard still matches Persian copy.
 */

const LETTER_FOLDS = {
  "\u064A": "\u06CC", // ي -> ی
  "\u0649": "\u06CC", // ى -> ی
  "\u0643": "\u06A9", // ك -> ک
  "\u0629": "\u0647", // ة -> ه
  "\u0623": "\u0627", // أ -> ا
  "\u0625": "\u0627", // إ -> ا
  "\u0622": "\u0627", // آ -> ا
  "\u0671": "\u0627", // ٱ -> ا
  "\u0624": "\u0648", // ؤ -> و
  "\u0626": "\u06CC", // ئ -> ی
};

const DIACRITICS = /[\u064B-\u0655\u0670\u06D6-\u06ED]/g;
const TATWEEL = /\u0640/g;
const INVISIBLES = /[\u200B\u200E\u200F\u202A-\u202E\u2066-\u2069\uFEFF]/g;
const JOINERS = /[\u200C\u200D]/g;
const ARABIC_INDIC_DIGITS = /[\u0660-\u0669]/g;
const EXTENDED_ARABIC_INDIC_DIGITS = /[\u06F0-\u06F9]/g;
const PERSIAN_DIGITS = "\u06F0\u06F1\u06F2\u06F3\u06F4\u06F5\u06F6\u06F7\u06F8\u06F9";

/** Convert a number (or digit string) to Persian digits. */
export function toPersianDigits(value) {
  return String(value).replace(/\d/g, (digit) => PERSIAN_DIGITS[Number(digit)]);
}

/** Fold a string into the canonical form used for matching. */
export function normalizeText(value) {
  return String(value === undefined || value === null ? "" : value)
    .replace(JOINERS, " ")
    .replace(INVISIBLES, "")
    .replace(TATWEEL, "")
    .replace(DIACRITICS, "")
    .replace(/[\u064A\u0649\u0643\u0629\u0623\u0625\u0622\u0671\u0624\u0626]/g,
      (char) => LETTER_FOLDS[char])
    .replace(ARABIC_INDIC_DIGITS, (digit) =>
      String(digit.charCodeAt(0) - 0x0660))
    .replace(EXTENDED_ARABIC_INDIC_DIGITS, (digit) =>
      String(digit.charCodeAt(0) - 0x06F0))
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * Build the searchable index from plain article descriptors.
 * Each entry keeps the original display fields plus normalised copies.
 */
export function buildIndex(articles) {
  return (articles || []).map((article) => {
    const title = String(article.title || "");
    const keywords = String(article.keywords || "");
    const body = String(article.body || "");
    return {
      id: String(article.id || ""),
      title,
      category: String(article.category || ""),
      href: String(article.href || `#${article.id || ""}`),
      normalizedTitle: normalizeText(title),
      normalizedKeywords: normalizeText(keywords),
      normalizedBody: normalizeText(body),
    };
  });
}

/**
 * Search the index. Every whitespace-separated token of the query must
 * appear in the entry (title, keywords or body); title hits weigh most.
 * An empty query returns every entry unchanged.
 */
export function searchEntries(entries, query) {
  const indexed = entries || [];
  const normalized = normalizeText(query);
  if (!normalized) return indexed.slice();

  const tokens = normalized.split(" ").filter(Boolean);
  const matches = [];

  for (const entry of indexed) {
    let score = 0;
    const fields = new Set();
    let matchedAll = true;

    for (const token of tokens) {
      let tokenScore = 0;
      if (entry.normalizedTitle.includes(token)) {
        tokenScore += 6;
        fields.add("title");
      }
      if (entry.normalizedKeywords.includes(token)) {
        tokenScore += 3;
        fields.add("keywords");
      }
      if (entry.normalizedBody.includes(token)) {
        tokenScore += 1;
        fields.add("body");
      }
      if (tokenScore === 0) {
        matchedAll = false;
        break;
      }
      score += tokenScore;
    }

    if (matchedAll) {
      matches.push({
        id: entry.id,
        title: entry.title,
        category: entry.category,
        href: entry.href,
        score,
        fields: Array.from(fields),
      });
    }
  }

  return matches.sort(
    (a, b) => b.score - a.score || a.title.localeCompare(b.title, "fa"),
  );
}

/** Persian status line for a result count. */
export function statusTextFor(count) {
  if (count === 0) return "\u0645\u0648\u0631\u062f\u06CC \u067E\u06CC\u062F\u0627 \u0646\u0634\u062F";
  return `${toPersianDigits(count)} \u0646\u062A\u06CC\u062C\u0647 \u067E\u06CC\u062F\u0627 \u0634\u062F`;
}
