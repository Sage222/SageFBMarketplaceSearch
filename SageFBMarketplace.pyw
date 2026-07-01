import sys
import threading
import urllib.parse
import time
import webbrowser
import customtkinter as ctk
from playwright.sync_api import sync_playwright

# App styling defaults
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

class ModernMarketplaceApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Melbourne Marketplace Sniper Pro")
        self.geometry("850x700")
        self.minsize(700, 500)

        # Base UI Typography
        self.ui_font = ctk.CTkFont(family="Segoe UI", size=13)
        self.bold_ui_font = ctk.CTkFont(family="Segoe UI", size=13, weight="bold")

        # --- Layout Grid Configuration ---
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        # --- UI Component: Main Search ---
        self.search_label = ctk.CTkLabel(self, text="Base Search Term:", font=self.bold_ui_font)
        self.search_label.grid(row=0, column=0, padx=20, pady=(20, 5), sticky="w")
        
        self.search_entry = ctk.CTkEntry(self, placeholder_text="e.g., macbook pro", font=self.ui_font, height=32)
        self.search_entry.grid(row=0, column=0, padx=(160, 20), pady=(20, 5), sticky="ew")

        # --- UI Component: Mandatory Keywords ---
        self.keywords_label = ctk.CTkLabel(self, text="Required Keywords:", font=self.bold_ui_font)
        self.keywords_label.grid(row=1, column=0, padx=20, pady=5, sticky="w")
        
        self.keywords_entry = ctk.CTkEntry(self, placeholder_text="e.g., m1, 16gb (comma separated)", font=self.ui_font, height=32)
        self.keywords_entry.grid(row=1, column=0, padx=(160, 20), pady=5, sticky="ew")

        # --- UI Component: Condition Dropdown ---
        self.condition_label = ctk.CTkLabel(self, text="Item Condition:", font=self.bold_ui_font)
        self.condition_label.grid(row=2, column=0, padx=20, pady=5, sticky="w")
        
        self.condition_dropdown = ctk.CTkOptionMenu(
            self, 
            values=["Used - Like New", "Used - Good", "Used - Fair", "New"],
            font=self.ui_font
        )
        self.condition_dropdown.set("Used - Like New")
        self.condition_dropdown.grid(row=2, column=0, padx=(160, 20), pady=5, sticky="w")

        # --- UI Component: Action Button ---
        self.search_button = ctk.CTkButton(
            self, 
            text="Launch Melbourne Search (+100km)", 
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            height=40,
            command=self.start_search_thread
        )
        self.search_button.grid(row=3, column=0, padx=20, pady=20, sticky="ew")

        # --- UI Component: Rich Output Feed ---
        self.output_display = ctk.CTkTextbox(self, wrap="word")
        self.output_display.grid(row=4, column=0, padx=20, pady=(0, 20), sticky="nsew")
        
        # Accessing the underlying Tkinter text widget mechanics for advanced font styling
        self.text_widget = self.output_display._textbox
        self.setup_text_tags()
        
        self.log_system_message("System Online. Target: Melbourne Core + 100km Radius.")

    def setup_text_tags(self):
        """Pre-configures text styles and formatting layers for the output feed."""
        self.text_widget.tag_config("sys_info", font=("Segoe UI", 11, "italic"), foreground="#9CA3AF")
        self.text_widget.tag_config("sys_error", font=("Segoe UI", 11, "bold"), foreground="#EF4444")
        self.text_widget.tag_config("match_header", font=("Segoe UI", 14, "bold"), foreground="#10B981")
        self.text_widget.tag_config("item_title", font=("Segoe UI", 13, "bold"))
        self.text_widget.tag_config("item_details", font=("Segoe UI", 12))
        self.text_widget.tag_config("label_prefix", font=("Segoe UI", 12, "bold"), foreground="#3B82F6")
        self.text_widget.tag_config("separator", font=("Segoe UI", 10), foreground="#4B5563")

    def log_system_message(self, text, style="sys_info"):
        """Appends system statuses to the feed."""
        self.text_widget.insert("end", f"[System] {text}\n", style)
        self.text_widget.see("end")

    def display_matched_item(self, count, title, details, url):
        """Renders a beautifully formatted card with a native hyperlinked URL."""
        self.text_widget.insert("end", f"\n✨ MATCH FOUND [{count}] ✨\n", "match_header")
        
        self.text_widget.insert("end", "Title: ", "label_prefix")
        self.text_widget.insert("end", f"{title}\n", "item_title")
        
        self.text_widget.insert("end", "Details: ", "label_prefix")
        self.text_widget.insert("end", f"{details}\n", "item_details")
        
        self.text_widget.insert("end", "Link: ", "label_prefix")
        
        # Generate a unique tag pointer for this specific listing link
        link_tag = f"link_{count}"
        self.text_widget.insert("end", f"{url}\n", link_tag)
        
        # Style the hyperlink dynamically
        self.text_widget.tag_config(link_tag, font=("Segoe UI", 12, "underline"), foreground="#3B82F6")
        
        # Bind interactions: click to open browser, change cursor on hover
        self.text_widget.tag_bind(link_tag, "<Button-1>", lambda event, target_url=url: webbrowser.open(target_url))
        self.text_widget.tag_bind(link_tag, "<Enter>", lambda event: self.text_widget.config(cursor="hand2"))
        self.text_widget.tag_bind(link_tag, "<Leave>", lambda event: self.text_widget.config(cursor=""))
        
        self.text_widget.insert("end", f"{'—' * 65}\n", "separator")
        self.text_widget.see("end")

    def start_search_thread(self):
        """Dispatches the Playwright scanner onto a background worker thread."""
        search_term = self.search_entry.get().strip()
        keywords_raw = self.keywords_entry.get().strip()
        condition_ui = self.condition_dropdown.get()

        if not search_term:
            self.log_system_message("Please enter a base search term.", style="sys_error")
            return

        condition_map = {
            "Used - Like New": "used_like_new",
            "Used - Good": "used_good",
            "Used - Fair": "used_fair",
            "New": "new"
        }
        condition_param = condition_map.get(condition_ui, "used_like_new")
        mandatory_keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]

        self.search_button.configure(state="disabled", text="Scanning Marketplace Hub...")
        self.text_widget.delete("1.0", "end")
        
        threading.Thread(
            target=self.run_scraper, 
            args=(search_term, mandatory_keywords, condition_param), 
            daemon=True
        ).start()

    def run_scraper(self, search_term, mandatory_keywords, condition_param):
        """Core automation engine scanning the 100km Melbourne cache."""
        base_url = "https://www.facebook.com/marketplace/melbourne/search/?"
        params = {
            "query": search_term,
            "itemCondition": condition_param,
            "sortBy": "creation_time_descend",
            "radius": "100",
            "exact": "false"
        }
        url = base_url + urllib.parse.urlencode(params)
        
        self.log_system_message(f"Targeting parameters generated:\n{url}\n")
        self.log_system_message("Spinning up underlying Chromium instance...")

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False)
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                page = context.new_page()
                
                self.log_system_message("Opening live feed stream...")
                page.goto(url)
                
                # Settle buffer for infinite scroll initialization
                time.sleep(5)
                
                self.log_system_message("Analyzing target item blocks against keyword filters...")
                listings = page.locator('a[href*="/marketplace/item/"]').all()
                
                matches_found = 0
                seen_urls = set()

                for item in listings:
                    try:
                        href = item.get_attribute("href")
                        inner_text = item.inner_text()
                        
                        if not inner_text or not href:
                            continue
                        
                        lines = [line.strip() for line in inner_text.split("\n") if line.strip()]
                        if not lines:
                            continue
                        
                        text_lower = inner_text.lower()
                        if all(kw.lower() in text_lower for kw in mandatory_keywords):
                            clean_url = "https://www.facebook.com" + href.split("?")[0]
                            
                            if clean_url not in seen_urls:
                                seen_urls.add(clean_url)
                                matches_found += 1
                                
                                title = lines[0]
                                details = " | ".join(lines[1:])
                                
                                # Send matching data structure directly to the styled card renderer
                                self.after(0, self.display_matched_item, matches_found, title, details, clean_url)
                    except Exception:
                        continue
                        
                browser.close()
                self.after(0, self.log_system_message, f"Sweep complete. Safely indexed {matches_found} listings.")

        except Exception as e:
            self.after(0, self.log_system_message, f"Pipeline Error encountered: {str(e)}", "sys_error")
        
        self.after(0, lambda: self.search_button.configure(state="normal", text="Launch Melbourne Search (+100km)"))

if __name__ == "__main__":
    app = ModernMarketplaceApp()
    app.mainloop()