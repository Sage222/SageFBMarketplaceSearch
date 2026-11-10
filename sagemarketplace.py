import sys
import re
import json
import html
import urllib.parse
from dataclasses import dataclass
from typing import List, Dict

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QSpinBox, QComboBox, QCheckBox, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QGroupBox, QGridLayout,
    QStatusBar, QMessageBox, QAbstractItemView
)
from PyQt6.QtCore import Qt, QUrl, QThread, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFont, QColor

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


@dataclass
class MarketplaceItem:
    item_id: str
    title: str
    price_str: str
    price_num: float
    orig_price_str: str
    discount_pct: float
    badge: str
    location: str
    match_type: str
    matched_snippet: str
    url: str
    raw_tooltip: str


class NumericTableWidgetItem(QTableWidgetItem):
    """Sorts numerically (Prices & Discounts) instead of alphabetically."""
    def __init__(self, text: str, sort_value: float):
        super().__init__(text)
        self.sort_value = sort_value

    def __lt__(self, other):
        if isinstance(other, NumericTableWidgetItem):
            return self.sort_value < other.sort_value
        return super().__lt__(other)


class LiveScrapeWorker(QThread):
    """Strictly verifies Must-Exist Keywords in either Title or actual Seller Description."""
    results_ready = pyqtSignal(list)
    status_update = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    BADGE_PATTERNS = re.compile(
        r"^(just listed|newly listed|pending|sold|price drop|reduced|sponsored|"
        r"ships to you|shipping available|free shipping|delivery available|"
        r"in stock|only \d+ left|available soon|\d+\s*km.*away)$",
        re.IGNORECASE
    )

    LOCATION_PATTERN = re.compile(
        r",\s*(?:VIC|NSW|QLD|WA|SA|TAS|ACT|NT|Australia|[A-Z]{2})\b",
        re.IGNORECASE
    )

    def __init__(self, target_url: str, must_keywords: List[str], exclude_keywords: List[str],
                 min_price: int, max_price: int, condition_label: str,
                 match_mode: str = "strict_title_and_desc", scroll_passes: int = 5, headless: bool = True):
        super().__init__()
        self.target_url = target_url
        self.must_keywords = [k.lower().strip() for k in must_keywords if k.strip()]
        self.exclude_keywords = [k.lower().strip() for k in exclude_keywords if k.strip()]
        self.min_price = min_price
        self.max_price = max_price
        self.condition_label = condition_label
        self.match_mode = match_mode
        self.scroll_passes = scroll_passes
        self.headless = headless
        self.graphql_descriptions: Dict[str, str] = {}

    @staticmethod
    def decode_json_string(raw_str: str) -> str:
        """Safely decodes JSON-escaped strings (including \\/ and \\uXXXX) without DeprecationWarnings."""
        try:
            return json.loads(f'"{raw_str}"')
        except Exception:
            return raw_str.replace(r"\/", "/").replace(r"\n", "\n")

    @staticmethod
    def parse_price_number(text: str) -> float:
        if "free" in text.lower():
            return 0.0
        nums = re.findall(r"[\d,]+(?:\.\d+)?", text)
        if nums:
            return float(nums[0].replace(",", ""))
        return -1.0

    def _extract_graphql_descriptions(self, response):
        """Passively captures item descriptions sent in Facebook's background GraphQL JSON payloads."""
        if "graphql" not in response.url:
            return
        try:
            text = response.text()
            for match in re.finditer(
                r'"id"\s*:\s*"(\d{10,20})"[^{}]{0,1500}?"(?:redacted_description|description)"\s*:\s*\{\s*"text"\s*:\s*"((?:\\.|[^"\\])*)"',
                text
            ):
                item_id, desc_raw = match.group(1), match.group(2)
                self.graphql_descriptions[item_id] = self.decode_json_string(desc_raw)
        except Exception:
            pass

    @staticmethod
    def build_snippet(text: str, keywords: List[str]) -> str:
        """Creates a clean preview snippet showing where the keywords matched in the description."""
        clean = re.sub(r"\s+", " ", text).strip()
        if not keywords or not clean:
            return clean[:120]
        lower = clean.lower()
        positions = [lower.find(k) for k in keywords if lower.find(k) != -1]
        if not positions:
            return clean[:120]
        first_pos = min(positions)
        start = max(0, first_pos - 30)
        end = min(len(clean), first_pos + 95)
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(clean) else ""
        return f"{prefix}{clean[start:end]}{suffix}"

    def parse_card(self, anchor_el, full_url: str) -> dict:
        raw_text = anchor_el.inner_text().strip()
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]

        img_alt = ""
        try:
            img_el = anchor_el.query_selector("img")
            if img_el:
                img_alt = (img_el.get_attribute("alt") or "").strip()
        except Exception:
            pass

        prices = []
        badges = []
        locations = []
        text_candidates = []

        for line in lines:
            price_tokens = re.findall(
                r"(?:AU\$|A\$|US\$|CA\$|\$|£|€)\s*[\d,]+(?:\.\d{1,2})?|\bFree\b",
                line, re.IGNORECASE
            )
            stripped_of_prices = re.sub(
                r"(?:AU\$|A\$|US\$|CA\$|\$|£|€)\s*[\d,]+(?:\.\d{1,2})?|\bFree\b",
                "", line, flags=re.IGNORECASE
            ).strip(" ·•-|,")

            if price_tokens and not stripped_of_prices:
                for pt in price_tokens:
                    prices.append((pt.strip(), self.parse_price_number(pt)))
                continue

            if self.BADGE_PATTERNS.match(line):
                badges.append(line)
                continue

            if self.LOCATION_PATTERN.search(line) and len(line) < 45:
                locations.append(line)
                continue

            text_candidates.append(line)

        price_str = "N/A"
        price_val = -1.0
        orig_price_str = "—"
        discount_pct = 0.0

        if prices:
            price_str, price_val = prices[0]
            if len(prices) > 1:
                orig_str, orig_val = prices[1]
                if orig_val > price_val > 0:
                    diff = orig_val - price_val
                    discount_pct = round((diff / orig_val) * 100, 1)
                    orig_price_str = f"{orig_str} (-${diff:,.0f} / {discount_pct:.0f}%)"
                elif orig_str != price_str:
                    orig_price_str = orig_str

        title = ""
        if text_candidates:
            title = text_candidates[0]
        elif img_alt:
            title = re.sub(r"\s+in\s+[^,]+,\s*[A-Za-z\s]+$", "", img_alt).strip()

        if not title and locations:
            title = locations.pop(0)
        if not title:
            title = "Untitled Listing"

        location = locations[-1] if locations else "Local Area"
        badge_str = ", ".join(dict.fromkeys(badges)) if badges else "Standard"

        id_match = re.search(r"/item/(\d+)", full_url)
        item_id = id_match.group(1) if id_match else "—"

        return {
            "item_id": item_id,
            "title": title,
            "price_str": price_str,
            "price_val": price_val,
            "orig_price_str": orig_price_str,
            "discount_pct": discount_pct,
            "badge": badge_str,
            "location": location,
            "img_alt": img_alt,
            "raw_tooltip": f"Raw Card Text:\n{raw_text}\n\nImage Alt: {img_alt}"
        }

    def extract_seller_description_from_page(self, detail_page, raw_html: str) -> str:
        """Extracts ONLY the target listing's description (ignoring 'Similar items' carousels)."""
        desc_parts = []

        # 1. Check meta description / og:description
        for meta_sel in ['meta[name="description"]', 'meta[property="og:description"]', 'meta[property="og:title"]']:
            try:
                el = detail_page.query_selector(meta_sel)
                if el:
                    content = el.get_attribute("content")
                    if content:
                        desc_parts.append(html.unescape(content))
            except Exception:
                pass

        # 2. Check embedded GraphQL JSON inside the item page's <script> tags
        for m in re.finditer(r'"redacted_description"\s*:\s*\{\s*"text"\s*:\s*"((?:\\.|[^"\\])*)"', raw_html):
            try:
                decoded = self.decode_json_string(m.group(1))
                desc_parts.append(html.unescape(decoded))
            except Exception:
                pass

        # 3. Fallback: Extract text above the "Similar items" boundary
        if not desc_parts:
            try:
                main_text = detail_page.inner_text("body")
                for cutoff_phrase in ["Similar items", "More from this seller", "Sponsored", "Today's picks"]:
                    idx = main_text.find(cutoff_phrase)
                    if idx != -1:
                        main_text = main_text[:idx]
                desc_parts.append(main_text)
            except Exception:
                pass

        return "\n".join(desc_parts)

    def run(self):
        if not PLAYWRIGHT_AVAILABLE:
            self.error_occurred.emit(
                "Playwright is not installed.\nRun: python -m playwright install chromium"
            )
            return

        items: List[MarketplaceItem] = []
        try:
            self.status_update.emit("Launching browser and loading Facebook Marketplace...")
            with sync_playwright() as p:
                try:
                    browser = p.chromium.launch(headless=self.headless)
                except Exception:
                    browser = p.chromium.launch(channel="chrome", headless=self.headless)

                context = browser.new_context(
                    viewport={"width": 1400, "height": 960},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                )
                page = context.new_page()
                page.on("response", self._extract_graphql_descriptions)
                page.goto(self.target_url, timeout=45000)

                self.status_update.emit("Waiting for Marketplace listings to render...")
                try:
                    page.wait_for_selector('a[href*="/marketplace/item/"]', timeout=15000)
                except Exception:
                    page.wait_for_timeout(4000)

                try:
                    close_btn = page.query_selector('div[aria-label="Close"], div[role="button"][aria-label="Close"]')
                    if close_btn:
                        close_btn.click()
                        page.wait_for_timeout(500)
                except Exception:
                    pass

                for i in range(self.scroll_passes):
                    self.status_update.emit(
                        f"Scrolling search feed ({i + 1}/{self.scroll_passes})..."
                    )
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(1300)

                valid_hrefs_above_fold = page.evaluate("""() => {
                    const links = [];
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
                    let node;
                    while ((node = walker.nextNode())) {
                        const txt = (node.innerText || '').trim();
                        if (node.tagName.match(/^H[1-4]$|^SPAN$/) &&
                            (txt === 'Results from outside your search' || txt === 'Results outside your search area')) {
                            break;
                        }
                        if (node.tagName === 'A' && node.href && node.href.includes('/marketplace/item/')) {
                            links.push(node.getAttribute('href'));
                        }
                    }
                    return links;
                }""")
                valid_href_set = set(valid_hrefs_above_fold) if valid_hrefs_above_fold else None

                anchors = page.query_selector_all('a[href*="/marketplace/item/"]')
                seen_urls = set()
                needs_desc_verification = []

                for a in anchors:
                    href = a.get_attribute("href")
                    if not href:
                        continue
                    if valid_href_set is not None and href not in valid_href_set:
                        continue

                    clean_href = href.split("?")[0]
                    full_url = f"https://www.facebook.com{clean_href}" if clean_href.startswith("/") else clean_href
                    if full_url in seen_urls:
                        continue
                    seen_urls.add(full_url)

                    parsed = self.parse_card(a, full_url)
                    title = parsed["title"]
                    price_val = parsed["price_val"]
                    searchable_card_text = f"{title} {parsed['img_alt']}".lower()

                    if self.exclude_keywords and any(bad in searchable_card_text for bad in self.exclude_keywords):
                        continue

                    if price_val >= 0:
                        if self.min_price > 0 and price_val < self.min_price:
                            continue
                        if self.max_price > 0 and price_val > self.max_price:
                            continue

                    title_has_all_kw = all(kw in searchable_card_text for kw in self.must_keywords)

                    if not self.must_keywords or title_has_all_kw:
                        items.append(MarketplaceItem(
                            item_id=parsed["item_id"],
                            title=title,
                            price_str=parsed["price_str"],
                            price_num=price_val,
                            orig_price_str=parsed["orig_price_str"],
                            discount_pct=parsed["discount_pct"],
                            badge=parsed["badge"],
                            location=parsed["location"],
                            match_type="✅ Verified in Title",
                            matched_snippet=f"Title: {title}",
                            url=full_url,
                            raw_tooltip=parsed["raw_tooltip"]
                        ))
                    elif self.match_mode == "strict_title_and_desc":
                        gql_desc = self.graphql_descriptions.get(parsed["item_id"], "")
                        if gql_desc:
                            gql_lower = f"{searchable_card_text} {gql_desc.lower()}"
                            if all(kw in gql_lower for kw in self.must_keywords) and not any(
                                bad in gql_lower for bad in self.exclude_keywords
                            ):
                                snippet = self.build_snippet(gql_desc, self.must_keywords)
                                items.append(MarketplaceItem(
                                    item_id=parsed["item_id"],
                                    title=title,
                                    price_str=parsed["price_str"],
                                    price_num=price_val,
                                    orig_price_str=parsed["orig_price_str"],
                                    discount_pct=parsed["discount_pct"],
                                    badge=parsed["badge"],
                                    location=parsed["location"],
                                    match_type="✅ Verified in Desc",
                                    matched_snippet=f"Desc: {snippet}",
                                    url=full_url,
                                    raw_tooltip=f"{parsed['raw_tooltip']}\n\nSeller Description:\n{gql_desc}"
                                ))
                        else:
                            needs_desc_verification.append((parsed, full_url, searchable_card_text))

                if needs_desc_verification and self.match_mode == "strict_title_and_desc":
                    total_cand = len(needs_desc_verification)
                    pool_size = min(4, total_cand)
                    worker_pages = []
                    for _ in range(pool_size):
                        wp = context.new_page()
                        wp.route(
                            "**/*.{png,jpg,jpeg,webp,gif,svg,mp4,woff,woff2,ttf,css}",
                            lambda route: route.abort()
                        )
                        worker_pages.append(wp)

                    for idx, (parsed, full_url, searchable_card_text) in enumerate(needs_desc_verification):
                        self.status_update.emit(
                            f"Strictly verifying seller descriptions ({idx + 1}/{total_cand}): {parsed['title'][:40]}... "
                            f"[Verified Matches: {len(items)}]"
                        )
                        wp = worker_pages[idx % pool_size]
                        try:
                            wp.goto(full_url, wait_until="domcontentloaded", timeout=15000)
                            raw_html = wp.content()
                            seller_desc = self.extract_seller_description_from_page(wp, raw_html)
                            combined_text = f"{searchable_card_text}\n{seller_desc}".lower()

                            if all(kw in combined_text for kw in self.must_keywords):
                                if not (self.exclude_keywords and any(bad in combined_text for bad in self.exclude_keywords)):
                                    snippet = self.build_snippet(seller_desc, self.must_keywords)
                                    items.append(MarketplaceItem(
                                        item_id=parsed["item_id"],
                                        title=parsed["title"],
                                        price_str=parsed["price_str"],
                                        price_num=parsed["price_val"],
                                        orig_price_str=parsed["orig_price_str"],
                                        discount_pct=parsed["discount_pct"],
                                        badge=parsed["badge"],
                                        location=parsed["location"],
                                        match_type="✅ Verified in Desc",
                                        matched_snippet=f"Desc: {snippet}",
                                        url=full_url,
                                        raw_tooltip=f"{parsed['raw_tooltip']}\n\nVerified Description:\n{seller_desc[:600]}"
                                    ))
                        except Exception:
                            pass

                    for wp in worker_pages:
                        wp.close()

                browser.close()
            self.results_ready.emit(items)

        except Exception as e:
            self.error_occurred.emit(str(e))


class FBMarketplaceSearchApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Facebook Marketplace Advanced Search & Filter Pro")
        self.resize(1420, 800)
        self.apply_dark_theme()
        self.init_ui()

    def apply_dark_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #18191A;
                color: #E4E6EB;
                font-family: 'Segoe UI', -apple-system, Roboto, sans-serif;
                font-size: 13px;
            }
            QGroupBox {
                border: 1px solid #3E4042;
                border-radius: 8px;
                margin-top: 12px;
                padding: 12px;
                background-color: #242526;
                font-weight: bold;
                color: #2D88FF;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
            }
            QLineEdit, QSpinBox, QComboBox {
                background-color: #3A3B3C;
                border: 1px solid #4E4F50;
                border-radius: 6px;
                padding: 6px 10px;
                color: #E4E6EB;
            }
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
                border: 1px solid #2D88FF;
            }
            QPushButton {
                background-color: #2D88FF;
                color: #FFFFFF;
                border: none;
                border-radius: 6px;
                padding: 8px 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #1A73E8;
            }
            QPushButton#secondaryBtn {
                background-color: #3A3B3C;
                border: 1px solid #4E4F50;
            }
            QPushButton#secondaryBtn:hover {
                background-color: #4E4F50;
            }
            QTableWidget {
                background-color: #242526;
                alternate-background-color: #2C2D2E;
                border: 1px solid #3E4042;
                border-radius: 8px;
                gridline-color: #3E4042;
            }
            QHeaderView::section {
                background-color: #3A3B3C;
                color: #E4E6EB;
                padding: 8px;
                border: none;
                border-right: 1px solid #242526;
                font-weight: bold;
            }
            QHeaderView::section:hover {
                background-color: #4E4F50;
            }
            QCheckBox {
                spacing: 6px;
            }
        """)

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        filter_group = QGroupBox("Search Parameters & Strict Filters")
        grid = QGridLayout(filter_group)
        grid.setSpacing(10)

        # Row 0
        grid.addWidget(QLabel("Main Search Item:"), 0, 0)
        self.query_input = QLineEdit()
        self.query_input.setPlaceholderText("e.g., OnePlus Pad, Tablet, Laptop (or leave blank)")
        grid.addWidget(self.query_input, 0, 1)

        grid.addWidget(QLabel("Must-Exist Keywords (comma-separated):"), 0, 2)
        self.must_input = QLineEdit("snapdragon, elite")
        self.must_input.setPlaceholderText("Every keyword listed here MUST exist in the ad")
        grid.addWidget(self.must_input, 0, 3)

        # Row 1
        grid.addWidget(QLabel("Exclude Words (comma-separated):"), 1, 0)
        self.exclude_input = QLineEdit()
        self.exclude_input.setPlaceholderText("e.g., broken, parts, faulty, box only")
        grid.addWidget(self.exclude_input, 1, 1)

        grid.addWidget(QLabel("City / Location Slug:"), 1, 2)
        self.location_input = QLineEdit("melbourne")
        grid.addWidget(self.location_input, 1, 3)

        # Row 2
        grid.addWidget(QLabel("Min Price ($):"), 2, 0)
        self.min_price_spin = QSpinBox()
        self.min_price_spin.setRange(0, 1_000_000)
        self.min_price_spin.setSingleStep(50)
        self.min_price_spin.setValue(600)
        grid.addWidget(self.min_price_spin, 2, 1)

        grid.addWidget(QLabel("Max Price ($):"), 2, 2)
        self.max_price_spin = QSpinBox()
        self.max_price_spin.setRange(0, 1_000_000)
        self.max_price_spin.setSingleStep(50)
        self.max_price_spin.setValue(2000)
        grid.addWidget(self.max_price_spin, 2, 3)

        # Row 3
        grid.addWidget(QLabel("Item Condition:"), 3, 0)
        cond_layout = QHBoxLayout()
        self.cond_checkboxes = {
            "new": QCheckBox("New"),
            "used_like_new": QCheckBox("Used - Like New"),
            "used_good": QCheckBox("Used - Good"),
            "used_fair": QCheckBox("Used - Fair"),
        }
        self.cond_checkboxes["new"].setChecked(True)
        self.cond_checkboxes["used_like_new"].setChecked(True)
        self.cond_checkboxes["used_good"].setChecked(True)
        for cb in self.cond_checkboxes.values():
            cond_layout.addWidget(cb)
        grid.addLayout(cond_layout, 3, 1, 1, 3)

        # Row 4
        grid.addWidget(QLabel("Sort Order:"), 4, 0)
        self.sort_combo = QComboBox()
        self.sort_combo.addItem("Best Match (Finds Description Matches Best)", "best_match")
        self.sort_combo.addItem("Newest First (Recommended for Deals)", "creation_time_descend")
        self.sort_combo.addItem("Price: Low to High", "price_ascend")
        self.sort_combo.addItem("Price: High to Low", "price_descend")
        self.sort_combo.addItem("Distance: Nearest First", "distance_ascend")
        grid.addWidget(self.sort_combo, 4, 1)

        grid.addWidget(QLabel("Listed Within:"), 4, 2)
        self.days_combo = QComboBox()
        self.days_combo.addItem("Any Time", "")
        self.days_combo.addItem("Last 24 Hours", "1")
        self.days_combo.addItem("Last 7 Days", "7")
        self.days_combo.addItem("Last 30 Days", "30")
        grid.addWidget(self.days_combo, 4, 3)

        # Row 5
        grid.addWidget(QLabel("Strict Verification Mode:"), 5, 0)
        self.scope_combo = QComboBox()
        self.scope_combo.addItem(
            "Strictly Verify Title + Seller Description (100% Accurate - Filters out false results)",
            "strict_title_and_desc"
        )
        self.scope_combo.addItem(
            "Strict Title Only (Instant - Card title alone must contain all keywords)",
            "title_only"
        )
        grid.addWidget(self.scope_combo, 5, 1)

        grid.addWidget(QLabel("Scroll Depth (Pages):"), 5, 2)
        self.scroll_spin = QSpinBox()
        self.scroll_spin.setRange(1, 25)
        self.scroll_spin.setValue(4)
        grid.addWidget(self.scroll_spin, 5, 3)

        main_layout.addWidget(filter_group)

        # Action Buttons + Live Filter Bar
        btn_layout = QHBoxLayout()
        self.gen_links_btn = QPushButton("⚡ Generate Advanced Search Links")
        self.gen_links_btn.clicked.connect(self.populate_smart_links)

        self.live_scrape_btn = QPushButton("🔍 Live Extract & Strictly Verify (Playwright)")
        self.live_scrape_btn.setObjectName("secondaryBtn")
        self.live_scrape_btn.clicked.connect(self.run_live_extraction)

        self.table_filter_input = QLineEdit()
        self.table_filter_input.setPlaceholderText("🔎 Quick filter table rows...")
        self.table_filter_input.textChanged.connect(self.filter_existing_table)

        self.headless_cb = QCheckBox("Run Browser Headless")
        self.headless_cb.setChecked(True)

        btn_layout.addWidget(self.gen_links_btn, 2)
        btn_layout.addWidget(self.live_scrape_btn, 2)
        btn_layout.addWidget(self.table_filter_input, 2)
        btn_layout.addWidget(self.headless_cb)
        main_layout.addLayout(btn_layout)

        # 9-Column Enriched Sortable Table
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels([
            "Ad Title",
            "Current Price",
            "Was / Discount",
            "Badge / Status",
            "Verification",
            "Matched Context (Where Keywords Were Found)",
            "Location",
            "Item ID",
            "Action"
        ])
        self.table.horizontalHeader().setCursor(Qt.CursorShape.PointingHandCursor)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        for col in [1, 2, 3, 4, 6, 7, 8]:
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)

        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSortIndicatorShown(True)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.cellDoubleClicked.connect(self.on_cell_double_clicked)
        main_layout.addWidget(self.table)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready. Every result is strictly verified to contain ALL Must-Exist Keywords.")

    def filter_existing_table(self, text: str):
        needle = text.lower().strip()
        for row in range(self.table.rowCount()):
            row_text = " ".join(
                (self.table.item(row, c).text().lower() if self.table.item(row, c) else "")
                for c in range(8)
            )
            self.table.setRowHidden(row, bool(needle and needle not in row_text))

    def build_url(self, query_str: str, conditions: List[str], sort_by: str, exact: bool = False) -> str:
        loc = self.location_input.text().strip() or "melbourne"
        base_url = f"https://www.facebook.com/marketplace/{urllib.parse.quote(loc)}/search"

        params = {"query": query_str}
        if exact:
            params["exact"] = "true"
        if sort_by and sort_by != "best_match":
            params["sortBy"] = sort_by

        min_p = self.min_price_spin.value()
        max_p = self.max_price_spin.value()
        if min_p > 0:
            params["minPrice"] = str(min_p)
        if max_p > 0:
            params["maxPrice"] = str(max_p)

        days = self.days_combo.currentData()
        if days:
            params["daysSinceListed"] = days

        if conditions:
            params["itemCondition"] = ",".join(conditions)

        return f"{base_url}?{urllib.parse.urlencode(params)}"

    def get_selected_conditions(self) -> tuple[List[str], str]:
        codes = [code for code, cb in self.cond_checkboxes.items() if cb.isChecked()]
        labels = [cb.text() for cb in self.cond_checkboxes.values() if cb.isChecked()]
        return codes, (", ".join(labels) if labels else "Any Condition")

    def add_item_row(self, item: MarketplaceItem):
        row = self.table.rowCount()
        self.table.insertRow(row)

        # Col 0: Ad Title
        title_item = QTableWidgetItem(item.title)
        title_item.setToolTip(item.raw_tooltip or item.title)
        title_item.setData(Qt.ItemDataRole.UserRole, item.url)
        self.table.setItem(row, 0, title_item)

        # Col 1: Current Price
        price_item = NumericTableWidgetItem(item.price_str, item.price_num if item.price_num >= 0 else 9999999.0)
        price_item.setForeground(QColor("#45BD62"))
        price_item.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.table.setItem(row, 1, price_item)

        # Col 2: Original Price / Discount
        orig_item = NumericTableWidgetItem(item.orig_price_str, item.discount_pct)
        if item.discount_pct > 0:
            orig_item.setForeground(QColor("#F5A623"))
            orig_item.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        else:
            orig_item.setForeground(QColor("#8A8D91"))
        self.table.setItem(row, 2, orig_item)

        # Col 3: Badge / Status
        badge_item = QTableWidgetItem(item.badge)
        if "just listed" in item.badge.lower() or "new" in item.badge.lower():
            badge_item.setForeground(QColor("#2D88FF"))
            badge_item.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.table.setItem(row, 3, badge_item)

        # Col 4: Verification Status
        match_item = QTableWidgetItem(item.match_type)
        match_item.setForeground(QColor("#45BD62"))
        self.table.setItem(row, 4, match_item)

        # Col 5: Matched Context Snippet
        snippet_item = QTableWidgetItem(item.matched_snippet)
        snippet_item.setForeground(QColor("#B0B3B8"))
        snippet_item.setToolTip(item.raw_tooltip)
        self.table.setItem(row, 5, snippet_item)

        # Col 6: Location
        self.table.setItem(row, 6, QTableWidgetItem(item.location))

        # Col 7: Item ID
        id_item = QTableWidgetItem(item.item_id)
        id_item.setForeground(QColor("#8A8D91"))
        self.table.setItem(row, 7, id_item)

        # Col 8: Clickable Button
        open_btn = QPushButton("🔗 Open Ad")
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.setToolTip(item.url)
        open_btn.clicked.connect(lambda checked=False, target=item.url: QDesktopServices.openUrl(QUrl(target)))
        self.table.setCellWidget(row, 8, open_btn)

    def on_cell_double_clicked(self, row: int, col: int):
        item = self.table.item(row, 0)
        if item:
            url = item.data(Qt.ItemDataRole.UserRole)
            if url:
                QDesktopServices.openUrl(QUrl(url))

    def populate_smart_links(self):
        base_q = self.query_input.text().strip()
        must_kws = [k.strip() for k in self.must_input.text().split(",") if k.strip()]

        if not base_q and not must_kws:
            QMessageBox.warning(self, "Missing Input", "Please enter a Main Search Item or Must-Exist Keywords.")
            return

        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)

        cond_codes, cond_label = self.get_selected_conditions()
        min_p = self.min_price_spin.value()
        max_p = self.max_price_spin.value()
        price_range_str = f"${min_p:,} - ${max_p:,}" if max_p > 0 else f"${min_p:,}+"
        loc = self.location_input.text().strip() or "melbourne"
        selected_sort = self.sort_combo.currentData()

        unquoted_query = f"{base_q} {' '.join(must_kws)}".strip()
        quoted_must = " ".join(f'"{kw}"' for kw in must_kws)
        quoted_query = f"{base_q} {quoted_must}".strip()

        links = [
            (f"[Title + Description Search] {unquoted_query}", "Full-Text Search",
             self.build_url(unquoted_query, cond_codes, selected_sort, exact=False)),
            (f"[Strict Quoted Search] {quoted_query}", "Exact Quotes",
             self.build_url(quoted_query, cond_codes, selected_sort, exact=True)),
            (f"[24h Deal Sniper] {unquoted_query}", "Listed Last 24h",
             self.build_url(unquoted_query, cond_codes, "creation_time_descend", exact=False) + "&daysSinceListed=1"),
        ]

        for title, badge, url in links:
            self.add_item_row(MarketplaceItem(
                item_id="Search Link",
                title=title,
                price_str=price_range_str,
                price_num=float(min_p),
                orig_price_str="—",
                discount_pct=0.0,
                badge=badge,
                location=loc.title(),
                match_type="Direct URL",
                matched_snippet=cond_label,
                url=url,
                raw_tooltip=url
            ))

        self.table.setSortingEnabled(True)
        self.status_bar.showMessage(f"Generated {self.table.rowCount()} search links.")

    def run_live_extraction(self):
        base_q = self.query_input.text().strip()
        must_kws = [k.strip() for k in self.must_input.text().split(",") if k.strip()]
        exclude_kws = [k.strip() for k in self.exclude_input.text().split(",") if k.strip()]

        if not base_q and not must_kws:
            QMessageBox.warning(self, "Missing Input", "Please enter a Main Search Item or Must-Exist Keywords.")
            return

        cond_codes, cond_label = self.get_selected_conditions()
        match_mode = self.scope_combo.currentData()

        if match_mode == "title_only":
            quoted_must = " ".join(f'"{kw}"' for kw in must_kws)
            combined_query = f"{base_q} {quoted_must}".strip()
            target_url = self.build_url(combined_query, cond_codes, self.sort_combo.currentData(), exact=True)
        else:
            combined_query = f"{base_q} {' '.join(must_kws)}".strip()
            target_url = self.build_url(combined_query, cond_codes, self.sort_combo.currentData(), exact=False)

        self.live_scrape_btn.setEnabled(False)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)

        self.worker = LiveScrapeWorker(
            target_url=target_url,
            must_keywords=must_kws,
            exclude_keywords=exclude_kws,
            min_price=self.min_price_spin.value(),
            max_price=self.max_price_spin.value(),
            condition_label=cond_label,
            match_mode=match_mode,
            scroll_passes=self.scroll_spin.value(),
            headless=self.headless_cb.isChecked()
        )
        self.worker.status_update.connect(self.status_bar.showMessage)
        self.worker.results_ready.connect(self.on_live_results)
        self.worker.error_occurred.connect(self.on_live_error)
        self.worker.start()

    def on_live_results(self, items: List[MarketplaceItem]):
        self.live_scrape_btn.setEnabled(True)
        if not items:
            self.table.setSortingEnabled(True)
            self.status_bar.showMessage("No listings contained all Must-Exist Keywords.")
            return

        for item in items:
            self.add_item_row(item)

        self.table.setSortingEnabled(True)
        self.status_bar.showMessage(
            f"Verification complete! Found {len(items)} listings confirmed to contain ALL keywords."
        )

    def on_live_error(self, err_msg: str):
        self.live_scrape_btn.setEnabled(True)
        self.table.setSortingEnabled(True)
        QMessageBox.critical(self, "Live Extraction Error", err_msg)
        self.status_bar.showMessage("Live extraction failed.")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = FBMarketplaceSearchApp()
    window.show()
    sys.exit(app.exec())