# RoundCube Emails Exporter

A Python + Playwright script that bulk-exports **all emails** from
[RoundCube webmail](https://roundcube.net/) as `.eml` files.

RoundCube's UI only provides an "Export" button for **one email at a time**.
This script automates the entire process:

1. Opens a **visible** Chromium browser window at your webmail URL.
2. Waits for **you to log in manually** in that window.
3. Once the URL contains `roundcube` (i.e. you have reached the RoundCube inbox), pauses and **asks you to press Enter** in the terminal to confirm the page is ready.
4. Clicks each email in the message list, opens **More (…) → Export**, and saves the `.eml` file.
5. Navigates through every page of the mailbox and repeats.
6. When finished (or on error), **pauses again and asks you to press Enter** before closing the browser so you can inspect the final state.

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
the folder you want to export) is fully loaded — the export then starts
automatically.

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
| Export button not found | The script will interactively ask you for the correct CSS selector and save it to `selectors.yaml` for future runs. |
| Selector override not working | Delete or edit `selectors.yaml` in the current directory; the script reloads it on each run. |
| Downloads not starting | Make sure your RoundCube session has permission to export; try increasing `--delay`. |
| Script stops mid-run | Note the last page number in the log and re-run with `--start-page N`. |
| 5-minute login timeout | Log in faster, or increase the `LOGIN_TIMEOUT_MS` constant near the top of `exporter.py`. |

---

## How it works

```
Open browser → wait for manual login (URL must contain '/roundcube')
  → press Enter in terminal to confirm page is ready
  └─ For each page:
       ├─ For each email row in the message list:
       │    ├─ Click the row  (opens the message preview)
       │    ├─ Click the "More (…)" toolbar button
       │    │    └─ (if not found: prompt for CSS selector, save to selectors.yaml, retry)
       │    ├─ Click "Export" in the dropdown
       │    │    └─ (if not found: prompt for CSS selector, save to selectors.yaml, retry)
       │    └─ Save the downloaded .eml file
       └─ Click "Next page" → repeat
  → press Enter in terminal to close the browser
```

**`selectors.yaml`** – if the script can't find an element it will ask you for
the correct CSS selector (inspect with browser DevTools / F12), save it to
`selectors.yaml` in the current directory, and retry.  The file is loaded
automatically on every subsequent run so you only need to supply a selector once.

The browser automation is handled by [Playwright](https://playwright.dev/python/),
which controls a real Chromium instance.  This means it works with any
RoundCube installation – no server-side access or API keys needed.
