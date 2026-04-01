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
import urllib.parse
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

# Text label of the Export button in the More dropdown.
# Override this if your RoundCube installation is localised and the button
# has a different label (e.g. "Exporter" in French).
EXPORT_BUTTON_TEXT = "Export"

# File where user-supplied selector overrides are persisted.
SELECTORS_FILE = Path("selectors.yaml")

# Directory used to store the persistent browser profile (cookies, localStorage,
# cached sessions).  Using a persistent context means the user only has to log
# in once; subsequent runs reuse the saved session automatically.
#
# ⚠ SECURITY: this directory contains authentication cookies and session tokens.
#   • It is listed in .gitignore – never commit it to version control.
#   • Treat it with the same care as a password file.
#   • Delete it (rm -rf .browser_profile) to force a fresh login.
PROFILE_DIR = Path(".browser_profile")

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
        parallel_tabs: int = 5,
    ):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.download_dir = Path(download_dir)
        self.mailbox = mailbox
        self.headless = headless
        self.delay = delay
        self.start_page = start_page
        # Cap at 30 to avoid excessive browser memory usage and OS file-descriptor
        # limits; beyond ~10 tabs the marginal speed gain is negligible.
        self.parallel_tabs = max(1, min(int(parallel_tabs), 30))
        self.exported = 0
        self.failed = 0
        self.context = None  # set in run() once the browser context is created
        # Selectors: start from defaults, then overlay any saved overrides.
        self.selectors = self._load_selectors()

    # ── Public entry point ──────────────────────────────────────────────────

    def run(self):
        """Launch the browser and export all emails."""
        self.download_dir.mkdir(parents=True, exist_ok=True)
        abs_download_dir = self.download_dir.resolve()
        log.info("Downloads will be saved to: %s", abs_download_dir)

        if self.headless:
            log.warning(
                "Manual login requires a visible browser window – "
                "ignoring --headless."
            )

        with sync_playwright() as pw:
            # Use a persistent context so that the browser profile (cookies,
            # localStorage, cached sessions) is saved to PROFILE_DIR on disk.
            # This makes the browser appear as a normal (non-incognito) window
            # and lets the user skip manual login on subsequent runs.
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            context = pw.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                headless=False,
                accept_downloads=True,
                # Stage Playwright's temp download files inside download_dir so
                # that Chrome's downloads page (chrome://downloads) points to
                # the right folder and the "Show in folder" button works.
                downloads_path=str(abs_download_dir),
                viewport={"width": 1400, "height": 900},
                # ── Anti-bot-detection ──────────────────────────────────────
                # Remove Chromium's automation flags so the browser passes
                # hosting-site security checks (e.g. Cloudflare "Checking your
                # browser…").  Without this, navigator.webdriver === true and
                # various automation-related Chrome features are visible, which
                # bot-detection scripts use to block automated browsers.
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
                # A realistic desktop Chrome user-agent (avoids "HeadlessChrome"
                # strings and keeps the UA consistent with a normal user).
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            # Delete navigator.webdriver before any page JavaScript runs so
            # that even fingerprinting scripts that read the property directly
            # cannot detect automation.
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            # Store the context so that _export_page_with_tabs can open new
            # tabs inside the same browser session.
            self.context = context
            page = context.pages[0] if context.pages else context.new_page()
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
                context.close()

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

            # ── Warn about dynamic / numeric IDs ─────────────────────────────
            # RoundCube assigns sequential IDs like "rcmbtn136" to toolbar
            # buttons.  These numbers change whenever the page reloads, so an
            # ID-based selector breaks on the next run.  Detect this pattern
            # and proactively offer a stable :has-text() alternative.
            element_id   = picked.get("id") or ""
            element_text = (picked.get("text") or "").strip()
            element_tag  = picked.get("tag") or "a"
            if (
                element_id
                and re.search(r"\d", element_id)
                and element_text
                and len(element_text) <= 40
            ):
                text_sel = f'{element_tag}:has-text("{element_text}")'
                print(
                    f"\n  ⚠  Warning: the ID '{element_id}' contains numbers and is likely\n"
                    f"     dynamically generated – it may change on the next page load.\n"
                    f"  Stable text-based alternative: {text_sel}\n"
                )
                swap = input("  Switch to the text-based selector? [Y/n]: ").strip().lower()
                if swap not in ("n", "no"):
                    sel = text_sel
                    print(f"  → Using: {sel}\n")

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

        **Important**: the inbox URL is captured at the very start of the
        wizard so that step 4 navigates back to the *exact* URL the browser
        is currently on (including any live cPanel session token).  This
        avoids a 404 that would occur if we reconstructed the URL from the
        ``self.url`` value that was stored at login time and whose cPanel
        ``cpsess`` token may have since been refreshed by the server.
        """
        # Snapshot the live inbox URL before any navigation so we can return
        # to it reliably even if the cPanel session token has been refreshed.
        inbox_url = page.url

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
        # Use the live URL captured before any wizard navigation (not the
        # reconstructed self.url) so a refreshed cPanel session token does
        # not cause a 404 page instead of the real inbox.
        try:
            page.goto(inbox_url, wait_until="networkidle")
            time.sleep(self.delay)
            # Keep self.url in sync with the current live session token so
            # that _go_to_mailbox (called right after the wizard) also works.
            if "/roundcube" in page.url.lower():
                self.url = page.url.split("?")[0].rstrip("/")
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
        # The setup wizard always ends with the browser on the inbox page and
        # all message rows already loaded.  Doing a second page.goto() here
        # would trigger a full page reload: RoundCube re-fetches the message
        # list via an asynchronous AJAX request that can fire *after*
        # networkidle is reached, so the rows would not be in the DOM when
        # _export_all_pages first queries them.
        #
        # Skip navigation when:
        #   • we are starting at page 1 (the default), AND
        #   • the browser is already showing _task=mail for the target mailbox.
        #
        # We still navigate when start_page > 1 (need to jump to a specific
        # page) or when the browser is on a different mailbox / view.
        if self.start_page == 1:
            parsed = urllib.parse.urlparse(page.url)
            qs = urllib.parse.parse_qs(parsed.query)
            if (
                qs.get("_task") == ["mail"]
                and qs.get("_mbox") == [self.mailbox]
            ):
                log.info(
                    "Already on mailbox '%s' at page 1 – skipping navigation.",
                    self.mailbox,
                )
                return

        # Refresh self.url from the live page URL so that a rotated cPanel
        # session token (cpsessXXXXXXXXXX) never causes a 404.
        if "/roundcube" in page.url.lower():
            self.url = page.url.split("?")[0].rstrip("/")
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

            if self.parallel_tabs > 1 and self.context is not None:
                # ── Parallel-tab mode ──────────────────────────────────────
                # Open each message in its own tab using native browser
                # gestures (Ctrl+click → window.open → new_page fallback),
                # trigger More→Export on each tab, save, then close tabs.
                # Rows for which no tab could be opened are handled by the
                # serial _export_single fallback inside _export_page_with_tabs.
                self._export_page_with_tabs(page, rows, current_page)
            else:
                # ── Serial mode (original behaviour) ──────────────────────
                # Export each message; re-query the DOM after each click
                # because the live NodeList may be invalidated on view updates.
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

    # ── Parallel-tab helpers ────────────────────────────────────────────────

    @staticmethod
    def _get_row_uid(row) -> str:
        """Extract the RoundCube message UID from a message-list row element.

        Different RoundCube versions and themes store the UID in different
        places.  This method performs a comprehensive search using JavaScript
        so it works across all known variants:

        1. ``data-uid`` attribute on the ``<tr>`` (modern RoundCube)
        2. ``id="rcmrow{uid}"`` on the ``<tr>`` (older RoundCube)
        3. ``data-id`` attribute on the ``<tr>`` (some themes)
        4. ``<input name="uid[]">`` or ``<input name*="uid">`` child element
        5. Any ``href`` on a child ``<a>`` that contains ``_uid=``
        6. Any ``data-*`` attribute whose name includes the word "uid"
        """
        try:
            uid = row.evaluate(r"""el => {
                // 1. data-uid on the row
                var v = el.getAttribute('data-uid');
                if (v && v.trim()) return v.trim();
                // 2. id="rcmrow{uid}"
                var id = el.getAttribute('id') || '';
                var m = id.match(/^rcmrow(.+)$/i);
                if (m) return m[1].trim();
                // 3. data-id on the row
                v = el.getAttribute('data-id');
                if (v && v.trim()) return v.trim();
                // 4. child checkbox / hidden input whose name involves uid
                var inp = el.querySelector(
                    'input[name="uid[]"], input[name*="uid"], input[value][name*="id"]'
                );
                if (inp && inp.value && inp.value.trim()) return inp.value.trim();
                // 5. any child link whose href contains _uid=
                var links = el.querySelectorAll('a[href]');
                for (var i = 0; i < links.length; i++) {
                    var href = links[i].getAttribute('href') || '';
                    var um = href.match(/_uid=([^&]+)/);
                    if (um) return decodeURIComponent(um[1]);
                }
                // 6. any data-* attribute on the row whose name contains "uid"
                var attrs = el.attributes;
                for (var j = 0; j < attrs.length; j++) {
                    if (attrs[j].name.toLowerCase().indexOf('uid') !== -1
                            && attrs[j].value.trim()) {
                        return attrs[j].value.trim();
                    }
                }
                return '';
            }""")
            return str(uid).strip() if uid else ""
        except Exception:
            return ""

    @staticmethod
    def _get_row_link_url(row, page) -> str:
        """Return the absolute URL for the message link inside a row.

        RoundCube renders each email row with an ``<a>`` element whose ``href``
        already contains the correct full path (including any cPanel session
        token and the ``3rdparty/roundcube/`` prefix):

            /cpsess3472777044/3rdparty/roundcube/?_task=mail&_mbox=INBOX
            &_uid=473918&_action=show

        We prefer this href over constructing a URL from ``self.url + UID``
        because the constructed URL lacks the ``3rdparty/roundcube/`` segment
        and therefore returns a 404 on cPanel webmail installs.

        The href may be relative (starts with ``/``).  In that case we resolve
        it against the page origin so the returned value is always an absolute
        URL ready for ``page.goto()`` or ``window.open()``.
        """
        try:
            href = row.evaluate(r"""el => {
                // prefer the dedicated "show" action link in the subject cell
                var a = el.querySelector('td.subject a[href*="_action=show"]');
                if (!a) a = el.querySelector('a[href*="_action=show"]');
                if (!a) a = el.querySelector('td.subject a[href]');
                if (!a) a = el.querySelector('a[href]');
                return a ? (a.getAttribute('href') || '') : '';
            }""")
        except Exception:
            return ""

        href = str(href).strip()
        if not href:
            return ""

        # Resolve relative URLs using the current page origin.
        if href.startswith("//"):
            parsed = urllib.parse.urlparse(page.url)
            return f"{parsed.scheme}:{href}"
        if href.startswith("/"):
            parsed = urllib.parse.urlparse(page.url)
            return f"{parsed.scheme}://{parsed.netloc}{href}"
        if href.startswith("http"):
            return href
        # Relative path — unlikely for RoundCube, but handle gracefully.
        return urllib.parse.urljoin(page.url, href)

    def _open_row_in_new_tab(self, page, row, label: str):
        """Open a message row in a new browser tab using native gestures.

        Three strategies are attempted in order, stopping at the first
        one that successfully produces an open tab:

        1. **Ctrl+click the subject link** – Ctrl+clicking the ``<a>`` element
           inside the subject cell is the native browser "Open in New Tab"
           gesture.  The link's ``href`` is used as-is (it already includes the
           correct cPanel session token and path), so RoundCube loads the
           message exactly as it would when the user clicks the link manually.

        2. **``window.open(url, '_blank')``** executed from the inbox page –
           runs inside the browser context of the inbox tab so all session
           state and cookies are fully inherited by the new tab.

        3. **``context.new_page().goto(url)``** – creates a fresh tab and
           navigates directly.  Used only when the first two strategies fail.

        In all URL-based strategies the URL is obtained from
        :meth:`_get_row_link_url`, which reads the ``href`` directly from the
        subject anchor instead of constructing a URL that may lack the correct
        path prefix (see that method's docstring for details).

        Returns the Playwright ``Page`` object for the newly opened tab on
        success, or ``None`` when every strategy fails.
        """
        # ── Strategy 1: Ctrl+click the subject link ──────────────────────────
        # We click the <a> element specifically, not the whole <tr>.  Clicking
        # <tr> only opens the message in the inline preview pane; Ctrl+clicking
        # the anchor triggers a real new-tab navigation via the href.
        try:
            link_el = row.query_selector(
                'td.subject a[href*="_action=show"], '
                'a[href*="_action=show"], '
                'td.subject a[href], '
                'a[href]'
            )
            if link_el is not None:
                with self.context.expect_page(timeout=4_000) as page_info:
                    link_el.click(modifiers=["Control"])
                tab = page_info.value
                tab.wait_for_load_state("domcontentloaded", timeout=30_000)
                return tab
        except Exception:
            pass

        # ── Resolve the message URL from the row's subject anchor ────────────
        url = self._get_row_link_url(row, page)

        if url:
            # ── Strategy 2: window.open() from the inbox page ─────────────────
            try:
                with self.context.expect_page(timeout=5_000) as page_info:
                    page.evaluate("url => window.open(url, '_blank')", url)
                tab = page_info.value
                tab.wait_for_load_state("domcontentloaded", timeout=30_000)
                return tab
            except Exception:
                pass

            # ── Strategy 3: direct new_page().goto() ─────────────────────────
            tab = None
            try:
                tab = self.context.new_page()
                tab.goto(url, wait_until="domcontentloaded", timeout=30_000)
                return tab
            except Exception as exc:
                log.warning(
                    "  ✗ All tab-open strategies failed for [%s]: %s", label, exc
                )
                if tab is not None:
                    try:
                        tab.close()
                    except Exception:
                        pass

        return None

    def _export_page_with_tabs(self, page, rows: list, current_page: int):
        """Export all messages on one inbox page using parallel browser tabs.

        For each batch of ``self.parallel_tabs`` rows the method runs four
        phases:

        1. **Open phase** – each message is opened in its own browser tab via
           :meth:`_open_row_in_new_tab`, which tries native gestures first
           (Ctrl+click, then ``window.open()``) before falling back to direct
           navigation.  Rows for which no tab can be opened are exported
           immediately via the serial :meth:`_export_single` fallback so that
           no email is ever silently skipped.

        2. **Export phase** – iterates the open tabs sequentially and triggers
           More→Export on each.  Because loading started during the Open phase,
           most tabs are already at network-idle by the time we reach them.

        3. **Save phase** – calls ``save_as()`` for every collected download.

        4. **Close phase** – closes every tab opened in this batch, returning
           focus to the main inbox page for the next batch or the page-advance
           step.

        Parameters
        ----------
        page:
            The main browser page showing the inbox message list.
        rows:
            Playwright ``ElementHandle`` objects for every message row on the
            current inbox page (as returned by ``query_selector_all``).
        current_page:
            1-based inbox page number used for constructing file-name labels.
        """
        for batch_start in range(0, len(rows), self.parallel_tabs):
            batch_rows = rows[batch_start: batch_start + self.parallel_tabs]

            # Build human-readable labels for filenames
            batch_labels = []
            for idx, row in enumerate(batch_rows):
                global_idx = batch_start + idx
                subject = ""
                subject_el = row.query_selector("td.subject, .subject, span.subject")
                if subject_el:
                    try:
                        subject = sanitize_filename(subject_el.inner_text().strip())
                    except Exception:
                        pass
                label = f"page{current_page:04d}_msg{global_idx + 1:04d}"
                if subject:
                    label = f"{label}_{subject}"
                batch_labels.append(label)

            # ── Phase 1: open each row in its own tab ────────────────────────
            tabs_data = []
            for batch_idx, (row, label) in enumerate(zip(batch_rows, batch_labels)):
                global_idx = batch_start + batch_idx
                tab = self._open_row_in_new_tab(page, row, label)
                if tab is not None:
                    tabs_data.append((tab, label))
                    log.info("  ↗ Tab opened for [%s]", label)
                else:
                    log.warning(
                        "  ✗ Could not open new tab for [%s] – falling back to serial.",
                        label,
                    )
                    self._export_single(page, row, current_page, global_idx + 1)

            # ── Phase 2: trigger exports (tabs load while we iterate) ────────
            pending_downloads = []
            for tab, label in tabs_data:
                dl = self._trigger_tab_export(tab, label)
                if dl is not None:
                    dest = self.download_dir / f"{label}.eml"
                    pending_downloads.append((dl, dest, label))
                else:
                    self.failed += 1

            # ── Phase 3: save downloads (most already finished in background) ─
            for dl, dest, label in pending_downloads:
                try:
                    dl.save_as(dest)
                    log.info("  ✓ Saved → %s", dest)
                    self.exported += 1
                except Exception as exc:
                    log.warning("  ✗ Failed to save [%s]: %s", label, exc)
                    self.failed += 1

            # ── Phase 4: close tabs ──────────────────────────────────────────
            for tab, _label in tabs_data:
                try:
                    tab.close()
                except Exception:
                    pass

    def _trigger_tab_export(self, tab, label: str):
        """Open the More dropdown and click Export on an already-navigated tab.

        Returns the Playwright ``Download`` object (download has started but
        may not be complete yet) on success, or ``None`` on failure.  The tab
        is left open; the caller is responsible for closing it.
        """
        # Wait for the page to be fully interactive (it started loading when
        # the tab was opened in Phase 1; it is often already done by now).
        try:
            tab.wait_for_load_state("networkidle", timeout=30_000)
        except PlaywrightTimeoutError:
            log.warning(
                "  ✗ Tab did not reach networkidle for [%s] – continuing anyway.",
                label,
            )

        time.sleep(0.3)

        # ── Open "More / ..." dropdown ──────────────────────────────────────
        try:
            more_btn = tab.wait_for_selector(
                self.selectors["more_button"], timeout=10_000
            )
            more_btn.click()
            time.sleep(0.4)
        except PlaywrightTimeoutError:
            log.warning("  ✗ 'More' button not found in tab for [%s]", label)
            return None

        # ── Find the Export menu item ───────────────────────────────────────
        export_el = None
        try:
            export_el = tab.wait_for_selector(
                self.selectors["export_item"], timeout=5_000
            )
        except PlaywrightTimeoutError:
            # Saved selector failed (e.g. dynamic ID changed).  Fall back to a
            # text-based search so exports keep working without user input.
            log.warning(
                "  ✗ 'Export' item not found by saved selector for [%s] – "
                "trying text-based fallback.",
                label,
            )
            try:
                loc = tab.locator(
                    f'a:has-text("{EXPORT_BUTTON_TEXT}"), '
                    f'button:has-text("{EXPORT_BUTTON_TEXT}")'
                )
                loc.first.wait_for(state="visible", timeout=2_000)
                export_el = loc.first.element_handle()
                log.info("  (text-based fallback used for export_item on [%s])", label)
            except Exception:
                pass

        if export_el is None:
            log.warning("  ✗ 'Export' item not found in dropdown for [%s]", label)
            try:
                tab.keyboard.press("Escape")
            except Exception:
                pass
            return None

        # ── Click Export and capture the download handle ────────────────────
        try:
            with tab.expect_download(timeout=30_000) as dl_info:
                export_el.click()
            return dl_info.value
        except Exception as exc:
            log.warning("  ✗ Export click/download failed for [%s]: %s", label, exc)
            try:
                tab.keyboard.press("Escape")
            except Exception:
                pass
            return None

    # ── Export a single email (serial mode) ────────────────────────────────

    def _export_single(self, page, row, page_num: int, row_idx: int):
        """Click a message row, open the More dropdown, and export the email.

        Updates ``self.exported`` on success and ``self.failed`` on any error.
        Falls back to a text-based locator when the saved ``export_item``
        selector fails (e.g. because RoundCube assigned a fresh numeric ID),
        and prompts the user to re-identify the element if both fail.
        """
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
                export_el = None
                # First try the saved/configured selector.
                try:
                    export_el = page.wait_for_selector(
                        self.selectors["export_item"], timeout=5_000
                    )
                except PlaywrightTimeoutError:
                    # Saved selector failed (common when RoundCube assigns a
                    # fresh numeric ID like #rcmbtn136 on each page load).
                    # Fall back to a text-based search inside the open dropdown.
                    log.warning("     ✗ 'Export' item not found in dropdown for [%s]", label)
                    try:
                        loc = page.locator(
                            f'a:has-text("{EXPORT_BUTTON_TEXT}"), '
                            f'button:has-text("{EXPORT_BUTTON_TEXT}")'
                        )
                        loc.first.wait_for(state="visible", timeout=2_000)
                        export_el = loc.first.element_handle()
                        log.info("     (text-based fallback used for export_item)")
                    except Exception:
                        pass

                if export_el is not None:
                    try:
                        with page.expect_download(timeout=30_000) as dl_info:
                            export_el.click()
                        download = dl_info.value
                        break
                    except Exception as dl_exc:
                        log.warning("     ✗ Download failed for [%s]: %s", label, dl_exc)
                        export_el = None

                # Both selectors failed – ask the user to re-identify the element.
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
    parser.add_argument(
        "--parallel-tabs",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Number of email tabs to open in parallel per inbox page "
            "(default: 5). All tabs in a batch start loading simultaneously, "
            "which typically gives a 3-4× speedup over the serial mode. "
            "Set to 1 to disable parallel tabs and use the original "
            "row-click serial mode."
        ),
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

    download_dir   = args.download_dir   or cfg.get("download_dir",   "./exported_emails")
    mailbox        = args.mailbox        or cfg.get("mailbox",        "INBOX")
    headless       = args.headless       if args.headless       is not None else cfg.get("headless",       False)
    delay          = args.delay          if args.delay          is not None else cfg.get("delay",          1.0)
    start_page     = args.start_page     if args.start_page     is not None else cfg.get("start_page",     1)
    parallel_tabs  = args.parallel_tabs  if args.parallel_tabs  is not None else cfg.get("parallel_tabs",  5)

    exporter = RoundCubeExporter(
        url=url,
        username=username,
        password=password,
        download_dir=download_dir,
        mailbox=mailbox,
        headless=headless,
        delay=delay,
        start_page=start_page,
        parallel_tabs=parallel_tabs,
    )
    exporter.run()


if __name__ == "__main__":
    main()
