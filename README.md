# RoundCube Emails Exporter

A Python + Playwright script that bulk-exports **all emails** from
[RoundCube webmail](https://roundcube.net/) as `.eml` files.

RoundCube's UI only provides an "Export" button for **one email at a time**.
This script automates the entire process:

1. Opens a **visible** Chromium browser window at your webmail URL.
2. Waits for **you to log in manually** in that window.
3. Once the URL contains `roundcube`, pauses and **asks you to press Enter** to confirm the inbox is ready.
4. Runs an **Element Setup Wizard** – checks all four required CSS selectors.  Any element that cannot be found triggers the **visual element picker** (see below).
5. Clicks each email in the message list, opens **More (…) → Export**, and saves the `.eml` file.
6. Navigates through every page of the mailbox and repeats.
7. When finished (or on error), **pauses and asks you to press Enter** before closing the browser.

---

## Requirements

- Python 3.8 or newer
- Google Chrome / Chromium (installed automatically by Playwright)

---

## Installation

```bash
pip install -r requirements.txt
playwright install chromium
```

---

## Usage

### Option A – command-line flags

```bash
python exporter.py --url "https://yourserver.com:2096/"
```

A browser window will open.  **Log in manually.**  Once the URL contains
`roundcube` (e.g. `…/3rdparty/roundcube/?_task=mail&_mbox=INBOX`), the script
detects it and pauses.  **Press Enter in the terminal** when the inbox (or
the folder you want to export) is fully loaded.

The **Element Setup Wizard** then runs automatically and verifies that all four
required elements (email rows, More button, Export item, Next page button) are
findable on the current page.  For any element that is missing the script
activates the **visual element picker**: a red banner appears in the browser,
every element you hover over is highlighted with a tooltip showing its CSS
selector, and **clicking** the correct element captures it automatically.  No
DevTools knowledge is needed.

### Option B – config file

Copy the example config, fill in your details, then run:

```bash
cp config.example.yaml config.yaml
# edit config.yaml – set your webmail URL
python exporter.py --config config.yaml
```

### All available options

| Flag | Default | Description |
|------|---------|-------------|
| `--url` | *(required)* | URL to open in the browser (cPanel webmail, direct RoundCube, etc.) |
| `--username` | – | Not used for login (kept for config-file compatibility) |
| `--password` | – | Not used for login (kept for config-file compatibility) |
| `--download-dir` | `./exported_emails` | Folder to save `.eml` files |
| `--mailbox` | `INBOX` | Mailbox/folder to export (`INBOX`, `Sent`, etc.) |
| `--headless` | off | Run browser with no visible window |
| `--delay` | `1.0` | Seconds to wait between actions (increase for slow servers) |
| `--start-page` | `1` | Page to start from – useful for resuming an interrupted run |
| `--config` | – | Path to a YAML config file |

---

## Output

Each email is saved as an `.eml` file:

```
exported_emails/
  page0001_msg0001_Office Order of CPA Regarding Dangerous Goods.eml
  page0001_msg0002_samples req for Rashid textile.eml
  ...
  page0002_msg0001_Turkish Facility Required for Garment Washing.eml
```

A log file `exporter.log` is also written in the current directory.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Browser window doesn't open | Make sure you are **not** running in a headless environment; a display is required for manual login. |
| Element not found – visual picker appears | Hover to find the element, click it to capture.  The selector is saved to `selectors.yaml` and reused on every future run. |
| Selector override not working | Delete or edit `selectors.yaml` in the current directory; the script reloads it on each run. |
| Downloads not starting | Make sure your RoundCube session has permission to export; try increasing `--delay`. |
| Script stops mid-run | Note the last page number in the log and re-run with `--start-page N`. |
| 5-minute login timeout | Log in faster, or increase the `LOGIN_TIMEOUT_MS` constant near the top of `exporter.py`. |

---

## How it works

```
Open browser → wait for manual login (URL must contain '/roundcube')
  → press Enter in terminal
  → Element Setup Wizard runs:
       ✓ / ✗  message rows
       ✓ / ✗  More button     (clicks sample row to expose toolbar)
       ✓ / ✗  Export item     (opens More dropdown to expose item)
       ✓ / ✗  Next page button
       For any ✗: 🎯 visual picker activates in browser
                   hover to inspect → click to capture → saved to selectors.yaml
  → press Enter to start export
  └─ For each page:
       ├─ For each email row in the message list:
       │    ├─ Click the row  (opens the message preview)
       │    ├─ Click the "More (…)" toolbar button
       │    │    └─ (if still not found: picker re-activates, retry)
       │    ├─ Click "Export" in the dropdown
       │    │    └─ (if still not found: picker re-activates with dropdown open, retry)
       │    └─ Save the downloaded .eml file
       └─ Click "Next page" → repeat
  → press Enter in terminal to close the browser
```

**Visual element picker** – when activated a red banner appears at the top of
the browser window.  Hover over any element to see a tooltip with its CSS
selector, then click to capture it.  `e.preventDefault()` is called so the
real click action is not triggered.  After confirming (Y/N/Edit) the selector
is saved to `selectors.yaml`.

**`selectors.yaml`** – stores any selector that differs from the built-in
default.  Loaded automatically on every run, so you only need to identify each
element once.

The browser automation is handled by [Playwright](https://playwright.dev/python/),
which controls a real Chromium instance.  This means it works with any
RoundCube installation – no server-side access or API keys needed.
