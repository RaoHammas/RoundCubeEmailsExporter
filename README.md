# RoundCube Emails Exporter

A Python + Playwright script that bulk-exports **all emails** from
[RoundCube webmail](https://roundcube.net/) as `.eml` files.

RoundCube's UI only provides an "Export" button for **one email at a time**.
This script automates the entire process:

1. Opens a Chromium browser and logs into your RoundCube instance.
2. Clicks each email in the message list.
3. Opens the **More (…) → Export** dropdown item.
4. Saves the downloaded `.eml` file to a local folder.
5. Navigates through every page of the mailbox and repeats.

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
python exporter.py \
  --url      "https://yourserver.com/roundcube/" \
  --username "user@example.com" \
  --password "yourpassword"
```

The `--url` also accepts **cPanel webmail** URLs (port 2096 or 2095).
The script will automatically log in via cPanel, select RoundCube if a
webmail-client selection page appears, and then export emails as usual:

```bash
python exporter.py \
  --url      "https://yourserver.com:2096" \
  --username "user@example.com" \
  --password "yourpassword"
```

### Option B – config file

Copy the example config, fill in your details, then run:

```bash
cp config.example.yaml config.yaml
# edit config.yaml with your URL / credentials
python exporter.py --config config.yaml
```

### All available options

| Flag | Default | Description |
|------|---------|-------------|
| `--url` | *(required)* | RoundCube base URL (also works with cPanel webmail URLs on port 2096/2095) |
| `--username` | *(required)* | Login username / email address |
| `--password` | *(required)* | Login password |
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
| Export button not found | Run **without** `--headless` to see what the browser sees; adjust the `SELECTORS` dictionary in `exporter.py` to match your RoundCube version. |
| Downloads not starting | Make sure your RoundCube session has permission to export; try increasing `--delay`. |
| Script stops mid-run | Note the last page number in the log and re-run with `--start-page N`. |
| Login fails | Check your URL ends with `/` and that credentials are correct. |

---

## How it works

```
Login → Navigate to mailbox
  └─ For each page:
       ├─ For each email row in the message list:
       │    ├─ Click the row  (opens the message preview)
       │    ├─ Click the "More (…)" toolbar button
       │    ├─ Click "Export" in the dropdown
       │    └─ Save the downloaded .eml file
       └─ Click "Next page" → repeat
```

The browser automation is handled by [Playwright](https://playwright.dev/python/),
which controls a real Chromium instance.  This means it works with any
RoundCube installation – no server-side access or API keys needed.
