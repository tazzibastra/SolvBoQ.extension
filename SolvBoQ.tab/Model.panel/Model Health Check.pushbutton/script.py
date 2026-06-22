# -*- coding: utf-8 -*-
"""
model_health_check.py
=====================
Stage 1.5 of the SANS 1200 BoQ pipeline — the "Model Health Check" button.

Runs inside Revit *before* mapping. Reads the open model, scores how "BoQ-ready"
it is (0-100), shows an HTML score card, and writes a two-section CSV: a QS
summary plus a modeler fix-it list (element id + location).

It NEVER blocks — it only reports. The lookup-table-dependent check (unmapped
combinations) deliberately lives in the Python mapping step, not here, because
this script has no access to the lookup table.

This script is fully self-contained — it carries its own model reader and does
not depend on any shared module (the Extract button likewise reads independently).

IRONPYTHON 2.7 (pyRevit): no f-strings (.format() only), CSV not xlsx,
int(eid.Value) with .IntegerValue fallback.
"""

from __future__ import print_function

import os
import json
import datetime

from pyrevit import revit, DB, forms, script

doc = revit.doc
output = script.get_output()

# Identifies this report kind in the machine-readable JSON (the review app reads
# it when a project is linked).
REPORT_KIND = "model_health_check"


# ---------------------------------------------------------------------------
# Model reading (self-contained — no shared module)
# ---------------------------------------------------------------------------

_CATEGORY_NAMES = [
    # Structural
    "OST_StructuralColumns", "OST_StructuralFraming", "OST_StructuralFoundation",
    "OST_StructuralConnectionHandler", "OST_Rebar",
    # Architectural
    "OST_Walls", "OST_Floors", "OST_Roofs", "OST_Ceilings", "OST_Doors",
    "OST_Windows", "OST_Stairs", "OST_StairsRailing", "OST_Ramps", "OST_Columns",
    "OST_CurtainWallPanels", "OST_CurtainWallMullions", "OST_Railings",
    # MEP
    "OST_PipeCurves", "OST_PipeFitting", "OST_PipeAccessory", "OST_PlumbingFixtures",
    "OST_DuctCurves", "OST_DuctFitting", "OST_DuctAccessory", "OST_MechanicalEquipment",
    "OST_ElectricalEquipment", "OST_ElectricalFixtures", "OST_LightingFixtures",
    "OST_Sprinklers", "OST_CableTray", "OST_Conduit",
    # Site / General
    "OST_Topography", "OST_Site", "OST_Parking", "OST_Entourage", "OST_Furniture",
    "OST_FurnitureSystems", "OST_SpecialityEquipment", "OST_GenericModel",
    # Rooms / Spaces
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


def _element_id_int(eid):
    """Version-safe ElementId -> int (Revit 2024+ uses .Value, older .IntegerValue)."""
    try:
        return int(eid.Value)
    except AttributeError:
        return int(eid.IntegerValue)


def _get_param_string(element, builtin_param):
    try:
        p = element.get_Parameter(builtin_param)
        if p and p.HasValue:
            s = p.AsString()
            if s:
                return s
            vs = p.AsValueString()
            return vs if vs else ""
    except Exception:
        pass
    return ""


def _get_type_element(element):
    try:
        tid = element.GetTypeId()
        if tid and tid != DB.ElementId.InvalidElementId:
            return doc.GetElement(tid)
    except Exception:
        pass
    return None


def _get_family_and_type(element):
    family_name = ""
    type_name = ""
    type_el = _get_type_element(element)
    if type_el is not None:
        try:
            family_name = type_el.FamilyName or ""
        except Exception:
            family_name = ""
        try:
            type_name = DB.Element.Name.GetValue(type_el) or ""
        except Exception:
            try:
                type_name = type_el.Name or ""
            except Exception:
                type_name = ""
    return family_name, type_name


def _get_level_name(element):
    try:
        lid = element.LevelId
        if lid and lid != DB.ElementId.InvalidElementId:
            lvl = doc.GetElement(lid)
            if lvl is not None:
                return lvl.Name
    except Exception:
        pass

    candidate_params = [
        DB.BuiltInParameter.FAMILY_BASE_LEVEL_PARAM,
        DB.BuiltInParameter.FAMILY_LEVEL_PARAM,
        DB.BuiltInParameter.INSTANCE_REFERENCE_LEVEL_PARAM,
        DB.BuiltInParameter.SCHEDULE_LEVEL_PARAM,
        DB.BuiltInParameter.WALL_BASE_CONSTRAINT,
        DB.BuiltInParameter.LEVEL_PARAM,
    ]
    for bip in candidate_params:
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


def _is_in_place(element):
    """True if the element is an in-place family instance."""
    try:
        sym = getattr(element, "Symbol", None)
        if sym is not None:
            fam = getattr(sym, "Family", None)
            if fam is not None and hasattr(fam, "IsInPlace"):
                return bool(fam.IsInPlace)
    except Exception:
        pass
    return False


def _get_phase_created(element):
    """Name of the phase the element was created in ('' if none)."""
    try:
        pid = element.CreatedPhaseId  # Revit 2021+
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
    """True if the element has a 'Phase Demolished' set (gets torn out)."""
    try:
        did = element.DemolishedPhaseId  # Revit 2021+
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
    """(option_name, is_primary). Main-model elements report ('', True)."""
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


def _get_bounding_box_m(element):
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


def _get_joined_element_ids(element):
    ids = []
    try:
        joined = DB.JoinGeometryUtils.GetJoinedElements(doc, element)
        for jid in joined:
            ids.append(_element_id_int(jid))
    except Exception:
        pass
    return ids


def _get_materials(element):
    """Per-material quantities (name, volume m3, area m2)."""
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
            mat_name = mat.Name
        except Exception:
            mat_name = ""
        vol_ft3 = 0.0
        area_ft2 = 0.0
        try:
            vol_ft3 = element.GetMaterialVolume(mid)
        except Exception:
            pass
        try:
            area_ft2 = element.GetMaterialArea(mid, False)
        except Exception:
            pass
        rows.append({
            "material_name": mat_name,
            "volume_m3": round(vol_ft3 * FT3_TO_M3, 6),
            "area_m2": round(area_ft2 * FT2_TO_M2, 6),
        })
    return rows


def _collect_elements():
    collected = []
    for bic in TARGET_CATEGORIES:
        try:
            els = (DB.FilteredElementCollector(doc)
                   .OfCategory(bic)
                   .WhereElementIsNotElementType()
                   .ToElements())
            collected.extend(list(els))
        except Exception:
            continue
    return collected


def _build_record(element):
    cat_name = ""
    try:
        if element.Category is not None:
            cat_name = element.Category.Name
    except Exception:
        pass
    family_name, type_name = _get_family_and_type(element)
    opt_name, opt_primary = _design_option_info(element)
    return {
        "element_id": _element_id_int(element.Id),
        "category": cat_name,
        "family": family_name,
        "type": type_name,
        "level": _get_level_name(element),
        "mark": _get_param_string(element, DB.BuiltInParameter.ALL_MODEL_MARK),
        "is_in_place": _is_in_place(element),
        "phase_created": _get_phase_created(element),
        "demolished": _is_demolished(element),
        "design_option": opt_name,
        "design_option_primary": opt_primary,
        "materials": _get_materials(element),
        "joined_to_element_ids": _get_joined_element_ids(element),
        "bounding_box": _get_bounding_box_m(element),
    }


def read_model(on_skip=None):
    """Walk the model once and return a list of element records."""
    records = []
    for el in _collect_elements():
        try:
            records.append(_build_record(el))
        except Exception as ex:
            if on_skip is not None:
                try:
                    on_skip(_element_id_int(el.Id), ex)
                except Exception:
                    pass
    return records


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Penalty per flagged element, and the maximum any single issue type can subtract
# from the score (so one noisy issue can't sink the whole score on its own).
PENALTY_WEIGHTS = {
    "model_warning": 6,
    "no_material": 8,
    "zero_quantity": 6,
    "missing_level": 4,
    "unjoined_intersection": 5,
    "in_place_family": 7,
    "generic_model_category": 3,
    "wrong_phase": 6,
    "design_option": 6,
    "missing_mark": 2,
    "duplicate_level": 4,
    "unused_level": 2,
}
PENALTY_CAPS = {
    "model_warning": 25,
    "no_material": 25,
    "zero_quantity": 20,
    "missing_level": 15,
    "unjoined_intersection": 20,
    "in_place_family": 20,
    "generic_model_category": 12,
    "wrong_phase": 20,
    "design_option": 18,
    "missing_mark": 12,
    "duplicate_level": 10,
    "unused_level": 8,
}

ISSUE_LABELS = {
    "model_warning": "Quantity-risk warning",
    "no_material": "No material assigned",
    "zero_quantity": "Zero volume / area",
    "missing_level": "No level association",
    "unjoined_intersection": "Overlaps but not joined",
    "in_place_family": "In-place family",
    "generic_model_category": "Generic Model category",
    "wrong_phase": "Existing / demolished phase",
    "design_option": "Non-primary design option",
    "missing_mark": "Missing Mark",
    "duplicate_level": "Duplicate level",
    "unused_level": "Unused level",
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

# Issue order for display (matches a sensible QS priority).
ISSUE_ORDER = [
    "model_warning", "no_material", "zero_quantity", "unjoined_intersection",
    "wrong_phase", "design_option", "in_place_family", "generic_model_category",
    "missing_mark", "duplicate_level", "unused_level", "missing_level",
]
# Accent dot colour per issue (echoes the boq-review-ui palette).
ISSUE_COLORS = {
    "model_warning": "#C62828",
    "no_material": "#C62828",
    "zero_quantity": "#F57F17",
    "unjoined_intersection": "#F57F17",
    "in_place_family": "#C62828",
    "generic_model_category": "#2F5496",
    "wrong_phase": "#C62828",
    "design_option": "#F57F17",
    "missing_mark": "#8a93a3",
    "duplicate_level": "#8a93a3",
    "unused_level": "#8a93a3",
    "missing_level": "#8a93a3",
}

# Categories measured by COUNT (or that legitimately have no material/quantity):
# skip the no_material / zero_quantity checks for these.
COUNT_LIKE_CATEGORIES = set([
    "Rooms", "Doors", "Windows", "Furniture", "Furniture Systems",
    "Specialty Equipment", "Mechanical Equipment", "Electrical Equipment",
    "Electrical Fixtures", "Lighting Fixtures", "Plumbing Fixtures", "Sprinklers",
])
# Categories that legitimately have no Level.
LEVEL_EXEMPT_CATEGORIES = set(["Topography", "Site"])
# Solid, quantity-bearing categories to check for unjoined overlaps (double-count
# risk). Rooms / hosted families are excluded — they overlap by design.
INTERSECTION_CATEGORIES = set([
    "Structural Columns", "Structural Framing", "Structural Foundations",
    "Walls", "Floors", "Roofs", "Ceilings", "Columns",
])
# Categories where an empty Mark is worth flagging (traceability / grouping).
MARK_EXPECTED_CATEGORIES = set([
    "Structural Columns", "Structural Framing", "Structural Foundations",
])
# Substrings that mark a model warning as a quantity / double-count risk.
# (Revit phrases these as e.g. "There are identical instances in the same place",
#  "Elements have duplicate ... values", "highlighted elements are joined but do
#  not intersect", "... overlap ...".)
WARNING_KEYWORDS = ["overlap", "duplicate", "identical", "do not intersect", "same place"]

# Bounding-box overlap tolerance (metres) — ignore mere face-touching.
_OVERLAP_EPS = 1e-3
# Safety guard: skip the pairwise overlap pass above this many eligible elements.
_MAX_INTERSECTION_ELEMENTS = 8000

# Score bands
_GREEN = "#2E7D32"
_AMBER = "#F57F17"
_RED = "#C62828"


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _find_unjoined_intersections(records):
    """Return a set of element_ids that overlap another solid element (by
    bounding box) without being joined to it. Sweep-and-prune on X so it isn't
    a full O(n^2) on spread-out models. Returns (flagged_set, skipped_bool)."""
    items = []
    for rec in records:
        if rec["category"] not in INTERSECTION_CATEGORIES:
            continue
        bb = rec["bounding_box"]
        if not bb:
            continue
        mn = bb["min_m"]
        mx = bb["max_m"]
        items.append({
            "id": rec["element_id"],
            "joined": set(rec["joined_to_element_ids"]),
            "minx": mn[0], "maxx": mx[0],
            "miny": mn[1], "maxy": mx[1],
            "minz": mn[2], "maxz": mx[2],
        })

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


def _record_issues(rec, intersection_flagged):
    """List the issue keys that apply to one element record."""
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

    if not rec["level"] and cat not in LEVEL_EXEMPT_CATEGORIES:
        issues.append("missing_level")
    if rec.get("is_in_place"):
        issues.append("in_place_family")
    if cat == "Generic Models":
        issues.append("generic_model_category")
    if rec["element_id"] in intersection_flagged:
        issues.append("unjoined_intersection")

    # Phase: demolished or modelled on an "existing" phase shouldn't price into
    # a new-works BoQ.
    if rec.get("demolished") or ("existing" in (rec.get("phase_created") or "").lower()):
        issues.append("wrong_phase")
    # Design option: non-primary options are alternatives, not measured work.
    if rec.get("design_option") and not rec.get("design_option_primary"):
        issues.append("design_option")
    # Mark missing on categories where it carries traceability/grouping.
    if cat in MARK_EXPECTED_CATEGORIES and not rec.get("mark"):
        issues.append("missing_mark")

    return issues


def _analyze_warnings():
    """Count model warnings and flag the quantity-relevant ones. Revit has already
    computed overlaps / duplicate instances / bad joins for us — more reliable than
    the bounding-box heuristic. Returns (total, relevant, fixit_rows)."""
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
                ids.append(_element_id_int(fid))
        except Exception:
            pass
        fixit.append({
            "element_id": ids[0] if ids else "",
            "category": "(model warning)",
            "family": "", "type": "", "level": "", "mark": "",
            "issue": "model_warning",
            "location": ("element ids: " + ", ".join(str(i) for i in ids[:10])) if ids else "",
            "suggestion": desc[:240],
        })
    return total, relevant, fixit


def _level_fixit(lv, issue, elev):
    try:
        nm = lv.Name
    except Exception:
        nm = ""
    return {
        "element_id": _element_id_int(lv.Id),
        "category": "Levels", "family": "", "type": nm, "level": nm, "mark": "",
        "issue": issue,
        "location": ("elev " + str(elev) + " m") if elev is not None else "",
        "suggestion": "",
    }


def _analyze_levels(used_level_names):
    """Flag duplicate (same-elevation) and unused levels — they scramble grouping.
    Returns (counts_dict, fixit_rows)."""
    counts = {}
    fixit = []
    try:
        levels = list(DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements())
    except Exception:
        return counts, fixit

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
            fixit.append(_level_fixit(lv, "duplicate_level", elev))
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
            fixit.append(_level_fixit(lv, "unused_level", elev))
    if unused:
        counts["unused_level"] = unused

    return counts, fixit


def _score(counts):
    """0-100 weighted score: each issue type subtracts min(count*weight, cap)."""
    total_penalty = 0
    breakdown = {}
    for issue, count in counts.items():
        weight = PENALTY_WEIGHTS.get(issue, 0)
        cap = PENALTY_CAPS.get(issue, 100)
        raw = count * weight
        applied = min(raw, cap)
        breakdown[issue] = {"count": count, "weight": weight,
                            "penalty": applied, "capped": raw > cap}
        total_penalty += applied
    score = 100 - total_penalty
    if score < 0:
        score = 0
    return int(round(score)), breakdown


def _band(score):
    if score >= 85:
        return _GREEN, "Good"
    if score >= 65:
        return _AMBER, "Needs attention"
    return _RED, "Poor"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _location_str(rec):
    parts = []
    if rec["level"]:
        parts.append(rec["level"])
    bb = rec["bounding_box"]
    if bb:
        mn = bb["min_m"]
        mx = bb["max_m"]
        cx = round((mn[0] + mx[0]) / 2.0, 2)
        cy = round((mn[1] + mx[1]) / 2.0, 2)
        cz = round((mn[2] + mx[2]) / 2.0, 2)
        parts.append("({0}, {1}, {2}) m".format(cx, cy, cz))
    return " | ".join(parts) if parts else "(no location)"


def _esc(v):
    s = u"" if v is None else u"{0}".format(v)
    s = s.replace(u'"', u'""')
    return u'"{0}"'.format(s)


def _write_report_csv(path, model_title, score, band_label, total_elements,
                      breakdown, fixit_rows):
    """Two-section CSV: QS summary then the modeler fix-it list."""
    lines = []

    def row(cells):
        lines.append(u",".join(_esc(c) for c in cells))

    # --- Section 1: summary (QS) ---
    row([u"SANS BoQ - Model Health Check"])
    row([u"Model", model_title])
    row([u"Generated", datetime.datetime.now().isoformat()])
    row([u"Score", score, band_label])
    row([u"Elements analysed", total_elements])
    row([])
    row([u"ISSUE SUMMARY (QS)"])
    row([u"issue", u"elements", u"weight", u"penalty", u"capped"])
    for issue in ISSUE_ORDER:
        b = breakdown.get(issue)
        if not b:
            continue
        row([ISSUE_LABELS.get(issue, issue), b["count"], b["weight"],
             b["penalty"], u"yes" if b["capped"] else u"no"])
    row([])

    # --- Section 2: fix-it list (modeler) ---
    row([u"FIX-IT LIST (MODELER)"])
    row([u"element_id", u"category", u"family", u"type", u"level", u"mark",
         u"issue", u"location", u"suggestion"])
    for r in fixit_rows:
        suggestion = r.get("suggestion") or ISSUE_FIX.get(r["issue"], "")
        row([r["element_id"], r["category"], r["family"], r["type"], r["level"],
             r["mark"], ISSUE_LABELS.get(r["issue"], r["issue"]),
             r["location"], suggestion])

    with open(path, "w") as f:
        text = u"\n".join(lines)
        f.write(text.encode("utf-8") if str is bytes else text)


def _report_dict(model_title, score, band_label, stats, warn_total, warn_relevant,
                 breakdown, fixit_rows):
    """Machine-readable report the review app renders when a project is linked."""
    bd = []
    for issue in ISSUE_ORDER:
        b = breakdown.get(issue)
        if not b:
            continue
        bd.append({
            "issue": issue, "label": ISSUE_LABELS.get(issue, issue),
            "color": ISSUE_COLORS.get(issue, "#8a93a3"),
            "count": b["count"], "weight": b["weight"],
            "penalty": b["penalty"], "capped": b["capped"],
        })
    fx = []
    for r in fixit_rows:
        fx.append({
            "element_id": r["element_id"], "category": r["category"],
            "family": r["family"], "type": r["type"], "level": r["level"],
            "mark": r["mark"], "issue": r["issue"],
            "label": ISSUE_LABELS.get(r["issue"], r["issue"]),
            "location": r.get("location", ""),
            "suggestion": r.get("suggestion") or ISSUE_FIX.get(r["issue"], ""),
        })
    return {
        "kind": REPORT_KIND,
        "model": model_title,
        "generated_at": datetime.datetime.now().isoformat(),
        "score": score,
        "band_label": band_label,
        "total_elements": stats.get("total", 0),
        "stats": {
            "issues": stats.get("issues", 0),
            "flagged": stats.get("flagged", 0),
            "clean": stats.get("clean", 0),
        },
        "warnings": {"total": warn_total, "relevant": warn_relevant},
        "breakdown": bd,
        "fixit": fx,
    }


def _chip(value, label):
    return (
        u"<div style='flex:1;border:1px solid #e1e6ed;border-radius:8px;"
        u"padding:10px 14px;background:#f7f9fc;'>"
        u"<div style='font-size:22px;font-weight:600;color:#1a2330;'>{0}</div>"
        u"<div style='font-size:11px;color:#8a93a3;text-transform:uppercase;"
        u"letter-spacing:0.03em;'>{1}</div></div>"
    ).format(value, label)


def _score_card_html(score, color, band_label, stats, breakdown):
    """A score card styled to match the boq-review-ui (gradient hero, summary
    chips, clean issue rows). All inline styles — renders in the pyRevit output
    window. No literal {} braces, so .format() stays happy."""

    # Issue rows
    rows = []
    for issue in ISSUE_ORDER:
        b = breakdown.get(issue)
        if not b or b["count"] == 0:
            continue
        rows.append((
            u"<div style='display:flex;align-items:center;gap:12px;padding:10px 0;"
            u"border-top:1px solid #eef1f6;'>"
            u"<span style='width:9px;height:9px;border-radius:50%;background:{0};"
            u"display:inline-block;flex:0 0 auto;'></span>"
            u"<span style='flex:1;color:#1a2330;font-size:13px;'>{1}</span>"
            u"<span style='color:#8a93a3;font-size:12px;'>{2} element{3}</span>"
            u"<span style='color:#C62828;font-size:13px;font-weight:600;"
            u"min-width:70px;text-align:right;'>-{4}{5}</span></div>"
        ).format(
            ISSUE_COLORS.get(issue, "#8a93a3"), ISSUE_LABELS.get(issue, issue),
            b["count"], u"s" if b["count"] != 1 else u"",
            b["penalty"], u" (capped)" if b["capped"] else u""))

    if rows:
        issues_html = (
            u"<div style='font-size:11px;color:#8a93a3;text-transform:uppercase;"
            u"letter-spacing:0.04em;margin:6px 0 0;'>Issues by type</div>"
            + u"".join(rows))
    else:
        issues_html = (
            u"<div style='padding:18px;text-align:center;color:#2E7D32;"
            u"font-weight:600;'>No issues found - model looks BoQ-ready.</div>")

    chips_html = (
        u"<div style='display:flex;gap:10px;margin-bottom:6px;'>"
        + _chip(stats["issues"], u"Issues found")
        + _chip(stats["flagged"], u"Elements flagged")
        + _chip(stats["clean"], u"Clean elements")
        + u"</div>")

    hero_html = (
        u"<div style='background:linear-gradient(135deg,#2f5496,#1e3a6b);"
        u"border-radius:14px 14px 0 0;padding:22px 24px;color:#fff;'>"
        u"<div style='display:flex;justify-content:space-between;align-items:flex-start;'>"
        u"<div><div style='font-size:11px;letter-spacing:0.08em;text-transform:uppercase;"
        u"opacity:0.75;'>Model health check</div>"
        u"<div style='font-size:48px;font-weight:700;line-height:1.05;'>{0}"
        u"<span style='font-size:22px;opacity:0.6;'>/100</span></div>"
        u"<span style='display:inline-block;margin-top:4px;padding:3px 12px;"
        u"border-radius:99px;background:{1};color:#fff;font-size:12px;font-weight:600;'>{2}</span>"
        u"</div>"
        u"<div style='text-align:right;'>"
        u"<div style='font-size:13px;font-weight:600;max-width:230px;word-break:break-word;'>{3}</div>"
        u"<div style='font-size:12px;opacity:0.75;margin-top:2px;'>{4} elements analysed</div>"
        u"</div></div>"
        u"<div style='height:8px;border-radius:99px;background:rgba(255,255,255,0.25);"
        u"margin-top:16px;'><div style='height:8px;width:{0}%;border-radius:99px;"
        u"background:{1};'></div></div></div>"
    ).format(score, color, band_label, stats["model"], stats["total"])

    body_html = (
        u"<div style='border:1px solid #e1e6ed;border-top:none;"
        u"border-radius:0 0 14px 14px;padding:18px 24px;'>"
        + chips_html + issues_html + u"</div>")

    return (u"<div style=\"font-family:'Segoe UI',Arial,sans-serif;max-width:640px;\">"
            + hero_html + body_html + u"</div>")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _candidate_dirs():
    """Ordered list of places to try writing the report. The model folder first
    (handy — report sits with the model), then user-writable fallbacks. Read-only
    install locations (e.g. Revit's bundled Samples under Program Files) are
    skipped so the write doesn't fail."""
    dirs = []
    try:
        if doc.PathName:
            d = os.path.dirname(doc.PathName)
            if d and "program files" not in d.lower():
                dirs.append(d)
    except Exception:
        pass
    home = os.path.expanduser("~")
    dirs.append(os.path.join(home, "Documents"))
    dirs.append(os.path.join(home, "Desktop"))
    tmp = os.environ.get("TEMP") or os.environ.get("TMP")
    if tmp:
        dirs.append(tmp)
    dirs.append(home)
    return dirs


def _write_report_with_fallback(fname, *args):
    """Try each candidate dir until the CSV writes. Returns the path written."""
    last_err = None
    for d in _candidate_dirs():
        if not d or not os.path.isdir(d):
            continue
        path = os.path.join(d, fname)
        try:
            _write_report_csv(path, *args)
            return path
        except Exception as ex:
            last_err = ex
            continue
    raise last_err if last_err else IOError("No writable location found for the report")


def _safe_title(title):
    out = []
    for ch in (title or "model"):
        out.append(ch if (ch.isalnum() or ch in "-_") else "_")
    return "".join(out)


def main():
    output.print_md("# Model health check")

    records = read_model(
        on_skip=lambda eid, ex: output.print_md("Skipped element {0}: {1}".format(eid, ex)),
    )
    if not records:
        forms.alert("No elements found in the target categories — nothing to check.",
                    exitscript=True)

    intersection_flagged, intersection_skipped = _find_unjoined_intersections(records)
    if intersection_skipped:
        output.print_md("> _Overlap check skipped: more than {0} solid elements "
                        "(kept the check fast)._".format(_MAX_INTERSECTION_ELEMENTS))

    counts = {}
    fixit_rows = []
    used_levels = set()
    elem_flagged = set()  # distinct elements with at least one element-level issue
    for rec in records:
        if rec["level"]:
            used_levels.add(rec["level"])
        for issue in _record_issues(rec, intersection_flagged):
            counts[issue] = counts.get(issue, 0) + 1
            elem_flagged.add(rec["element_id"])
            fixit_rows.append({
                "element_id": rec["element_id"],
                "category": rec["category"],
                "family": rec["family"],
                "type": rec["type"],
                "level": rec["level"],
                "mark": rec["mark"],
                "issue": issue,
                "location": _location_str(rec),
            })

    # --- model-level checks (warnings + levels) ---
    warn_total, warn_relevant, warn_fixit = _analyze_warnings()
    if warn_relevant:
        counts["model_warning"] = warn_relevant
        fixit_rows.extend(warn_fixit)

    level_counts, level_fixit = _analyze_levels(used_levels)
    counts.update(level_counts)
    fixit_rows.extend(level_fixit)

    score, breakdown = _score(counts)
    color, band_label = _band(score)

    # --- score card (styled to match the boq-review-ui) ---
    stats = {
        "total": len(records),
        "issues": sum(counts.values()),
        "flagged": len(elem_flagged),
        "clean": len(records) - len(elem_flagged),
        "model": doc.Title,
    }
    output.print_html(_score_card_html(score, color, band_label, stats, breakdown))
    output.print_md("**Model warnings:** {0} total, {1} quantity-relevant "
                    "(overlaps / duplicates / bad joins)."
                    .format(warn_total, warn_relevant))

    # --- write the two-section CSV + clickable link ---
    fname = "model_health_{0}_{1}.csv".format(
        _safe_title(doc.Title),
        datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    try:
        csv_path = _write_report_with_fallback(
            fname, doc.Title, score, band_label, len(records), breakdown, fixit_rows)
        file_url = "file:///" + csv_path.replace("\\", "/")
        output.print_md("**Report:** [{0}]({1})".format(fname, file_url))
        output.print_md("_Saved to: `{0}`_".format(csv_path))

        # Machine-readable JSON next to the CSV — upload this to link a project
        # in the review app (it renders the full results on screen).
        json_path = os.path.splitext(csv_path)[0] + ".json"
        report = _report_dict(doc.Title, score, band_label, stats,
                              warn_total, warn_relevant, breakdown, fixit_rows)
        try:
            with open(json_path, "w") as jf:
                text = json.dumps(report, indent=2)
                jf.write(text.encode("utf-8") if str is bytes else text)
            output.print_md("_App report (link this in a project): `{0}`_".format(json_path))
        except Exception as jex:
            output.print_md("Could not write the JSON report: {0}".format(jex))
    except Exception as ex:
        output.print_md("Could not write the report CSV anywhere: {0}".format(ex))

    # --- never blocks; the score is stamped into the report for downstream use ---
    output.print_md("---")
    output.print_md("Health check is advisory only — it never blocks extraction or "
                    "mapping. Score **{0}/100 ({1})** recorded in the report above."
                    .format(score, band_label))


if __name__ == "__main__":
    main()
