# pull-page — Waterstones Signed Editions Scraper

A Python scraper that extracts the **Signed / Special Edition** book listings
from the [Waterstones](https://www.waterstones.com/category/signed-editions)
website.

The page is protected by Cloudflare and loads its book listings lazily as the
visitor scrolls.  The scraper uses **Playwright** (a real Chromium browser)
with stealth patches to minimise bot-detection signals, scrolls the page
automatically to trigger all lazy-loaded content, then parses the
`Special Editions` section to extract each book's title, author, and URL.

---

## Requirements

* Python 3.10 or later
* `pip` package manager

---

## Installation

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install the Playwright-managed Chromium browser (one-time)
playwright install chromium
```

---

## Usage

```bash
# Basic run (headless browser, results saved to signed_editions.json)
python scraper.py

# Show the browser window (useful for manually solving a CAPTCHA)
python scraper.py --headless false

# Custom target URL
python scraper.py --url "https://www.waterstones.com/category/signed-editions"

# Custom output file
python scraper.py --output my_books.json
```

### All options

| Flag | Default | Description |
|------|---------|-------------|
| `--url` | `https://www.waterstones.com/category/signed-editions` | Page to scrape |
| `--output` | `signed_editions.json` | Output JSON file path |
| `--headless` | `true` | `false` opens the browser window so you can solve CAPTCHAs |

---

## Output format

The results are printed to the terminal and saved as a JSON array:

```json
[
  {
    "title": "The Book Title",
    "author": "Author Name",
    "url": "https://www.waterstones.com/book/..."
  }
]
```

A `page_debug.html` file is also saved alongside the output so you can
inspect exactly what HTML the browser received (useful if results are
unexpectedly empty).

---

## Cloudflare / CAPTCHA handling

Waterstones is protected by Cloudflare.  The scraper applies stealth patches
that make the browser appear as a regular human visitor and typically pass the
automatic JS challenge.  If Cloudflare serves an interactive CAPTCHA you will
need to solve it manually:

```bash
python scraper.py --headless false
```

The browser window will open; solve the CAPTCHA, then the scraper will
continue automatically once the challenge is cleared.
