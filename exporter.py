#!/usr/bin/env python3
"""
RoundCube Emails Bulk Exporter
================================
Automates clicking on individual emails in RoundCube webmail,
clicking the Export button for each one, and navigating through pages.
Every email is downloaded as a standard .eml file.

The script opens a visible browser window and waits for YOU to log in
manually.  Once the browser URL contains 'roundcube' (i.e. you have
reached the RoundCube inbox), the export starts automatically.

Usage (CLI):
    python exporter.py --url "https://yourserver.com:2096/"

Usage (config file):
    python exporter.py --config config.yaml
"""

import argparse
import logging
import re
import sys
import time
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Logging – console + file
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("exporter.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# How long (milliseconds) to wait for the user to complete manual login
# before giving up.  300 000 ms = 5 minutes.
LOGIN_TIMEOUT_MS = 300_000

# ---------------------------------------------------------------------------
# RoundCube CSS selectors
# Multiple fallback selectors are joined with commas so Playwright tries each.
# Adjust these if your RoundCube version uses different class/id names.
# ---------------------------------------------------------------------------
SELECTORS = {
    # ── Message list rows ───────────────────────────────────────────────────
    # RoundCube renders the inbox as a <table id="messagelist"> where every
    # email is a <tr class="message …">.
    "message_rows": "#messagelist tbody tr.message",

    # ── Message-toolbar "More / ..." button ─────────────────────────────────
    # The exact id/class differs slightly between RoundCube versions;
    # the comma-separated list makes the selector robust across versions.
    "more_button": (
        "a#rcm__action_more, "
        "button#rcm__action_more, "
        "#messagetoolbar a[id*='more'], "
        "#messagetoolbar button[id*='more'], "
        "a.button.more, "
        "a[title='More'], "
        "button[title='More'], "
        "#toolbar-menu a.rcm__action_more"
    ),

    # ── "Export" item inside the More dropdown ───────────────────────────────
    "export_item": (
        "a.rcmaction_export, "
        "li a[class*='export'], "
        "li a[href*='export'], "
        "a[onclick*='export'], "
        "#message-menu a[class*='export'], "
        "ul.menu li a[id*='export']"
    ),

    # ── Next-page pagination button ─────────────────────────────────────────
    "next_page": (
        "a.nextpage, "
        "li.nextpage a, "
        "a[title='Next page'], "
        "button[title='Next page'], "
        "span.nextpage a"
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    """Load a YAML configuration file."""
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def sanitize_filename(text: str, max_len: int = 80) -> str:
    """Strip characters that are illegal in filenames."""
    allowed = set(
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789 _-."
    )
    cleaned = "".join(c if c in allowed else "_" for c in text)
    return cleaned[:max_len].strip(" _")


# ---------------------------------------------------------------------------
# Core exporter class
# ---------------------------------------------------------------------------

class RoundCubeExporter:
    """Drives a Chromium browser to export every email from a RoundCube
    mailbox as an .eml file."""

    def __init__(
        self,
        url: str,
        username: str = "",
        password: str = "",
        download_dir: str = "./exported_emails",
        mailbox: str = "INBOX",
        headless: bool = False,
        delay: float = 1.0,
        start_page: int = 1,
    ):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.download_dir = Path(download_dir)
        self.mailbox = mailbox
        self.headless = headless
        self.delay = delay
        self.start_page = start_page
        self.exported = 0
        self.failed = 0

    # ── Public entry point ──────────────────────────────────────────────────

    def run(self):
        """Launch the browser and export all emails."""
        self.download_dir.mkdir(parents=True, exist_ok=True)

        if self.headless:
            log.warning(
                "Manual login requires a visible browser window – "
                "ignoring --headless."
            )

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            context = browser.new_context(
                accept_downloads=True,
                viewport={"width": 1400, "height": 900},
            )
            page = context.new_page()
            try:
                self._wait_for_manual_login(page)
                self._go_to_mailbox(page)
                self._export_all_pages(page)
            except Exception as exc:
                log.error("Fatal error: %s", exc, exc_info=True)
            finally:
                log.info(
                    "Finished. Exported: %d  |  Failed: %d",
                    self.exported,
                    self.failed,
                )
                browser.close()

    # ── Manual-login wait ───────────────────────────────────────────────────

    def _wait_for_manual_login(self, page):
        """Open the browser at self.url and wait for the user to log in.

        Export starts only after the browser URL contains '/roundcube',
        which indicates that the user has successfully authenticated and
        reached the RoundCube webmail application.  The script waits up
        to 5 minutes for the URL to change, then asks the user to press
        Enter before the actual export begins.
        """
        log.info("Opening %s", self.url)
        page.goto(self.url, wait_until="networkidle")

        if "/roundcube" not in page.url.lower():
            log.info(
                "==========================================================\n"
                "  Please log in manually in the browser window.\n"
                "  Waiting until the URL contains 'roundcube' …\n"
                "  (timeout: 5 minutes)\n"
                "=========================================================="
            )
            # Wait until the browser URL contains '/roundcube' (case-insensitive).
            # This fires as soon as the user reaches the RoundCube inbox.
            page.wait_for_url(
                re.compile(r"/roundcube", re.IGNORECASE),
                timeout=LOGIN_TIMEOUT_MS,
            )
            log.info("RoundCube detected in URL.")

        # Capture the RoundCube base URL (strip query string so that
        # _go_to_mailbox can append its own parameters).
        self.url = page.url.split("?")[0].rstrip("/")
        log.info("RoundCube base URL set to %s", self.url)

        # Give the user a chance to confirm the page is fully ready before
        # the automated export starts (e.g. let slow pages finish loading,
        # or navigate to a specific folder first).
        input(
            "\n"
            "  ============================================================\n"
            "  Browser is on the RoundCube page.  Make sure the inbox (or\n"
            "  the folder you want to export) is fully loaded, then press\n"
            "  Enter here to start the export …\n"
            "  ============================================================\n"
        )

    # ── Navigate to the target mailbox ─────────────────────────────────────

    def _go_to_mailbox(self, page):
        target = f"{self.url}?_task=mail&_mbox={self.mailbox}"
        if self.start_page > 1:
            target += f"&_page={self.start_page}"
        log.info(
            "Navigating to mailbox '%s' (starting at page %d) …",
            self.mailbox,
            self.start_page,
        )
        page.goto(target, wait_until="networkidle")
        time.sleep(self.delay)

    # ── Iterate through every page ─────────────────────────────────────────

    def _export_all_pages(self, page):
        current_page = self.start_page

        while True:
            log.info("━━━ Page %d ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", current_page)
            rows = page.query_selector_all(SELECTORS["message_rows"])

            if not rows:
                log.warning("No message rows found on page %d – stopping.", current_page)
                break

            log.info("Found %d message(s)", len(rows))

            # Export each message; re-query the DOM after each click because
            # the live NodeList may be invalidated when the view updates.
            for idx in range(len(rows)):
                rows = page.query_selector_all(SELECTORS["message_rows"])
                if idx >= len(rows):
                    break
                self._export_single(page, rows[idx], current_page, idx + 1)
                time.sleep(self.delay)

            # ── Advance to the next page ────────────────────────────────────
            next_btn = page.query_selector(SELECTORS["next_page"])
            if not next_btn:
                log.info("No 'Next page' button – reached end of mailbox.")
                break

            classes = next_btn.get_attribute("class") or ""
            aria_disabled = next_btn.get_attribute("aria-disabled") or ""
            disabled_attr = next_btn.get_attribute("disabled")
            if disabled_attr is not None or aria_disabled == "true" or "disabled" in classes:
                log.info("'Next page' button is disabled – reached last page.")
                break

            log.info("Moving to page %d …", current_page + 1)
            next_btn.click()
            page.wait_for_load_state("networkidle")
            time.sleep(self.delay)
            current_page += 1

    # ── Export a single email ───────────────────────────────────────────────

    def _export_single(self, page, row, page_num: int, row_idx: int):
        # Build a human-readable label from the message subject (if available)
        subject = ""
        subject_el = row.query_selector("td.subject, .subject, span.subject")
        if subject_el:
            subject = sanitize_filename(subject_el.inner_text().strip())

        label = f"page{page_num:04d}_msg{row_idx:04d}"
        if subject:
            label = f"{label}_{subject}"

        log.info("  → [%s]", label)

        try:
            # 1. Click the message row to select/open it
            row.click()
            page.wait_for_load_state("networkidle")
            time.sleep(0.5)

            # 2. Open the "More / ..." toolbar dropdown
            more_btn = page.wait_for_selector(
                SELECTORS["more_button"], timeout=10_000
            )
            more_btn.click()
            time.sleep(0.4)

            # 3. Click "Export" and capture the browser download
            with page.expect_download(timeout=30_000) as dl_info:
                export_item = page.wait_for_selector(
                    SELECTORS["export_item"], timeout=5_000
                )
                export_item.click()

            download = dl_info.value
            dest = self.download_dir / f"{label}.eml"
            download.save_as(dest)
            log.info("     ✓ Saved → %s", dest)
            self.exported += 1

        except PlaywrightTimeoutError as exc:
            log.warning("     ✗ Timeout – [%s]: %s", label, exc)
            self.failed += 1
            # Dismiss any open overlay/dropdown before continuing
            page.keyboard.press("Escape")

        except Exception as exc:
            log.warning("     ✗ Error – [%s]: %s", label, exc)
            self.failed += 1
            page.keyboard.press("Escape")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Bulk-export all emails from RoundCube webmail as .eml files "
            "by automating the browser's Export button."
        )
    )
    parser.add_argument(
        "--config",
        metavar="FILE",
        help="Path to a YAML config file (see config.example.yaml).",
    )
    parser.add_argument("--url",      help="URL to open in the browser (e.g. your cPanel webmail URL).")
    parser.add_argument("--username", help="(Unused – login is manual.) Kept for config-file compatibility.")
    parser.add_argument("--password", help="(Unused – login is manual.) Kept for config-file compatibility.")
    parser.add_argument(
        "--download-dir",
        default=None,
        metavar="DIR",
        help="Directory to save .eml files (default: ./exported_emails).",
    )
    parser.add_argument(
        "--mailbox",
        default=None,
        help="Mailbox / folder to export (default: INBOX).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=None,
        help="Run browser in headless mode (no visible window).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=None,
        help="Seconds to wait between actions (default: 1.0).",
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=None,
        metavar="N",
        help="Page number to start from – useful for resuming (default: 1).",
    )
    return parser.parse_args()


def main():
    args = _parse_args()

    # Load optional config file first; CLI flags override individual keys.
    cfg: dict = {}
    if args.config:
        cfg = load_config(args.config)

    url      = args.url          or cfg.get("url")
    username = args.username     or cfg.get("username", "")
    password = args.password     or cfg.get("password", "")

    if not url:
        print(
            "ERROR: --url is required.\n"
            "       Supply it as a CLI flag or via a --config YAML file.",
            file=sys.stderr,
        )
        sys.exit(1)

    download_dir = args.download_dir or cfg.get("download_dir", "./exported_emails")
    mailbox      = args.mailbox      or cfg.get("mailbox",      "INBOX")
    headless     = args.headless     if args.headless is not None else cfg.get("headless", False)
    delay        = args.delay        if args.delay    is not None else cfg.get("delay",    1.0)
    start_page   = args.start_page   if args.start_page is not None else cfg.get("start_page", 1)

    exporter = RoundCubeExporter(
        url=url,
        username=username,
        password=password,
        download_dir=download_dir,
        mailbox=mailbox,
        headless=headless,
        delay=delay,
        start_page=start_page,
    )
    exporter.run()


if __name__ == "__main__":
    main()
