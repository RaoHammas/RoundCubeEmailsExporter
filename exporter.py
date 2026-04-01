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

# How long (seconds) to wait for the user to click an element in the picker
# before giving up.  180 s = 3 minutes.
PICKER_TIMEOUT_S = 180

# File where user-supplied selector overrides are persisted.
SELECTORS_FILE = Path("selectors.yaml")

# ---------------------------------------------------------------------------
# RoundCube CSS selectors (defaults)
# Multiple fallback selectors are joined with commas so Playwright tries each.
# Adjust these if your RoundCube version uses different class/id names, or
# let the script prompt you at runtime and save to selectors.yaml.
# ---------------------------------------------------------------------------
DEFAULT_SELECTORS = {
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
# Browser-side element picker
# Injected into the live page; user hovers to inspect, clicks to capture.
# After one click the picker removes itself and stores the result in
# window.__rce_picked = { general, path, id, tag, classes, text, title, href }.
# ---------------------------------------------------------------------------
PICKER_JS = r"""
(function () {
    if (window.__rcePicker) { window.__rcePicker.cleanup(); }
    window.__rce_picked = null;

    /* ── Tooltip ──────────────────────────────────────────────────────────── */
    var tip = document.createElement('div');
    tip.style.cssText = [
        'position:fixed', 'z-index:2147483647', 'pointer-events:none',
        'background:#1a1a2e', 'color:#e8e8e8', 'padding:6px 10px',
        'border-radius:6px', 'font:12px/1.5 monospace', 'max-width:520px',
        'word-break:break-all', 'box-shadow:0 2px 12px rgba(0,0,0,.65)',
        'border:2px solid #e74c3c', 'display:none', 'white-space:pre'
    ].join(';');
    document.body.appendChild(tip);

    /* ── Banner ───────────────────────────────────────────────────────────── */
    var banner = document.createElement('div');
    banner.id = '__rce_banner';
    banner.style.cssText = [
        'position:fixed', 'top:0', 'left:0', 'right:0',
        'z-index:2147483646', 'background:#c0392b', 'color:#fff',
        'text-align:center', 'padding:8px 16px',
        'font:bold 13px/1.4 sans-serif', 'letter-spacing:.4px',
        'pointer-events:none'
    ].join(';');
    banner.textContent =
        '\uD83C\uDFAF ELEMENT PICKER ACTIVE \u2014 hover to inspect \u25b8 click to capture';
    document.body.appendChild(banner);

    var prev = null, prevOutline = '';

    /* ── CSS selector builders ────────────────────────────────────────────── */
    /* State-specific classes that should not be part of a stable selector. */
    var STATE_CLS = /^(unread|read|selected|focused|active|hover|disabled|first|last|odd|even|checked|expanded|collapsed|open|closed)$/i;

    function mkGeneral(el) {
        var tag = el.tagName.toLowerCase();
        var cls = Array.from(el.classList).filter(function (c) {
            return c.trim() && c.length < 40 && !/^\d/.test(c) && !STATE_CLS.test(c);
        }).slice(0, 4);
        return tag + (cls.length ? '.' + cls.join('.') : '');
    }

    function mkPath(el) {
        var parts = [];
        var cur = el;
        while (cur && cur.tagName && cur !== document.documentElement) {
            if (cur.id) { parts.unshift('#' + cur.id); break; }
            var part = cur.tagName.toLowerCase();
            var cls = Array.from(cur.classList).filter(function (c) {
                return c.trim() && c.length < 40 && !/^\d/.test(c) && !STATE_CLS.test(c);
            }).slice(0, 3);
            if (cls.length) part += '.' + cls.join('.');
            parts.unshift(part);
            try {
                if (document.querySelectorAll(parts.join(' > ')).length === 1) break;
            } catch (e) { /* ignore invalid selector fragments */ }
            cur = cur.parentElement;
        }
        return parts.join(' > ');
    }

    function onMove(e) {
        var el = document.elementFromPoint(e.clientX, e.clientY);
        if (!el || el.id === '__rce_banner') return;
        if (el !== prev) {
            if (prev) prev.style.outline = prevOutline;
            prev = el; prevOutline = el.style.outline || '';
            el.style.outline = '2px solid #e74c3c';
        }
        var gen  = mkGeneral(el);
        var path = mkPath(el);
        tip.textContent =
            'general : ' + gen +
            '\npath    : ' + path +
            '\ntag     : ' + el.tagName.toLowerCase() +
            (el.id        ? '\nid      : ' + el.id        : '') +
            (el.className ? '\nclasses : ' + el.className : '');
        tip.style.display = 'block';
        var x = e.clientX + 16, y = e.clientY + 16;
        if (x + 530 > window.innerWidth)  x = Math.max(0, e.clientX - 530);
        if (y + 110 > window.innerHeight) y = Math.max(0, e.clientY - 110);
        tip.style.left = x + 'px';
        tip.style.top  = y + 'px';
    }

    function onClick(e) {
        var el = document.elementFromPoint(e.clientX, e.clientY);
        if (!el || el.id === '__rce_banner') return;
        e.preventDefault();
        e.stopImmediatePropagation();
        window.__rce_picked = {
            general : mkGeneral(el),
            path    : mkPath(el),
            id      : el.id || null,
            tag     : el.tagName.toLowerCase(),
            classes : Array.from(el.classList),
            text    : (el.textContent || '').trim().slice(0, 100),
            title   : el.getAttribute('title'),
            href    : el.getAttribute('href')
        };
        cleanup();
    }

    function cleanup() {
        if (prev) prev.style.outline = prevOutline;
        document.removeEventListener('mousemove', onMove, true);
        document.removeEventListener('click',     onClick, true);
        if (tip.parentNode)    tip.parentNode.removeChild(tip);
        if (banner.parentNode) banner.parentNode.removeChild(banner);
        delete window.__rcePicker;
    }

    window.__rcePicker = { cleanup: cleanup };
    document.addEventListener('mousemove', onMove, true);
    document.addEventListener('click',     onClick, true);
})();
"""

_PICKER_CLEANUP_JS = (
    "(function(){"
    "  if(window.__rcePicker) window.__rcePicker.cleanup();"
    "  window.__rce_picked = null;"
    "})()"
)


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
        # Selectors: start from defaults, then overlay any saved overrides.
        self.selectors = self._load_selectors()

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
                self._run_setup_wizard(page)
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
                input(
                    "\n"
                    "  ============================================================\n"
                    "  Export finished.  Press Enter to close the browser …\n"
                    "  ============================================================\n"
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
            "  Browser is on the RoundCube page.  Navigate to the inbox\n"
            "  (or the folder you want to export) and make sure it is\n"
            "  fully loaded, then press Enter to verify elements …\n"
            "  ============================================================\n"
        )

    # ── Selector persistence ────────────────────────────────────────────────

    @staticmethod
    def _load_selectors() -> dict:
        """Return DEFAULT_SELECTORS merged with any overrides from selectors.yaml."""
        selectors = dict(DEFAULT_SELECTORS)
        if SELECTORS_FILE.exists():
            try:
                with open(SELECTORS_FILE, "r", encoding="utf-8") as fh:
                    overrides = yaml.safe_load(fh) or {}
                selectors.update({k: v for k, v in overrides.items() if v})
                log.info("Loaded selector overrides from %s", SELECTORS_FILE)
            except Exception as exc:
                log.warning("Could not read %s: %s", SELECTORS_FILE, exc)
        return selectors

    def _save_selectors(self):
        """Persist the current self.selectors dict to selectors.yaml."""
        try:
            with open(SELECTORS_FILE, "w", encoding="utf-8") as fh:
                yaml.dump(self.selectors, fh, default_flow_style=False, allow_unicode=True, sort_keys=False)
            log.info("Saved updated selectors to %s", SELECTORS_FILE)
        except Exception as exc:
            log.warning("Could not save selectors: %s", exc)

    def _ask_for_selector(
        self,
        page,
        key: str,
        description: str,
        pre_click_selector: str = "",
        use_general: bool = False,
    ) -> bool:
        """Ask the user to identify an element by clicking it in the browser.

        If *pre_click_selector* is given the script first clicks that element
        (e.g. to open a dropdown) so the target element is visible before the
        picker is injected.
        """
        if pre_click_selector:
            try:
                pre_el = page.wait_for_selector(pre_click_selector, timeout=5_000)
                pre_el.click()
                time.sleep(0.5)
                log.info("Opened '%s' for element picker.", pre_click_selector)
            except PlaywrightTimeoutError:
                log.warning("Could not open '%s' before picker.", pre_click_selector)
        return self._pick_element_by_click(page, key, description, use_general=use_general)

    def _pick_element_by_click(
        self,
        page,
        key: str,
        description: str,
        use_general: bool = False,
    ) -> bool:
        """Inject a visual hover+click picker and wait for the user to identify
        an element by clicking it in the browser window.

        A red outline follows the cursor and a tooltip shows the element's
        CSS selector.  The *first* element the user clicks is captured;
        ``e.preventDefault()`` ensures the real click action is suppressed.

        Parameters
        ----------
        use_general:
            Use the tag+class selector (no element ID) instead of the
            ID-anchored path selector.  Pass ``True`` for multi-row selectors
            like ``message_rows`` where you want to match *all* similar rows.
        """
        while True:
            try:
                page.evaluate("window.__rce_picked = null;")
                page.evaluate(PICKER_JS)
            except Exception as exc:
                log.warning("Could not inject element picker: %s", exc)
                return False

            print(
                f"\n"
                f"  ╔══════════════════════════════════════════════════════╗\n"
                f"  ║  🎯  ELEMENT PICKER                                 ║\n"
                f"  ╠══════════════════════════════════════════════════════╣\n"
                f"  ║  Identifying: {description[:46]:<46}  ║\n"
                f"  ╠══════════════════════════════════════════════════════╣\n"
                f"  ║  In the browser window:                             ║\n"
                f"  ║    • Hover over elements to see their selector      ║\n"
                f"  ║    • Click the correct element to capture it        ║\n"
                f"  ║  Press Ctrl+C here to skip this element.            ║\n"
                f"  ╚══════════════════════════════════════════════════════╝\n"
            )

            picked = None
            try:
                deadline = time.time() + PICKER_TIMEOUT_S
                while time.time() < deadline:
                    result = page.evaluate("window.__rce_picked")
                    if result:
                        picked = result
                        break
                    time.sleep(0.3)
            except KeyboardInterrupt:
                print("\n  (Skipped by user.)\n")

            # Remove the picker overlay regardless of outcome.
            try:
                page.evaluate(_PICKER_CLEANUP_JS)
            except Exception:
                pass

            if not picked:
                log.warning("No element picked for '%s'.", key)
                return False

            sel = (
                picked.get("general") if use_general else picked.get("path")
            ) or picked.get("path") or ""
            if not sel:
                print("  Could not derive a selector – please try again.")
                continue

            print(
                f"\n  Captured:\n"
                f"    Selector : {sel}\n"
                f"    Tag      : <{picked.get('tag', '')}>\n"
                f"    ID       : {picked.get('id') or '(none)'}\n"
                f"    Classes  : {' '.join(picked.get('classes') or []) or '(none)'}\n"
                f"    Text     : {(picked.get('text') or '')[:60]}\n"
            )

            choice = input(
                "  [Y]es – use this selector\n"
                "  [N]o  – click a different element\n"
                "  [E]dit – enter / tweak the selector manually\n"
                "  Choice [Y/n/e]: "
            ).strip().lower()

            if choice in ("n", "no"):
                continue                            # re-inject picker and try again
            if choice in ("e", "edit"):
                custom = input(f"  Selector for '{key}': ").strip()
                if custom:
                    sel = custom

            self.selectors[key] = sel
            self._save_selectors()
            log.info("Selector '%s' updated to: %s", key, sel)
            return True

    def _run_setup_wizard(self, page):
        """Check all required CSS selectors before export starts.

        For each of the four required elements the script first tries the
        current selector (default or previously saved).  Any that cannot be
        found on the live page trigger the visual picker so the user can click
        the correct element.  All results are saved to ``selectors.yaml``.

        The wizard navigates the browser through the states needed to expose
        each element (opening a sample message, opening the More dropdown, …)
        and ends with a final Enter-to-start prompt.
        """
        print(
            "\n"
            "  ════════════════════════════════════════════════════════\n"
            "  🔍  ELEMENT SETUP CHECK                                \n"
            "  Verifying required CSS selectors on the current page …  \n"
            "  ════════════════════════════════════════════════════════"
        )

        # ── 1. message_rows ─────────────────────────────────────────────────
        rows = page.query_selector_all(self.selectors["message_rows"])
        if rows:
            print(f"  ✓  message rows     – {len(rows)} row(s) found")
        else:
            print("  ✗  message rows     – NOT found")
            print(
                "\n  Make sure the inbox (or the folder you want to export) is\n"
                "  fully loaded in the browser, then click any ONE email row.\n"
            )
            self._pick_element_by_click(
                page,
                "message_rows",
                "any single email row in the message list",
                use_general=True,
            )

        rows = page.query_selector_all(self.selectors["message_rows"])

        # ── Click a sample row so the message toolbar becomes visible ────────
        if rows:
            try:
                rows[0].click()
                page.wait_for_load_state("networkidle")
                time.sleep(0.8)
            except Exception as exc:
                log.warning("Could not click sample row during setup: %s", exc)

        # ── 2. more_button ────────────────────────────────────────────────────
        more_el = page.query_selector(self.selectors["more_button"])
        if more_el:
            print("  ✓  more button      – found")
        else:
            print("  ✗  more button      – NOT found")
            print(
                "\n  An email should be selected in the browser.  Please click\n"
                "  the 'More' or '…' button in the message toolbar.\n"
            )
            self._pick_element_by_click(
                page,
                "more_button",
                "the 'More / …' button in the message toolbar",
            )

        # ── 3. export_item – More dropdown must be open ───────────────────────
        dropdown_open = False
        try:
            more_el2 = page.wait_for_selector(self.selectors["more_button"], timeout=5_000)
            more_el2.click()
            time.sleep(0.5)
            dropdown_open = True
        except PlaywrightTimeoutError:
            pass

        export_el = page.query_selector(self.selectors["export_item"])
        if export_el:
            print("  ✓  export item      – found")
            page.keyboard.press("Escape")
            time.sleep(0.2)
        else:
            print("  ✗  export item      – NOT found")
            if dropdown_open:
                print(
                    "\n  The 'More' dropdown is open in the browser.\n"
                    "  Please click the 'Export' item in the dropdown.\n"
                )
            else:
                print(
                    "\n  Please open the 'More' dropdown in the toolbar,\n"
                    "  then click the 'Export' item.\n"
                )
            self._pick_element_by_click(
                page,
                "export_item",
                "the 'Export' item inside the 'More' dropdown menu",
            )
            page.keyboard.press("Escape")
            time.sleep(0.2)

        # ── 4. next_page – navigate back to inbox to check pagination ─────────
        try:
            page.goto(
                f"{self.url}?_task=mail&_mbox={self.mailbox}",
                wait_until="networkidle",
            )
            time.sleep(self.delay)
        except Exception:
            pass

        next_el = page.query_selector(self.selectors["next_page"])
        if next_el:
            print("  ✓  next page button – found")
        else:
            print(
                "  ✗  next page button – NOT found\n"
                "  (This is fine if the mailbox fits on a single page.)"
            )
            print(
                "\n  If there are multiple pages, please click the 'Next page'\n"
                "  button in the browser.  Press Ctrl+C to skip.\n"
            )
            self._pick_element_by_click(
                page,
                "next_page",
                "the 'Next page' pagination button  (Ctrl+C to skip)",
            )

        print(
            "\n"
            "  ════════════════════════════════════════════════════════\n"
            "  ✓  Setup complete.  Selectors verified/saved.\n"
            "  ════════════════════════════════════════════════════════"
        )
        input("\n  Press Enter to begin the email export …\n")

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

            # Retry loop: if message rows aren't found, ask for a new selector.
            while True:
                rows = page.query_selector_all(self.selectors["message_rows"])
                if rows:
                    break
                log.warning("No message rows found on page %d.", current_page)
                updated = self._ask_for_selector(
                    page,
                    "message_rows",
                    "message list rows (the individual email rows in the inbox table)",
                    use_general=True,
                )
                if not updated:
                    log.warning("Stopping – no message rows found.")
                    return

            log.info("Found %d message(s)", len(rows))

            # Export each message; re-query the DOM after each click because
            # the live NodeList may be invalidated when the view updates.
            for idx in range(len(rows)):
                rows = page.query_selector_all(self.selectors["message_rows"])
                if idx >= len(rows):
                    break
                self._export_single(page, rows[idx], current_page, idx + 1)
                time.sleep(self.delay)

            # ── Advance to the next page ────────────────────────────────────
            next_btn = page.query_selector(self.selectors["next_page"])
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
            # ── Step 1: Click the message row to select/open it ──────────────
            row.click()
            page.wait_for_load_state("networkidle")
            time.sleep(0.5)

            # ── Step 2: Open "More / ..." toolbar dropdown (with retry) ──────
            while True:
                try:
                    more_btn = page.wait_for_selector(
                        self.selectors["more_button"], timeout=10_000
                    )
                    more_btn.click()
                    time.sleep(0.4)
                    break
                except PlaywrightTimeoutError:
                    log.warning("     ✗ 'More' button not found for [%s]", label)
                    updated = self._ask_for_selector(
                        page,
                        "more_button",
                        "'More / ...' toolbar button (opens a dropdown with the Export option)",
                    )
                    if not updated:
                        log.warning("     ✗ Skipping [%s]", label)
                        self.failed += 1
                        return

            # ── Step 3: Click "Export" and capture the download (with retry) ─
            while True:
                try:
                    with page.expect_download(timeout=30_000) as dl_info:
                        export_item = page.wait_for_selector(
                            self.selectors["export_item"], timeout=5_000
                        )
                        export_item.click()
                    download = dl_info.value
                    break
                except PlaywrightTimeoutError:
                    log.warning("     ✗ 'Export' item not found in dropdown for [%s]", label)
                    page.keyboard.press("Escape")
                    time.sleep(0.3)
                    updated = self._ask_for_selector(
                        page,
                        "export_item",
                        "'Export' menu item inside the 'More' dropdown",
                        pre_click_selector=self.selectors["more_button"],
                    )
                    if not updated:
                        log.warning("     ✗ Skipping [%s]", label)
                        self.failed += 1
                        return
                    # Ensure the dropdown is closed, then re-open for the real click.
                    page.keyboard.press("Escape")
                    time.sleep(0.2)
                    # Re-open the More dropdown before retrying
                    try:
                        more_btn = page.wait_for_selector(
                            self.selectors["more_button"], timeout=10_000
                        )
                        more_btn.click()
                        time.sleep(0.4)
                    except PlaywrightTimeoutError:
                        log.warning(
                            "     ✗ Could not re-open 'More' dropdown for [%s]", label
                        )
                        self.failed += 1
                        return

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
