# -*- coding: utf-8 -*-
"""
boq_model_checks.py
===================
Shared, side-effect-free check logic for the SANS 1200 BoQ model checks.

`build_report(doc, kind)` walks the open model and returns the SAME report dict
the pyRevit buttons emit (score, band, breakdown, fix-it, warnings). It's used by
the **pyRevit Routes** server (`startup.py`) so the review app can trigger the
check live — "click in the frontend, runs in Revit" — instead of pressing the
toolbar button.

kind:
  "readiness" -> BoQ-readiness subset (quantity / classification checks only)
  "health"    -> comprehensive model health (adds mark/level hygiene)

DESIGN: pure functions, `doc` passed in, NO module-level Revit access and NO
pyRevit `script`/`output`/`forms` — so it imports cleanly at Revit startup (when
the Routes server registers) without a document open. Deploy to the extension's
`lib/` folder (pyRevit auto-adds it to sys.path).

IRONPYTHON 2.7: no f-strings; version-safe ElementId.
"""

from __future__ import print_function

import datetime

from pyrevit import DB


# ---------------------------------------------------------------------------
# Config (full set; `kind` selects which apply)
# ---------------------------------------------------------------------------

_CATEGORY_NAMES = [
    "OST_StructuralColumns", "OST_StructuralFraming", "OST_StructuralFoundation",
    "OST_StructuralConnectionHandler", "OST_Rebar",
    "OST_Walls", "OST_Floors", "OST_Roofs", "OST_Ceilings", "OST_Doors",
    "OST_Windows", "OST_Stairs", "OST_StairsRailing", "OST_Ramps", "OST_Columns",
    "OST_CurtainWallPanels", "OST_CurtainWallMullions", "OST_Railings",
    "OST_PipeCurves", "OST_PipeFitting", "OST_PipeAccessory", "OST_PlumbingFixtures",
    "OST_DuctCurves", "OST_DuctFitting", "OST_DuctAccessory", "OST_MechanicalEquipment",
    "OST_ElectricalEquipment", "OST_ElectricalFixtures", "OST_LightingFixtures",
    "OST_Sprinklers", "OST_CableTray", "OST_Conduit",
    "OST_Topography", "OST_Site", "OST_Parking", "OST_Entourage", "OST_Furniture",
    "OST_FurnitureSystems", "OST_SpecialityEquipment", "OST_GenericModel",
    "OST_Rooms",
]

TARGET_CATEGORIES = []
for _name in _CATEGORY_NAMES:
    try:
        TARGET_CATEGORIES.append(getattr(DB.BuiltInCategory, _name))
    except AttributeError:
        pass

FT_TO_M = 0.3048
FT2_TO_M2 = FT_TO_M ** 2
FT3_TO_M3 = FT_TO_M ** 3

PENALTY_WEIGHTS = {
    "model_warning": 6, "no_material": 8, "zero_quantity": 6, "missing_level": 4,
    "unjoined_intersection": 5, "in_place_family": 7, "generic_model_category": 3,
    "wrong_phase": 6, "design_option": 6, "missing_mark": 2,
    "duplicate_level": 4, "unused_level": 2,
}
PENALTY_CAPS = {
    "model_warning": 25, "no_material": 25, "zero_quantity": 20, "missing_level": 15,
    "unjoined_intersection": 20, "in_place_family": 20, "generic_model_category": 12,
    "wrong_phase": 20, "design_option": 18, "missing_mark": 12,
    "duplicate_level": 10, "unused_level": 8,
}
ISSUE_LABELS = {
    "model_warning": "Quantity-risk warning", "no_material": "No material assigned",
    "zero_quantity": "Zero volume / area", "missing_level": "No level association",
    "unjoined_intersection": "Overlaps but not joined", "in_place_family": "In-place family",
    "generic_model_category": "Generic Model category", "wrong_phase": "Existing / demolished phase",
    "design_option": "Non-primary design option", "missing_mark": "Missing Mark",
    "duplicate_level": "Duplicate level", "unused_level": "Unused level",
}
ISSUE_FIX = {
    "model_warning": "Resolve the Revit warning (overlap / duplicate / join) - it's a direct double-count risk.",
    "no_material": "Assign a material so quantities can be taken off.",
    "zero_quantity": "Element reports zero volume/area - check geometry / joins.",
    "missing_level": "Associate the element with a Level.",
    "unjoined_intersection": "Overlaps another element but isn't joined - join geometry or resolve the overlap to avoid double-counting.",
    "in_place_family": "Replace the in-place family with a loadable / system family for reliable takeoff.",
    "generic_model_category": "Re-category from Generic Model so it classifies correctly.",
    "wrong_phase": "On an existing/demolition phase - exclude from the new-works BoQ or re-phase.",
    "design_option": "In a non-primary design option - shouldn't be measured; confirm the primary option.",
    "missing_mark": "Assign a Mark for traceability and grouping.",
    "duplicate_level": "Two levels share an elevation - merge or rename so grouping isn't scrambled.",
    "unused_level": "Level has no elements on it - remove or confirm it's intended.",
}
ISSUE_ORDER = [
    "model_warning", "no_material", "zero_quantity", "unjoined_intersection",
    "wrong_phase", "design_option", "in_place_family", "generic_model_category",
    "missing_mark", "duplicate_level", "unused_level", "missing_level",
]
ISSUE_COLORS = {
    "model_warning": "#C62828", "no_material": "#C62828", "zero_quantity": "#F57F17",
    "unjoined_intersection": "#F57F17", "in_place_family": "#C62828",
    "generic_model_category": "#2F5496", "wrong_phase": "#C62828",
    "design_option": "#F57F17", "missing_mark": "#8a93a3",
    "duplicate_level": "#8a93a3", "unused_level": "#8a93a3", "missing_level": "#8a93a3",
}

# Per-element checks that only run in the comprehensive "health" kind.
_HEALTH_ONLY_ELEMENT = set(["missing_level", "missing_mark"])

COUNT_LIKE_CATEGORIES = set([
    "Rooms", "Doors", "Windows", "Furniture", "Furniture Systems",
    "Specialty Equipment", "Mechanical Equipment", "Electrical Equipment",
    "Electrical Fixtures", "Lighting Fixtures", "Plumbing Fixtures", "Sprinklers",
])
LEVEL_EXEMPT_CATEGORIES = set(["Topography", "Site"])
INTERSECTION_CATEGORIES = set([
    "Structural Columns", "Structural Framing", "Structural Foundations",
    "Walls", "Floors", "Roofs", "Ceilings", "Columns",
])
MARK_EXPECTED_CATEGORIES = set([
    "Structural Columns", "Structural Framing", "Structural Foundations",
])
WARNING_KEYWORDS = ["overlap", "duplicate", "identical", "do not intersect", "same place"]

_OVERLAP_EPS = 1e-3
_MAX_INTERSECTION_ELEMENTS = 8000


# ---------------------------------------------------------------------------
# Reading (doc passed in; no module-level Revit access)
# ---------------------------------------------------------------------------

def _eid_int(eid):
    try:
        return int(eid.Value)
    except AttributeError:
        return int(eid.IntegerValue)


def _param_string(element, bip):
    try:
        p = element.get_Parameter(bip)
        if p and p.HasValue:
            s = p.AsString()
            if s:
                return s
            vs = p.AsValueString()
            return vs if vs else ""
    except Exception:
        pass
    return ""


def _family_and_type(doc, element):
    fam = ""
    typ = ""
    try:
        tid = element.GetTypeId()
        te = doc.GetElement(tid) if (tid and tid != DB.ElementId.InvalidElementId) else None
    except Exception:
        te = None
    if te is not None:
        try:
            fam = te.FamilyName or ""
        except Exception:
            fam = ""
        try:
            typ = DB.Element.Name.GetValue(te) or ""
        except Exception:
            try:
                typ = te.Name or ""
            except Exception:
                typ = ""
    return fam, typ


def _level_name(doc, element):
    try:
        lid = element.LevelId
        if lid and lid != DB.ElementId.InvalidElementId:
            lvl = doc.GetElement(lid)
            if lvl is not None:
                return lvl.Name
    except Exception:
        pass
    for bip in (DB.BuiltInParameter.FAMILY_BASE_LEVEL_PARAM,
                DB.BuiltInParameter.FAMILY_LEVEL_PARAM,
                DB.BuiltInParameter.INSTANCE_REFERENCE_LEVEL_PARAM,
                DB.BuiltInParameter.SCHEDULE_LEVEL_PARAM,
                DB.BuiltInParameter.WALL_BASE_CONSTRAINT,
                DB.BuiltInParameter.LEVEL_PARAM):
        try:
            p = element.get_Parameter(bip)
            if p and p.HasValue:
                lid = p.AsElementId()
                if lid and lid != DB.ElementId.InvalidElementId:
                    lvl = doc.GetElement(lid)
                    if lvl is not None:
                        return lvl.Name
        except Exception:
            continue
    return ""


def _level_elevation(doc, element):
    """Elevation (m) of the element's associated level, or "" if none. Used
    downstream to tell ground-bearing slabs from suspended (upper-floor) ones."""
    try:
        lid = element.LevelId
        if lid and lid != DB.ElementId.InvalidElementId:
            lvl = doc.GetElement(lid)
            if lvl is not None and hasattr(lvl, "Elevation"):
                return round(lvl.Elevation * FT_TO_M, 3)
    except Exception:
        pass
    for bip in (DB.BuiltInParameter.FAMILY_BASE_LEVEL_PARAM,
                DB.BuiltInParameter.FAMILY_LEVEL_PARAM,
                DB.BuiltInParameter.INSTANCE_REFERENCE_LEVEL_PARAM,
                DB.BuiltInParameter.SCHEDULE_LEVEL_PARAM,
                DB.BuiltInParameter.WALL_BASE_CONSTRAINT,
                DB.BuiltInParameter.LEVEL_PARAM):
        try:
            p = element.get_Parameter(bip)
            if p and p.HasValue:
                lid = p.AsElementId()
                if lid and lid != DB.ElementId.InvalidElementId:
                    lvl = doc.GetElement(lid)
                    if lvl is not None and hasattr(lvl, "Elevation"):
                        return round(lvl.Elevation * FT_TO_M, 3)
        except Exception:
            continue
    return ""


def _is_in_place(element):
    try:
        sym = getattr(element, "Symbol", None)
        if sym is not None:
            fam = getattr(sym, "Family", None)
            if fam is not None and hasattr(fam, "IsInPlace"):
                return bool(fam.IsInPlace)
    except Exception:
        pass
    return False


def _phase_created(doc, element):
    try:
        pid = element.CreatedPhaseId
        if pid and pid != DB.ElementId.InvalidElementId:
            ph = doc.GetElement(pid)
            if ph is not None:
                return ph.Name
    except Exception:
        pass
    try:
        p = element.get_Parameter(DB.BuiltInParameter.PHASE_CREATED)
        if p and p.HasValue:
            pid = p.AsElementId()
            if pid and pid != DB.ElementId.InvalidElementId:
                ph = doc.GetElement(pid)
                if ph is not None:
                    return ph.Name
    except Exception:
        pass
    return ""


def _is_demolished(element):
    try:
        did = element.DemolishedPhaseId
        if did and did != DB.ElementId.InvalidElementId:
            return True
    except Exception:
        pass
    try:
        p = element.get_Parameter(DB.BuiltInParameter.PHASE_DEMOLISHED)
        if p and p.HasValue:
            did = p.AsElementId()
            return bool(did and did != DB.ElementId.InvalidElementId)
    except Exception:
        pass
    return False


def _design_option_info(element):
    try:
        opt = element.DesignOption
    except Exception:
        opt = None
    if opt is None:
        return "", True
    name = ""
    try:
        name = opt.Name
    except Exception:
        pass
    primary = True
    try:
        p = opt.get_Parameter(DB.BuiltInParameter.OPTION_PRIMARY)
        if p and p.HasValue:
            primary = (p.AsInteger() == 1)
    except Exception:
        pass
    return name, primary


def _bbox_m(element):
    try:
        bb = element.get_BoundingBox(None)
        if bb is None:
            return None
        return {
            "min_m": [bb.Min.X * FT_TO_M, bb.Min.Y * FT_TO_M, bb.Min.Z * FT_TO_M],
            "max_m": [bb.Max.X * FT_TO_M, bb.Max.Y * FT_TO_M, bb.Max.Z * FT_TO_M],
        }
    except Exception:
        return None


def _joined_ids(doc, element):
    ids = []
    try:
        for jid in DB.JoinGeometryUtils.GetJoinedElements(doc, element):
            ids.append(_eid_int(jid))
    except Exception:
        pass
    return ids


def _materials(doc, element):
    rows = []
    try:
        mat_ids = element.GetMaterialIds(False)
    except Exception:
        mat_ids = []
    for mid in mat_ids:
        mat = doc.GetElement(mid)
        if mat is None:
            continue
        try:
            nm = mat.Name
        except Exception:
            nm = ""
        v = 0.0
        a = 0.0
        try:
            v = element.GetMaterialVolume(mid)
        except Exception:
            pass
        try:
            a = element.GetMaterialArea(mid, False)
        except Exception:
            pass
        rows.append({"material_name": nm,
                     "volume_m3": round(v * FT3_TO_M3, 6),
                     "area_m2": round(a * FT2_TO_M2, 6)})
    return rows


def _collect(doc):
    out = []
    for bic in TARGET_CATEGORIES:
        try:
            out.extend(list(DB.FilteredElementCollector(doc)
                            .OfCategory(bic).WhereElementIsNotElementType().ToElements()))
        except Exception:
            continue
    return out


def _record(doc, element):
    cat = ""
    try:
        if element.Category is not None:
            cat = element.Category.Name
    except Exception:
        pass
    fam, typ = _family_and_type(doc, element)
    opt, primary = _design_option_info(element)
    return {
        "element_id": _eid_int(element.Id), "category": cat, "family": fam, "type": typ,
        "level": _level_name(doc, element),
        "level_elevation": _level_elevation(doc, element),
        "mark": _param_string(element, DB.BuiltInParameter.ALL_MODEL_MARK),
        "is_in_place": _is_in_place(element),
        "phase_created": _phase_created(doc, element), "demolished": _is_demolished(element),
        "design_option": opt, "design_option_primary": primary,
        "materials": _materials(doc, element),
        "joined_to_element_ids": _joined_ids(doc, element),
        "bounding_box": _bbox_m(element),
    }


def read_model(doc):
    records = []
    for el in _collect(doc):
        try:
            records.append(_record(doc, el))
        except Exception:
            pass
    return records


# ---------------------------------------------------------------------------
# Checks (shared with the buttons)
# ---------------------------------------------------------------------------

def _find_unjoined_intersections(records):
    items = []
    for rec in records:
        if rec["category"] not in INTERSECTION_CATEGORIES:
            continue
        bb = rec["bounding_box"]
        if not bb:
            continue
        mn = bb["min_m"]
        mx = bb["max_m"]
        items.append({"id": rec["element_id"], "joined": set(rec["joined_to_element_ids"]),
                      "minx": mn[0], "maxx": mx[0], "miny": mn[1], "maxy": mx[1],
                      "minz": mn[2], "maxz": mx[2]})
    flagged = set()
    n = len(items)
    if n > _MAX_INTERSECTION_ELEMENTS:
        return flagged, True
    items.sort(key=lambda d: d["minx"])
    for i in range(n):
        a = items[i]
        j = i + 1
        while j < n and items[j]["minx"] <= a["maxx"] + _OVERLAP_EPS:
            b = items[j]
            ox = min(a["maxx"], b["maxx"]) - max(a["minx"], b["minx"])
            oy = min(a["maxy"], b["maxy"]) - max(a["miny"], b["miny"])
            oz = min(a["maxz"], b["maxz"]) - max(a["minz"], b["minz"])
            if ox > _OVERLAP_EPS and oy > _OVERLAP_EPS and oz > _OVERLAP_EPS:
                if a["id"] not in b["joined"] and b["id"] not in a["joined"]:
                    flagged.add(a["id"])
                    flagged.add(b["id"])
            j += 1
    return flagged, False


def _record_issues(rec, intersection_flagged, kind):
    issues = []
    cat = rec["category"]
    has_mat = bool(rec["materials"])
    measured = cat not in COUNT_LIKE_CATEGORIES

    if measured and not has_mat:
        issues.append("no_material")
    elif has_mat:
        vol = 0.0
        area = 0.0
        for m in rec["materials"]:
            vol += m.get("volume_m3") or 0
            area += m.get("area_m2") or 0
        if measured and vol <= 1e-9 and area <= 1e-9:
            issues.append("zero_quantity")

    if rec.get("is_in_place"):
        issues.append("in_place_family")
    if cat == "Generic Models":
        issues.append("generic_model_category")
    if rec["element_id"] in intersection_flagged:
        issues.append("unjoined_intersection")
    if rec.get("demolished") or ("existing" in (rec.get("phase_created") or "").lower()):
        issues.append("wrong_phase")
    if rec.get("design_option") and not rec.get("design_option_primary"):
        issues.append("design_option")

    # health-only hygiene checks
    if kind == "health":
        if not rec["level"] and cat not in LEVEL_EXEMPT_CATEGORIES:
            issues.append("missing_level")
        if cat in MARK_EXPECTED_CATEGORIES and not rec.get("mark"):
            issues.append("missing_mark")

    return issues


def _analyze_warnings(doc):
    try:
        warns = list(doc.GetWarnings())
    except Exception:
        return 0, 0, []
    total = len(warns)
    relevant = 0
    fixit = []
    for w in warns:
        try:
            desc = w.GetDescriptionText() or ""
        except Exception:
            desc = ""
        if not any(k in desc.lower() for k in WARNING_KEYWORDS):
            continue
        relevant += 1
        ids = []
        try:
            for fid in w.GetFailingElements():
                ids.append(_eid_int(fid))
        except Exception:
            pass
        fixit.append({
            "element_id": ids[0] if ids else "", "category": "(model warning)",
            "family": "", "type": "", "level": "", "mark": "", "issue": "model_warning",
            "location": ("element ids: " + ", ".join(str(i) for i in ids[:10])) if ids else "",
            "suggestion": desc[:240],
        })
    return total, relevant, fixit


def _analyze_levels(doc, used_level_names):
    counts = {}
    fixit = []
    try:
        levels = list(DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements())
    except Exception:
        return counts, fixit

    def lvl_row(lv, issue, elev):
        try:
            nm = lv.Name
        except Exception:
            nm = ""
        return {"element_id": _eid_int(lv.Id), "category": "Levels", "family": "",
                "type": nm, "level": nm, "mark": "", "issue": issue,
                "location": ("elev " + str(elev) + " m") if elev is not None else "",
                "suggestion": ""}

    by_elev = {}
    for lv in levels:
        try:
            elev = round(lv.Elevation * FT_TO_M, 3)
        except Exception:
            elev = None
        by_elev.setdefault(elev, []).append(lv)
    dup = 0
    for elev, group in by_elev.items():
        if elev is None or len(group) < 2:
            continue
        for lv in group:
            dup += 1
            fixit.append(lvl_row(lv, "duplicate_level", elev))
    if dup:
        counts["duplicate_level"] = dup
    unused = 0
    for lv in levels:
        try:
            nm = lv.Name
        except Exception:
            nm = ""
        if nm and nm not in used_level_names:
            unused += 1
            try:
                elev = round(lv.Elevation * FT_TO_M, 3)
            except Exception:
                elev = None
            fixit.append(lvl_row(lv, "unused_level", elev))
    if unused:
        counts["unused_level"] = unused
    return counts, fixit


def _score(counts):
    total_penalty = 0
    breakdown = {}
    for issue, count in counts.items():
        weight = PENALTY_WEIGHTS.get(issue, 0)
        cap = PENALTY_CAPS.get(issue, 100)
        raw = count * weight
        applied = min(raw, cap)
        breakdown[issue] = {"count": count, "weight": weight, "penalty": applied, "capped": raw > cap}
        total_penalty += applied
    score = 100 - total_penalty
    if score < 0:
        score = 0
    return int(round(score)), breakdown


def _band_label(score, kind):
    if kind == "readiness":
        if score >= 85:
            return "BoQ-ready"
        if score >= 65:
            return "Needs attention"
        return "Not BoQ-ready"
    if score >= 85:
        return "Good"
    if score >= 65:
        return "Needs attention"
    return "Poor"


def _location_str(rec):
    parts = []
    if rec["level"]:
        parts.append(rec["level"])
    bb = rec["bounding_box"]
    if bb:
        mn = bb["min_m"]
        mx = bb["max_m"]
        parts.append("({0}, {1}, {2}) m".format(
            round((mn[0] + mx[0]) / 2.0, 2),
            round((mn[1] + mx[1]) / 2.0, 2),
            round((mn[2] + mx[2]) / 2.0, 2)))
    return " | ".join(parts) if parts else "(no location)"


# Columns the mapping step (map_to_boq.py) reads — same shape as the Extract
# button's CSV, flattened one row per material per element.
_EXTRACT_COLUMNS = ["element_id", "category", "family", "type", "level", "level_elevation",
                    "mark", "material_name", "volume_m3", "area_m2", "joined_to_count"]


def build_extract_csv(doc):
    """Walk the model and return the raw extraction as CSV text (the input the
    mapping step expects) — so the app can pull model data live instead of
    asking the user for a file."""
    records = read_model(doc)

    def esc(v):
        s = u"" if v is None else u"{0}".format(v)
        return u'"' + s.replace(u'"', u'""') + u'"'

    lines = [u",".join(_EXTRACT_COLUMNS)]
    for r in records:
        base = [r["element_id"], r["category"], r["family"], r["type"], r["level"], r["level_elevation"], r["mark"]]
        jc = len(r.get("joined_to_element_ids") or [])
        mats = r.get("materials") or []
        if mats:
            for m in mats:
                row = base + [m.get("material_name", ""), m.get("volume_m3", 0),
                              m.get("area_m2", 0), jc]
                lines.append(u",".join(esc(x) for x in row))
        else:
            row = base + [u"", 0.0, 0.0, jc]
            lines.append(u",".join(esc(x) for x in row))
    return u"\n".join(lines)


def build_report(doc, kind="readiness"):
    """Run the checks on `doc` and return the report dict the review app renders.
    kind = 'readiness' (BoQ subset) or 'health' (comprehensive)."""
    kind = "health" if kind == "health" else "readiness"
    records = read_model(doc)

    intersection_flagged, _skipped = _find_unjoined_intersections(records)
    counts = {}
    fixit_rows = []
    used_levels = set()
    elem_flagged = set()
    for rec in records:
        if rec["level"]:
            used_levels.add(rec["level"])
        for issue in _record_issues(rec, intersection_flagged, kind):
            counts[issue] = counts.get(issue, 0) + 1
            elem_flagged.add(rec["element_id"])
            fixit_rows.append({
                "element_id": rec["element_id"], "category": rec["category"],
                "family": rec["family"], "type": rec["type"], "level": rec["level"],
                "mark": rec["mark"], "issue": issue, "location": _location_str(rec),
            })

    warn_total, warn_relevant, warn_fixit = _analyze_warnings(doc)
    if warn_relevant:
        counts["model_warning"] = warn_relevant
        fixit_rows.extend(warn_fixit)

    if kind == "health":
        lvl_counts, lvl_fixit = _analyze_levels(doc, used_levels)
        counts.update(lvl_counts)
        fixit_rows.extend(lvl_fixit)

    score, breakdown = _score(counts)
    band_label = _band_label(score, kind)

    bd = []
    for issue in ISSUE_ORDER:
        b = breakdown.get(issue)
        if not b:
            continue
        bd.append({"issue": issue, "label": ISSUE_LABELS.get(issue, issue),
                   "color": ISSUE_COLORS.get(issue, "#8a93a3"), "count": b["count"],
                   "weight": b["weight"], "penalty": b["penalty"], "capped": b["capped"]})
    fx = []
    for r in fixit_rows:
        fx.append({"element_id": r["element_id"], "category": r["category"],
                   "family": r["family"], "type": r["type"], "level": r["level"],
                   "mark": r["mark"], "issue": r["issue"],
                   "label": ISSUE_LABELS.get(r["issue"], r["issue"]),
                   "location": r.get("location", ""),
                   "suggestion": r.get("suggestion") or ISSUE_FIX.get(r["issue"], "")})

    model_title = ""
    try:
        model_title = doc.Title
    except Exception:
        pass

    return {
        "kind": "model_health_check" if kind == "health" else "boq_readiness_check",
        "model": model_title,
        "generated_at": datetime.datetime.now().isoformat(),
        "score": score, "band_label": band_label,
        "total_elements": len(records),
        "stats": {"issues": sum(counts.values()), "flagged": len(elem_flagged),
                  "clean": len(records) - len(elem_flagged)},
        "warnings": {"total": warn_total, "relevant": warn_relevant},
        "breakdown": bd, "fixit": fx,
    }
