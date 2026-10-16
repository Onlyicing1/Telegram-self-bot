"""Guide website (guide/) — structure, delivery and real search behaviour.

The guide is a standalone static site served at /guide by the FastAPI app. These
tests verify the shipped artifacts themselves: the HTML/CSS/JS on disk, the HTTP
responses a reader gets, and the search module executed for real by Node.
"""
import json
import re
import shutil
import subprocess
import threading
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "guide"
INDEX = GUIDE / "index.html"
CSS = GUIDE / "styles.css"
SEARCH_JS = GUIDE / "search.js"
GUIDE_JS = GUIDE / "guide.js"

EXPECTED_ARTICLE_IDS = [
    "cat-getting-started",
    "cat-fonts",
    "cat-media",
    "cat-ai",
    "cat-workflows",
    "cat-troubleshooting",
]


class _GuideParser(HTMLParser):
    """Collect just enough structure to assert the guide's contract."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html_attrs: dict[str, str] = {}
        self.ids: list[str] = []
        self.hrefs: list[str] = []
        self.tag_counts: dict[str, int] = {}
        self.external_refs: list[str] = []
        self.inline_scripts: list[str] = []
        self.script_srcs: list[str] = []
        self.link_rels: list[tuple[str, str]] = []
        self.articles: dict[str, dict] = {}
        self._article: dict | None = None
        self._depth = 0
        self._text: list[str] = []
        self._labels: list[tuple[str, str]] = []
        self._current_label_for: str | None = None
        self._script_open = False
        self.aria_live: list[str] = []
        self.hidden_ids: set[str] = set()

    # -- helpers ---------------------------------------------------------
    def _collect_attrs(self, tag: str, attrs: dict) -> None:
        if tag == "html":
            self.html_attrs = attrs
        if "id" in attrs:
            self.ids.append(attrs["id"])
            if "hidden" in attrs:
                self.hidden_ids.add(attrs["id"])
        for key in ("href", "src"):
            value = attrs.get(key)
            if value:
                if key == "href":
                    self.hrefs.append(value)
                if value.startswith(("http://", "https://", "//")):
                    self.external_refs.append(f"{tag}[{key}]={value}")
        if tag == "script":
            if attrs.get("src"):
                self.script_srcs.append(attrs["src"])
            else:
                self._script_open = True
        if tag == "link" and attrs.get("href"):
            self.link_rels.append((attrs.get("rel", ""), attrs["href"]))
        if "aria-live" in attrs:
            self.aria_live.append(attrs["aria-live"])
        if tag == "label" and "for" in attrs:
            self._current_label_for = attrs["for"]

    # -- parser hooks ----------------------------------------------------
    def handle_starttag(self, tag, attrs):
        data = {k: (v if v is not None else "") for k, v in attrs}
        self.tag_counts[tag] = self.tag_counts.get(tag, 0) + 1
        self._collect_attrs(tag, data)
        if tag == "article" and "guide-article" in data.get("class", ""):
            self._article = {
                "id": data.get("id", ""),
                "title": data.get("data-title", ""),
                "category": data.get("data-category", ""),
                "keywords": data.get("data-keywords", ""),
                "evidence": data.get("data-evidence", ""),
                "h2": 0,
                "h3": 0,
                "summary": 0,
                "text": "",
            }
            self._text = []
            self._depth = 1
        elif self._article is not None:
            self._depth += 1
            if tag == "h2":
                self._article["h2"] += 1
            elif tag == "h3":
                self._article["h3"] += 1
            elif tag == "summary":
                self._article["summary"] += 1

    def handle_endtag(self, tag):
        if self._article is not None:
            self._depth -= 1
            if self._depth == 0:
                self._article["text"] = " ".join(" ".join(self._text).split())
                self.articles[self._article["id"]] = self._article
                self._article = None

    def handle_data(self, data):
        if self._article is not None:
            self._text.append(data)
        if getattr(self, "_script_open", False):
            self.inline_scripts.append(data)
            self._script_open = False


@pytest.fixture(scope="module")
def parsed() -> _GuideParser:
    parser = _GuideParser()
    parser.feed(INDEX.read_text(encoding="utf-8"))
    return parser


@pytest.fixture(scope="module")
def css_text() -> str:
    return CSS.read_text(encoding="utf-8")


# ── 1. Files and document structure ──────────────────────────


class TestSiteFiles:
    def test_every_guide_asset_exists_and_is_not_empty(self):
        for path in (INDEX, CSS, SEARCH_JS, GUIDE_JS, GUIDE / "README.md"):
            assert path.is_file(), path
            assert path.stat().st_size > 0, path

    def test_no_build_step_or_package_was_added(self):
        # The guide must stay dependency-free: no package.json, no bundler
        # config, no lockfile and no module directory of its own.
        assert not (GUIDE / "package.json").exists()
        assert not (GUIDE / "node_modules").exists()
        assert list(GUIDE.rglob("*.tsconfig*")) == []


class TestDocumentStructure:
    def test_language_and_direction_are_persian_rtl(self, parsed):
        assert parsed.html_attrs.get("lang") == "fa"
        assert parsed.html_attrs.get("dir") == "rtl"

    def test_single_h1_and_landmarks(self, parsed):
        assert parsed.tag_counts.get("h1") == 1
        assert parsed.tag_counts.get("header") == 1
        assert parsed.tag_counts.get("main") == 1
        assert parsed.tag_counts.get("footer") == 1
        assert parsed.tag_counts.get("nav", 0) >= 1
        assert "main" in parsed.ids

    def test_skip_link_targets_the_main_landmark(self, parsed):
        assert "#main" in parsed.hrefs

    def test_ids_are_unique(self, parsed):
        duplicates = {i for i in parsed.ids if parsed.ids.count(i) > 1}
        assert duplicates == set()

    def test_every_in_page_link_resolves(self, parsed):
        known = set(parsed.ids)
        broken = [
            href for href in parsed.hrefs
            if href.startswith("#") and href[1:] and href[1:] not in known
        ]
        assert broken == []

    def test_no_external_resources_are_referenced(self, parsed):
        assert parsed.external_refs == []
        assert all(not src.startswith("//") for src in parsed.script_srcs)
        assert [rel for rel, _ in parsed.link_rels] == ["stylesheet"]

    def test_no_inline_script_or_third_party_code(self, parsed):
        assert [text for text in parsed.inline_scripts if text.strip()] == []
        assert parsed.script_srcs == ["guide.js"]


# ── 2. Content contract ──────────────────────────────────────


class TestContentContract:
    def test_all_six_categories_are_present_as_articles(self, parsed):
        assert list(parsed.articles) == EXPECTED_ARTICLE_IDS

    def test_each_category_is_reachable_from_nav_and_card(self, parsed):
        # Every category is linked at least twice: header nav + category card.
        for aid in EXPECTED_ARTICLE_IDS:
            assert parsed.hrefs.count(f"#{aid}") >= 2, aid

    def test_each_article_has_headings_steps_and_search_terms(self, parsed):
        for aid, article in parsed.articles.items():
            assert article["h2"] == 1, aid
            # A tutorial heading or an FAQ question — every article is split
            # into at least three readable sections.
            assert article["h3"] + article["summary"] >= 3, aid
            assert len(article["keywords"]) > 20, aid
            assert article["title"], aid
            assert article["category"], aid
            assert len(article["text"]) > 400, aid

    def test_each_article_declares_existing_evidence_files(self, parsed):
        for aid, article in parsed.articles.items():
            sources = article["evidence"].split()
            assert sources, aid
            for source in sources:
                assert (ROOT / source).is_file(), f"{aid}: missing {source}"

    def test_hero_links_into_the_product_docs(self, parsed):
        assert "#cat-getting-started" in parsed.hrefs

    def test_search_controls_are_real_and_announced(self, parsed):
        assert "search-input" in parsed.ids
        assert "search-status" in parsed.ids
        assert "search-results" in parsed.ids
        assert "no-results" in parsed.ids
        assert "search-form" in parsed.ids
        assert "polite" in parsed.aria_live
        assert parsed.tag_counts.get("label", 0) >= 1
        # empty states start hidden and are revealed by script
        assert {"no-results", "search-clear"}.issubset(parsed.hidden_ids)

    def test_no_credentials_or_backend_calls_in_assets(self):
        secrets_pattern = re.compile(
            r"(API_ID|API_HASH|SESSION_STRING|BOT_OWNER_ID|BOT_TOKEN|SUPABASE_URL|"
            r"SUPABASE_SERVICE_ROLE_KEY|YDC_API_KEY|AI_[A-Z_]*API_KEY)\s*[:=]\s*"
            r"[\"']?[A-Za-z0-9_\-.:]{6,}"
        )
        network_pattern = re.compile(
            r"\bfetch\s*\(|XMLHttpRequest|WebSocket|sendBeacon|localStorage|"
            r"sessionStorage|document\.cookie|import\s*\("
        )
        for path in (INDEX, CSS, SEARCH_JS, GUIDE_JS):
            text = path.read_text(encoding="utf-8")
            assert not secrets_pattern.search(text), path.name
            assert not network_pattern.search(text), path.name


# ── 3. RTL / responsive / accessibility styling ──────────────


class TestStyling:
    def test_rtl_aware_rules_are_used(self, css_text):
        assert "margin-inline" in css_text or "padding-inline" in css_text
        assert "border-inline-start" in css_text
        assert "text-align: start" in css_text

    def test_responsive_breakpoints_and_mobile_nav(self, css_text):
        assert "max-width: 880px" in css_text
        assert "max-width: 520px" in css_text
        assert ".site-nav.is-open" in css_text

    def test_accessible_focus_and_motion_preferences(self, css_text):
        assert ":focus-visible" in css_text
        assert "prefers-reduced-motion" in css_text
        assert ".visually-hidden" in css_text

    def test_theme_is_dark_with_neon_accents(self, css_text):
        assert "--bg: #0b0a12" in css_text
        assert "--purple: #a855f7" in css_text
        assert "--cyan: #22d3ee" in css_text

    def test_stylesheet_blocks_are_balanced(self, css_text):
        assert css_text.count("{") == css_text.count("}")


class TestAssetsParse:
    @pytest.mark.parametrize("script", ["search.js", "guide.js"])
    def test_javascript_parses(self, script):
        node = shutil.which("node")
        if node is None:  # pragma: no cover - environment dependent
            pytest.skip("node is not available to parse the guide scripts")
        completed = subprocess.run(
            [node, "--check", str(GUIDE / script)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr


# ── 4. Delivery over HTTP (the app + a plain static server) ──


class TestHttpDelivery:
    def test_app_serves_the_guide_at_guide(self):
        from fastapi.testclient import TestClient

        from backend.web.app import app

        client = TestClient(app)
        page = client.get("/guide/")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert 'dir="rtl"' in page.text and 'lang="fa"' in page.text
        for aid in EXPECTED_ARTICLE_IDS:
            assert f'id="{aid}"' in page.text

        for asset, content_type in (
            ("styles.css", "text/css"),
            ("search.js", "text/javascript"),
            ("guide.js", "text/javascript"),
        ):
            response = client.get(f"/guide/{asset}")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith(content_type)

        assert client.get("/guide/missing-page").status_code == 404
        # the mount must not shadow the existing API surface
        assert client.get("/health").json() == {"status": "ok"}

    def test_plain_static_host_can_serve_the_directory(self):
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

        class _Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                return

            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(GUIDE), **kwargs)

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Quiet)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with urllib.request.urlopen(f"{base}/", timeout=10) as response:
                assert response.status == 200
                body = response.read().decode("utf-8")
            assert 'lang="fa"' in body
            with urllib.request.urlopen(f"{base}/styles.css", timeout=10) as response:
                assert response.status == 200
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


# ── 5. The shipped search module, executed in Node ───────────


def _article_entries() -> list[dict]:
    parser = _GuideParser()
    parser.feed(INDEX.read_text(encoding="utf-8"))
    return [
        {
            "id": article["id"],
            "title": article["title"],
            "category": article["category"],
            "keywords": article["keywords"],
            "body": article["text"],
            "href": f"#{article['id']}",
        }
        for article in parser.articles.values()
    ]


@pytest.fixture(scope="module")
def node_results(tmp_path_factory) -> dict:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - environment dependent
        pytest.skip("node is not available to execute guide/search.js")

    payload = {
        "entries": _article_entries(),
        "queries": {
            "empty": "",
            "fonts_persian": "فونت",
            "fonts_arabic_keyboard": "فونت\u200cهاي",
            "save_persian": "ذخیره",
            "save_all_tokens": "ذخیره عمیق",
            "trigger_english": "Nova",
            "trigger_upper": "NOVA",
            "bio_persian": "بیو",
            "database_persian": "supabase",
            "digits_persian": "۲۷",
            "no_match": "zzzznope",
        },
        "normalize_cases": [
            ["كتاب", "کتاب"],
            ["ي", "ی"],
            ["۱۲۳", "123"],
            ["A\u200cB", "a b"],
            ["  سَلام  ", "سلام"],
        ],
    }
    payload_path = tmp_path_factory.mktemp("guide") / "payload.json"
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    runner = tmp_path_factory.mktemp("guide") / "runner.mjs"
    runner.write_text(
        "import { readFileSync } from 'node:fs';\n"
        "import * as search from "
        f"{json.dumps(SEARCH_JS.resolve().as_uri())};\n"
        "const payload = JSON.parse(readFileSync(process.argv[2], 'utf8'));\n"
        "const index = search.buildIndex(payload.entries);\n"
        "const results = {};\n"
        "for (const [name, query] of Object.entries(payload.queries)) {\n"
        "  results[name] = search.searchEntries(index, query).map((m) => m.id);\n"
        "}\n"
        "const normalized = payload.normalize_cases.map(\n"
        "  ([input, expected]) => [search.normalizeText(input), expected]);\n"
        "process.stdout.write(JSON.stringify({\n"
        "  results,\n"
        "  normalized,\n"
        "  persian_digits: search.toPersianDigits(27),\n"
        "  status_zero: search.statusTextFor(0),\n"
        "  status_two: search.statusTextFor(2),\n"
        "}));\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [node, str(runner), str(payload_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


class TestSearchBehaviour:
    def test_empty_query_returns_every_article(self, node_results):
        assert node_results["results"]["empty"] == EXPECTED_ARTICLE_IDS

    @pytest.mark.parametrize(
        "query_name,expected",
        [
            ("fonts_persian", "cat-fonts"),
            ("fonts_arabic_keyboard", "cat-fonts"),
            ("save_persian", "cat-media"),
            ("save_all_tokens", "cat-media"),
            ("trigger_english", "cat-ai"),
            ("trigger_upper", "cat-ai"),
            ("bio_persian", "cat-workflows"),
            ("database_persian", "cat-troubleshooting"),
            ("digits_persian", "cat-fonts"),
        ],
    )
    def test_persian_and_latin_queries_hit_the_right_article(
        self, node_results, query_name, expected
    ):
        assert expected in node_results["results"][query_name]

    def test_unknown_query_returns_nothing(self, node_results):
        assert node_results["results"]["no_match"] == []

    def test_normalisation_folds_arabic_keys_and_diacritics(self, node_results):
        for actual, expected in node_results["normalized"]:
            assert actual == expected

    def test_persian_helper_output(self, node_results):
        assert node_results["persian_digits"] == "۲۷"
        assert "پیدا نشد" in node_results["status_zero"]
        assert node_results["status_two"].startswith("۲")
