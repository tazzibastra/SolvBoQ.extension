# -*- coding: utf-8 -*-
"""
startup.py  (pyRevit extension startup — Routes API)
====================================================
Registers the live model-check endpoints on pyRevit's local HTTP server so the
review app can run them live ("click in the frontend, runs in Revit").

ENDPOINTS (http://localhost:48884/boq/...)
    GET /boq/ping       -> {"ok": true, "model": "<active doc title>"}
    GET /boq/readiness  -> BoQ-readiness report JSON
    GET /boq/health     -> comprehensive model-health report JSON

DEPLOY: this sits at the extension root; `boq_model_checks.py` is in `lib/`.
Enable pyRevit Routes (Settings > Routes / `pyrevit configs routes enable`) and
reload pyRevit.

DIAGNOSTICS: every startup step is appended to
`~/Documents/solvboq_routes_startup.log` so we can see exactly where (if) route
registration fails — pyRevit's `startup.py` swallows errors quietly otherwise.
"""

import os
import sys
import traceback
import datetime

_LOG = os.path.join(os.path.expanduser("~"), "Documents", "solvboq_routes_startup.log")


def _log(msg):
    try:
        with open(_LOG, "a") as f:
            f.write("[{0}] {1}\n".format(datetime.datetime.now().isoformat(), msg))
    except Exception:
        pass


_log("=== startup.py invoked ===")

# Defensively make our lib/ importable (pyRevit usually does this already).
try:
    _here = os.path.dirname(os.path.abspath(__file__))
    _lib = os.path.join(_here, "lib")
    if os.path.isdir(_lib) and _lib not in sys.path:
        sys.path.append(_lib)
        _log("added lib to sys.path: " + _lib)
    else:
        _log("lib path: " + _lib + " (on path already? " + str(_lib in sys.path) + ", exists? " + str(os.path.isdir(_lib)) + ")")
except Exception:
    _log("could not resolve __file__:\n" + traceback.format_exc())

try:
    from pyrevit import routes
    _log("imported pyrevit.routes")

    import boq_model_checks as checks
    _log("imported boq_model_checks")

    api = routes.API("boq")
    _log("created routes.API('boq')")

    def _active_doc(uiapp):
        """Resolve the active document robustly: prefer injected uiapp, else
        fall back to pyRevit's active doc."""
        try:
            if uiapp is not None:
                return uiapp.ActiveUIDocument.Document
        except Exception:
            pass
        from pyrevit import revit
        return revit.doc

    def _report(uiapp, kind):
        try:
            doc = _active_doc(uiapp)
            if doc is None:
                return routes.make_response(
                    data={"error": "No active document. Open a model in Revit."}, status=409)
            return routes.make_response(data=checks.build_report(doc, kind))
        except Exception:
            return routes.make_response(data={"error": traceback.format_exc()}, status=500)

    @api.route("/ping", methods=["GET"])
    def ping(uiapp=None):
        try:
            doc = _active_doc(uiapp)
            return routes.make_response(data={"ok": True, "model": doc.Title if doc is not None else None})
        except Exception:
            return routes.make_response(data={"ok": False, "error": traceback.format_exc()}, status=500)

    @api.route("/extract", methods=["GET"])
    def extract(uiapp=None):
        try:
            doc = _active_doc(uiapp)
            if doc is None:
                return routes.make_response(
                    data={"error": "No active document. Open a model in Revit."}, status=409)
            return routes.make_response(data={"csv": checks.build_extract_csv(doc),
                                              "model": doc.Title})
        except Exception:
            return routes.make_response(data={"error": traceback.format_exc()}, status=500)

    @api.route("/readiness", methods=["GET"])
    def readiness(uiapp=None):
        return _report(uiapp, "readiness")

    @api.route("/health", methods=["GET"])
    def health(uiapp=None):
        return _report(uiapp, "health")

    _log("registered routes: /ping /extract /readiness /health  -- DONE")

except Exception:
    _log("ROUTE REGISTRATION FAILED:\n" + traceback.format_exc())
