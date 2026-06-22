# SolvBoQ pyRevit extension

Revit-side tools for the SANS 1200 BoQ pipeline. Adds a **SolvBoQ** ribbon tab
with a **Model** panel:

- **Extract Model Data** — export the model to JSON + CSV for mapping.
- **Model Health Check** — comprehensive model-readiness score + fix-it report.
- **BoQ Readiness Check** — focused BoQ-readiness score (quantity/classification).

Each button writes a CSV report, an HTML score card, and a machine-readable
`*.json` you can link in the review app. Layout:

```
SolvBoQ.extension/
  startup.py                         # registers the live-check HTTP routes
  lib/boq_model_checks.py            # shared check logic (used by the routes)
  SolvBoQ.tab/Model.panel/
    Extract Model Data.pushbutton/script.py
    Model Health Check.pushbutton/script.py
    BoQ Readiness Check.pushbutton/script.py
```

## Install (do this once per machine — users don't copy files)

**Option A — shared folder (simplest for a team).** Put `SolvBoQ.extension` on a
shared/local folder, then register the *parent* folder with pyRevit:

```
pyrevit extensions paths add "C:\path\to\folder-containing-the-extension"
```

…or in Revit: **pyRevit → Settings → Custom Extension Directories → add** that
folder, then **Reload**. The **SolvBoQ** tab appears in the ribbon.

**Option B — git (auto-updates).** Push this extension to a git repo and install
it from the pyRevit Extensions manager (or `pyrevit extend ui SolvBoQ <repo-url>`).
pyRevit pulls updates on reload, so fixes reach everyone without re-copying.

## Optional: live checks from the review app (pyRevit Routes)

The web app's **"Run live from Revit"** button calls a small HTTP server pyRevit
runs *inside* Revit (this extension's `startup.py` registers `/boq/...`). To
enable it, once per machine:

```
pyrevit configs routes enable
pyrevit configs routes port 48884     # optional; default is 48884
```

…or **Settings → Routes → enable**, then reload. Revit must be open with the
model loaded. **This is optional** — without it, the app's *upload the report*
path still works for everyone.

## Requirements
- Revit 2020+ with pyRevit 6.x (IronPython 2.7 engine; scripts honour its limits).
- No third-party Python packages — the buttons are self-contained.
