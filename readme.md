# Facebook Marketplace Advanced Search & Filter Pro (`sagemarketplace.py`)

A dark-themed **PyQt6 desktop application** that bypasses Facebook Marketplace's notoriously fuzzy search engine. It enforces strict **Must-Exist Keywords**, **Excluded Keywords**, **Price Bounds**, and **Condition Filters** across both listing titles and full seller descriptions using a multi-stage **Playwright** verification pipeline.

---

## ✨ Key Features

* **100% Strict Keyword Verification (Title + Seller Description):**
  * **Stage 1 (Title & Alt-Text Check):** Instantly verifies listings where all required keywords appear directly on the search card.
  * **Stage 2 (GraphQL Passive Interception):** Captures hidden seller descriptions directly from Facebook's background `api/graphql/` network traffic while scrolling.
  * **Stage 3 (Concurrent Lightweight Deep Verifier):** Opens a pool of resource-blocked background tabs (aborting images, videos, fonts, and CSS for speed) to inspect the actual seller description (`meta[name="description"]` and embedded JSON-LD) while ignoring "Similar items" carousels so unrelated ads never trigger false positives.
* **Out-of-Area Padding Cutoff:** Automatically detects Facebook's `"Results from outside your search"` divider and stops scraping below it so random interstate or unrelated filler ads are ignored.
* **Intelligent Card Parser:**
  * Separates **Current Price** from **Strikethrough / Original Price** and calculates exact dollar and percentage discounts (e.g., `AU$1,600 (-$200 / 12%)`).
  * Separates status badges (`Just listed`, `Price drop`, `Pending`, `Ships to you`) so they never overwrite the actual listing title.
* **True Numeric & Alphabetical Column Sorting:** Click any column header to sort results ascending or descending. Prices and discount percentages sort mathematically (`AU$700` before `AU$1,400`).
* **Matched Context Snippets & Full Tooltips:** Displays the exact snippet from the title or seller description where your keywords were found, with full raw card text and description previews on mouse hover.
* **Direct Search Link Generator:** Instantly builds pre-filtered Marketplace URLs (`minPrice`, `maxPrice`, `itemCondition`, `daysSinceListed`, `sortBy`) without launching the scraper.

---

## 🛠️ Prerequisites & Installation

### 1. Install Python Dependencies
Requires **Python 3.10+**. Install `PyQt6` and `playwright`:

```bash
pip install PyQt6 playwright