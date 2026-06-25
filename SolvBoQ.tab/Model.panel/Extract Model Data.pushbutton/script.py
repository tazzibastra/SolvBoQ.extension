# -*- coding: utf-8 -*-
"""
extract_model_data.py
=====================
pyRevit extraction script - Stage 1 of the SANS 1200 BoQ pipeline.
(The "Extract Model Data" button.)

WHAT IT DOES
    Walks ALL model elements in the open Revit model and pulls out the
    RAW geometric facts, with full traceability, into JSON + CSV.
    It does NOT do any SANS measurement, mapping, or deduction. That is
    deliberate: this stage produces clean, auditable inputs. Everything that
    requires a rule or a judgement happens downstream where it can be logged
    and confirmed.

    This script is fully self-contained — it does not depend on any shared
    module. (The health-check button likewise carries its own reader.)

HOW TO RUN
    - pyRevit > drop this in a button, or run via the pyRevit 'Run script' /
      RevitPythonShell against the open model.
    - Works under both the IronPython 2.7 and CPython 3 pyRevit engines.
    - Prompts for a save location and writes two files:
        <name>.json  - rich, nested (one element -> many materials)
        <name>.csv   - flat (one row per material per element), pipeline-friendly

NOTE ON UNITS
    Revit stores everything internally in feet regardless of project units.
    We convert explicitly with constants below rather than using UnitUtils,
    because the UnitUtils / UnitTypeId API changed across Revit versions and
    explicit constants are version-proof.
"""

from __future__ import print_function

import json
import os
import datetime

from pyrevit import revit, DB, forms, script

doc = revit.doc
output = script.get_output()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# All physical building element categories relevant to a BoQ.
# Built dynamically so that categories missing in a given Revit version
# are silently skipped rather than crashing at startup.
_CATEGORY_NAMES = [
    # Structural
    "OST_StructuralColumns",
    "OST_StructuralFraming",
    "OST_StructuralFoundation",
    "OST_StructuralConnectionHandler",
    "OST_Rebar",

    # Architectural
    "OST_Walls",
    "OST_Floors",
    "OST_Roofs",
    "OST_Ceilings",
    "OST_Doors",
    "OST_Windows",
    "OST_Stairs",
    "OST_StairsRailing",
    "OST_Ramps",
    "OST_Columns",
    "OST_CurtainWallPanels",
    "OST_CurtainWallMullions",
    "OST_Railings",

    # MEP
    "OST_PipeCurves",
    "OST_PipeFitting",
    "OST_PipeAccessory",
    "OST_PlumbingFixtures",
    "OST_DuctCurves",
    "OST_DuctFitting",
    "OST_DuctAccessory",
    "OST_MechanicalEquipment",
    "OST_ElectricalEquipment",
    "OST_ElectricalFixtures",
    "OST_LightingFixtures",
    "OST_Sprinklers",
    "OST_CableTray",
    "OST_Conduit",

    # Site / General
    "OST_Topography",
    "OST_Site",
    "OST_Parking",
    "OST_Entourage",
    "OST_Furniture",
    "OST_FurnitureSystems",
    "OST_SpecialityEquipment",
    "OST_GenericModel",

    # Rooms / Spaces
    "OST_Rooms",
]

TARGET_CATEGORIES = []
for _name in _CATEGORY_NAMES:
    try:
        TARGET_CATEGORIES.append(getattr(DB.BuiltInCategory, _name))
    except AttributeError:
        pass  # category doesn't exist in this Revit version

# Heuristic used only to TAG material as concrete-like. Not a hard filter.
CONCRETE_HINTS = ["concrete", "conc", "beton"]

# Internal-unit (feet) -> metric conversion. Exact factors.
FT_TO_M = 0.3048
FT2_TO_M2 = FT_TO_M ** 2          # 0.09290304
FT3_TO_M3 = FT_TO_M ** 3          # 0.028316846592


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def element_id_int(eid):
    """Version-safe ElementId to integer.
    Revit <= 2023 uses .IntegerValue, Revit 2024+ uses .Value.
    Cast to int() because Revit 2027 returns long which IronPython's
    json module cannot serialize."""
    try:
        return int(eid.Value)
    except AttributeError:
        return int(eid.IntegerValue)


def get_param_string(element, builtin_param):
    """Safely read a built-in parameter as a string, or '' if absent."""
    try:
        p = element.get_Parameter(builtin_param)
        if p and p.HasValue:
            s = p.AsString()
            if s:
                return s
            # fall back to value string for non-string storage
            vs = p.AsValueString()
            return vs if vs else ""
    except Exception:
        pass
    return ""


def get_type_element(element):
    """Return the ElementType for any element (works for both family
    instances and system families like floors/walls)."""
    try:
        tid = element.GetTypeId()
        if tid and tid != DB.ElementId.InvalidElementId:
            return doc.GetElement(tid)
    except Exception:
        pass
    return None


def get_family_and_type(element):
    """Return (family_name, type_name) as best we can across element kinds."""
    family_name = ""
    type_name = ""
    type_el = get_type_element(element)
    if type_el is not None:
        # Most ElementTypes expose FamilyName; system families fall back to category
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


def get_level_name(element):
    """Resolve a human-readable level name. Tries the LevelId property first,
    then a list of common built-in level parameters."""
    # 1. direct LevelId property
    try:
        lid = element.LevelId
        if lid and lid != DB.ElementId.InvalidElementId:
            lvl = doc.GetElement(lid)
            if lvl is not None:
                return lvl.Name
    except Exception:
        pass

    # 2. common level-bearing parameters
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


def get_level_elevation(element):
    """Elevation (m) of the element's associated level, or "" if none. Lets the
    BoQ tell ground-bearing slabs from suspended (upper-floor) ones."""
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


def is_structural(element):
    """Best-effort read of the 'Structural' instance flag (relevant for walls)."""
    try:
        p = element.get_Parameter(DB.BuiltInParameter.WALL_STRUCTURAL_SIGNIFICANT)
        if p and p.HasValue:
            return bool(p.AsInteger())
    except Exception:
        pass
    # Many genuinely structural categories have no such flag; treat as True.
    return True


def get_bounding_box_m(element):
    """Return bounding box min/max in metres, or None."""
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


def get_joined_element_ids(element):
    """Return the ids of elements whose geometry is joined to this one.
    This is the key signal for intersection / double-count handling."""
    ids = []
    try:
        joined = DB.JoinGeometryUtils.GetJoinedElements(doc, element)
        for jid in joined:
            ids.append(element_id_int(jid))
    except Exception:
        pass
    return ids


def looks_like_concrete(material_name):
    name = (material_name or "").lower()
    return any(h in name for h in CONCRETE_HINTS)


def _json_safe(obj):
    """Fallback serializer for .NET types that IronPython's json module
    cannot handle (long, System.Double, System.Boolean, etc.)."""
    if isinstance(obj, bool):
        return bool(obj)
    if isinstance(obj, float):
        return float(obj)
    try:
        return int(obj)
    except (TypeError, ValueError):
        pass
    try:
        return float(obj)
    except (TypeError, ValueError):
        pass
    return str(obj)


def get_materials(element):
    """Return a list of per-material quantity dicts for an element.
    Uses GetMaterialVolume / GetMaterialArea so concrete is isolated from
    any other materials (rebar, finishes) in the same element."""
    rows = []
    try:
        mat_ids = element.GetMaterialIds(False)  # False = structural, not paint
    except Exception:
        mat_ids = []

    for mid in mat_ids:
        mat = doc.GetElement(mid)
        if mat is None:
            continue
        mat_name = ""
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
            "looks_like_concrete": looks_like_concrete(mat_name),
            "volume_m3": round(vol_ft3 * FT3_TO_M3, 6),
            "area_m2": round(area_ft2 * FT2_TO_M2, 6),
        })
    return rows


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------

def collect_elements():
    """Collect all instances across the target building categories.
    Categories that don't exist in the model are silently skipped."""
    collected = []
    for bic in TARGET_CATEGORIES:
        try:
            els = (DB.FilteredElementCollector(doc)
                   .OfCategory(bic)
                   .WhereElementIsNotElementType()
                   .ToElements())
            collected.extend(list(els))
        except Exception:
            # Category may not exist in this Revit version — skip silently
            continue
    return collected


def build_record(element):
    """Build the full extraction record for one element."""
    cat_name = ""
    try:
        if element.Category is not None:
            cat_name = element.Category.Name
    except Exception:
        pass

    family_name, type_name = get_family_and_type(element)
    materials = get_materials(element)

    # element-level concrete hint = any material on it looks like concrete
    has_concrete = any(m["looks_like_concrete"] for m in materials)
    concrete_volume_m3 = round(
        sum(m["volume_m3"] for m in materials if m["looks_like_concrete"]), 6
    )

    return {
        "element_id": element_id_int(element.Id),
        "category": cat_name,
        "family": family_name,
        "type": type_name,
        "level": get_level_name(element),
        "level_elevation": get_level_elevation(element),
        "mark": get_param_string(element, DB.BuiltInParameter.ALL_MODEL_MARK),
        "comments": get_param_string(
            element, DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS),
        "is_structural": is_structural(element),
        "has_concrete_material": has_concrete,
        "concrete_volume_m3": concrete_volume_m3,
        "materials": materials,
        "joined_to_element_ids": get_joined_element_ids(element),
        "bounding_box": get_bounding_box_m(element),
    }


def flatten_records(records):
    """One row per material per element (elements with no material still get
    a single row, so nothing is dropped)."""
    flat = []
    for r in records:
        base = {
            "element_id": r["element_id"],
            "category": r["category"],
            "family": r["family"],
            "type": r["type"],
            "level": r["level"],
            "level_elevation": r["level_elevation"],
            "mark": r["mark"],
            "comments": r["comments"],
            "is_structural": r["is_structural"],
            "joined_to_count": len(r["joined_to_element_ids"]),
        }
        if r["materials"]:
            for m in r["materials"]:
                row = dict(base)
                row["material_name"] = m["material_name"]
                row["looks_like_concrete"] = m["looks_like_concrete"]
                row["volume_m3"] = m["volume_m3"]
                row["area_m2"] = m["area_m2"]
                flat.append(row)
        else:
            row = dict(base)
            row["material_name"] = ""
            row["looks_like_concrete"] = False
            row["volume_m3"] = 0.0
            row["area_m2"] = 0.0
            flat.append(row)
    return flat


def write_csv(flat_rows, path):
    """Write a flat CSV without relying on the csv module's dialect quirks
    across IronPython / CPython. Quotes fields and escapes embedded quotes."""
    columns = [
        "element_id", "category", "family", "type", "level", "level_elevation", "mark",
        "comments", "is_structural", "joined_to_count", "material_name",
        "looks_like_concrete", "volume_m3", "area_m2",
    ]

    def esc(v):
        s = u"" if v is None else u"{0}".format(v)
        s = s.replace(u'"', u'""')
        return u'"{0}"'.format(s)

    lines = [u",".join(columns)]
    for row in flat_rows:
        lines.append(u",".join(esc(row.get(c, "")) for c in columns))

    with open(path, "w") as f:
        f.write(u"\n".join(lines).encode("utf-8") if str is bytes
                else u"\n".join(lines))


def main():
    output.print_md("# Model data extraction")

    elements = collect_elements()
    if not elements:
        forms.alert("No elements found in the target categories.", exitscript=True)

    records = []
    for el in elements:
        try:
            records.append(build_record(el))
        except Exception as ex:
            output.print_md("Skipped element {0}: {1}".format(
                element_id_int(el.Id), ex))

    # ---- summary stats (printed, not used to filter) --------------------
    total = len(records)
    with_concrete = sum(1 for r in records if r["has_concrete_material"])
    total_conc_vol = round(sum(r["concrete_volume_m3"] for r in records), 4)
    not_joined = sum(1 for r in records if not r["joined_to_element_ids"])

    output.print_md("**Elements extracted:** {0}".format(total))
    output.print_md("**With concrete material:** {0}".format(with_concrete))
    output.print_md("**Total concrete volume (raw, m3):** {0}".format(total_conc_vol))
    output.print_md(
        "**Not joined to any element:** {0} "
        "(possible intersection / double-count candidates - flagged, not corrected)"
        .format(not_joined))

    # ---- write files ----------------------------------------------------
    default_name = "model_data_extract_{0}".format(
        datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))

    save_path = forms.save_file(file_ext="json", default_name=default_name)
    if not save_path:
        output.print_md("No save location chosen - nothing written.")
        return

    base, _ = os.path.splitext(save_path)
    json_path = base + ".json"
    csv_path = base + ".csv"

    payload = {
        "meta": {
            "model": doc.Title,
            "extracted_at": datetime.datetime.now().isoformat(),
            "units": "metric (m, m2, m3)",
            "concrete_hint_terms": CONCRETE_HINTS,
            "note": ("Raw geometric quantities only. No SANS measurement, "
                     "mapping, or deduction applied at this stage."),
            "element_count": total,
        },
        "elements": records,
    }

    with open(json_path, "w") as f:
        json.dump(payload, f, indent=2, default=_json_safe)

    write_csv(flatten_records(records), csv_path)

    output.print_md("---")
    output.print_md("Wrote:")
    output.print_md("- `{0}`".format(json_path))
    output.print_md("- `{0}`".format(csv_path))


if __name__ == "__main__":
    main()
