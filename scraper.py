#!/usr/bin/env python3
"""
Waterstones Signed Editions Scraper
=====================================
Extracts the list of signed / special-edition books from the Waterstones
website.  The page uses Cloudflare protection and lazy-loads its content as
the visitor scrolls, so this scraper uses Playwright (a real browser) with
stealth patches that make it harder for bot-detection systems to identify it
as automated traffic.

Usage
-----
Install dependencies once:
    pip install -r requirements.txt
    playwright install chromium

Then run:
    python scraper.py                        # default URL
    python scraper.py --url <url>            # custom URL
    python scraper.py --output results.json  # custom output file
    python scraper.py --headless false       # show the browser window
"""

import argparse
import json
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import stealth_sync

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

DEFAULT_URL = "https://www.waterstones.com/category/signed-editions"
DEFAULT_OUTPUT = "signed_editions.json"

# How long (ms) to wait for the page to become interactive after navigation
PAGE_LOAD_TIMEOUT = 60_000

# Number of pixels to scroll per step while loading lazy content
SCROLL_STEP = 800

# Pause between scroll steps (seconds) – give the browser time to fetch/render
SCROLL_PAUSE = 1.5

# Maximum total scroll iterations before giving up (safety cap)
MAX_SCROLL_ITERATIONS = 80

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_browser_context(playwright, headless: bool):
    """Launch a Chromium browser with settings designed to reduce bot
    detection signals, and return (browser, context, page)."""
    browser = playwright.chromium.launch(
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    context = browser.new_context(
        viewport={"width": 1366, "height": 768},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        locale="en-GB",
        timezone_id="Europe/London",
        # Pretend to accept common web content types
        extra_http_headers={
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
        },
    )
    page = context.new_page()
    # Apply stealth patches to the page (removes webdriver flags, etc.)
    stealth_sync(page)
    return browser, context, page


def _scroll_to_bottom(page) -> None:
    """Scroll the page incrementally to trigger lazy-loaded content.

    Waterstones uses infinite-scroll / intersection-observer patterns, so we
    must scroll slowly enough for each batch of books to load before we move
    on.  We stop once two consecutive scroll passes find the page height
    unchanged (i.e. no more content is being added).
    """
    previous_height = -1
    no_change_streak = 0
    iteration = 0

    while iteration < MAX_SCROLL_ITERATIONS:
        page.evaluate(f"window.scrollBy(0, {SCROLL_STEP})")
        time.sleep(SCROLL_PAUSE)

        current_height = page.evaluate("document.body.scrollHeight")
        scroll_y = page.evaluate("window.scrollY + window.innerHeight")

        if current_height == previous_height:
            no_change_streak += 1
            # Two consecutive iterations with no height change → done
            if no_change_streak >= 2:
                break
        else:
            no_change_streak = 0

        # Also stop once the viewport bottom has reached the document bottom
        if scroll_y >= current_height:
            no_change_streak += 1
            if no_change_streak >= 2:
                break

        previous_height = current_height
        iteration += 1

    # Final scroll to the very bottom
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(SCROLL_PAUSE)


def _extract_books_from_html(html: str) -> list[dict]:
    """Parse the fully-rendered HTML and return a list of book dicts.

    Strategy
    --------
    1. Locate every ``<h2>`` whose text is "Special Editions" (case-insensitive
       match so minor whitespace/capitalisation variations are handled).
    2. Collect all ``<a>`` tags that follow that heading, up to the next
       top-level section boundary (another ``<h2>``/``<h1>``, or end of page).
    3. From each ``<a>`` tag extract the book title and author.

    Waterstones typically structures each product link as::

        <a href="/book/...">
          <span class="title">Book Title</span>
          <span class="author">Author Name</span>
          ...
        </a>

    but the exact HTML can vary.  We therefore try several fallback strategies
    so the scraper is resilient to minor template changes.
    """
    soup = BeautifulSoup(html, "lxml")
    books: list[dict] = []

    # ------------------------------------------------------------------
    # 1. Find the "Special Editions" heading
    # ------------------------------------------------------------------
    special_editions_heading = None
    for heading in soup.find_all(["h1", "h2"]):
        if "special editions" in heading.get_text(strip=True).lower():
            special_editions_heading = heading
            break

    if special_editions_heading is None:
        print(
            "WARNING: Could not find a 'Special Editions' heading on this page.\n"
            "The page structure may have changed, or Cloudflare may be blocking "
            "the request (check the saved HTML for details).",
            file=sys.stderr,
        )
        # Fall back: scan the entire page for book links
        section_soup = soup
    else:
        # ------------------------------------------------------------------
        # 2. Collect all siblings / descendants after the heading until the
        #    next major section boundary.
        # ------------------------------------------------------------------
        section_soup = _collect_section_after_heading(special_editions_heading)

    # ------------------------------------------------------------------
    # 3. Extract book details from <a> tags
    # ------------------------------------------------------------------
    seen_urls: set[str] = set()
    for anchor in section_soup.find_all("a", href=True):
        href = anchor["href"]
        # Only consider product links (Waterstones book URLs contain /book/)
        if "/book/" not in href:
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)

        book = _parse_anchor(anchor, href)
        if book:
            books.append(book)

    return books


def _collect_section_after_heading(heading) -> BeautifulSoup:
    """Return a BeautifulSoup fragment containing all elements that follow
    *heading* up to (but not including) the next ``<h1>`` or ``<h2>`` sibling."""
    fragment_html = []
    for sibling in heading.next_siblings:
        tag = getattr(sibling, "name", None)
        if tag in ("h1", "h2"):
            break
        fragment_html.append(str(sibling))

    # If we collected nothing from siblings, try the parent's siblings instead
    if not fragment_html:
        node = heading.parent
        for sibling in node.next_siblings:
            tag = getattr(sibling, "name", None)
            if tag in ("h1", "h2"):
                break
            fragment_html.append(str(sibling))

    return BeautifulSoup("".join(fragment_html), "lxml")


def _parse_anchor(anchor, href: str) -> dict | None:
    """Extract title and author from a single product ``<a>`` element.

    Returns ``None`` if no meaningful text could be extracted.
    """
    title = ""
    author = ""

    # Strategy A – explicit span classes used by Waterstones
    title_span = anchor.find(class_=lambda c: c and "title" in c.lower())
    author_span = anchor.find(class_=lambda c: c and "author" in c.lower())
    if title_span:
        title = title_span.get_text(separator=" ", strip=True)
    if author_span:
        author = author_span.get_text(separator=" ", strip=True)

    # Strategy B – try data attributes
    if not title:
        title = anchor.get("data-title") or anchor.get("title") or ""
    if not author:
        author = anchor.get("data-author") or ""

    # Strategy C – fall back to the full anchor text; try to split on newline
    if not title:
        full_text = anchor.get_text(separator="\n", strip=True)
        parts = [p.strip() for p in full_text.splitlines() if p.strip()]
        if parts:
            title = parts[0]
        if len(parts) > 1 and not author:
            author = parts[1]

    if not title and not author:
        return None

    # Build absolute URL
    if href.startswith("/"):
        url = f"https://www.waterstones.com{href}"
    else:
        url = href

    return {
        "title": title,
        "author": author,
        "url": url,
    }


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------


def scrape(url: str, output: str, headless: bool) -> list[dict]:
    """Navigate to *url*, scroll the page fully, then extract and return
    signed-edition books.  Results are also written to *output* as JSON."""
    print(f"Navigating to: {url}")
    print(f"Headless mode: {headless}")

    with sync_playwright() as playwright:
        browser, context, page = _build_browser_context(playwright, headless)

        try:
            # ----------------------------------------------------------------
            # Load the page
            # ----------------------------------------------------------------
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
            except PlaywrightTimeoutError:
                print(
                    "WARNING: Page load timed out – proceeding with whatever "
                    "content was downloaded so far.",
                    file=sys.stderr,
                )

            # Wait a moment for the initial render and any challenge pages
            time.sleep(3)

            # Check for Cloudflare challenge page
            page_title = page.title().lower()
            if "just a moment" in page_title or "checking your browser" in page_title:
                print(
                    "INFO: Cloudflare challenge detected.  Waiting for it to "
                    "resolve automatically (up to 30 s)…",
                    file=sys.stderr,
                )
                try:
                    # Wait until the challenge resolves (title changes)
                    page.wait_for_function(
                        "() => !document.title.toLowerCase().includes('just a moment') "
                        "&& !document.title.toLowerCase().includes('checking your browser')",
                        timeout=30_000,
                    )
                    time.sleep(2)
                except PlaywrightTimeoutError:
                    print(
                        "WARNING: Cloudflare challenge did not resolve within "
                        "30 s.  Results may be incomplete.",
                        file=sys.stderr,
                    )

            # ----------------------------------------------------------------
            # Scroll to trigger lazy-loaded content
            # ----------------------------------------------------------------
            print("Scrolling page to load lazy content…")
            _scroll_to_bottom(page)

            # ----------------------------------------------------------------
            # Capture the fully-rendered HTML
            # ----------------------------------------------------------------
            html = page.content()

            # Optionally save the raw HTML for debugging
            Path("page_debug.html").write_text(html, encoding="utf-8")
            print("Raw HTML saved to page_debug.html (for debugging).")

        finally:
            context.close()
            browser.close()

    # ----------------------------------------------------------------
    # Parse and extract books
    # ----------------------------------------------------------------
    books = _extract_books_from_html(html)

    # ----------------------------------------------------------------
    # Output results
    # ----------------------------------------------------------------
    if books:
        print(f"\nFound {len(books)} signed edition(s):\n")
        for i, book in enumerate(books, start=1):
            print(f"  {i:>3}. {book['title']}")
            if book["author"]:
                print(f"       Author : {book['author']}")
            print(f"       URL    : {book['url']}")
    else:
        print(
            "\nNo books were extracted.  This usually means Cloudflare is "
            "blocking the request.  Try running with --headless false so you "
            "can solve any CAPTCHA manually.",
            file=sys.stderr,
        )

    output_path = Path(output)
    output_path.write_text(json.dumps(books, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nResults written to {output_path.resolve()}")

    return books


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape Waterstones signed / special edition books.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help="URL of the Waterstones page to scrape.",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Path to the JSON file where results will be saved.",
    )
    parser.add_argument(
        "--headless",
        default="true",
        choices=["true", "false"],
        help=(
            "Run the browser in headless (invisible) mode.  "
            "Set to 'false' to see the browser and manually solve CAPTCHAs."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    headless = args.headless.lower() == "true"
    scrape(url=args.url, output=args.output, headless=headless)
