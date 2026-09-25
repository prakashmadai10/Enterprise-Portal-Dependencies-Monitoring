"""Create the empty dependency-monitoring hosted tables in an ArcGIS Enterprise portal.

Data model (one hosted item with these tables):
  0  Items              one row per item                        (key: item_id)
  1  Dependencies       one row per item / related item / direction, for the Dashboard lists
                        (key: origin_id, dependent_id, direction)
  2  Broken references  one row per reference to a missing item (key: origin_id, missing_id)
  4  Item edges         one row per link between two items, stored once (key: source, target, kind)
  6  Run history        one row per scan run                    (key: run_started)
  7  Changes            one row per item/dependency change, append-only (no key)
  8  Indirect impact    one row per item reachable 2+ hops upstream (key: origin_id, affected_id)

Table ids must match TABLE_DEFS exactly: the hosted feature service assigns each new table the
next sequential id regardless of what "id" is requested, so any gap here (ids 3 and 5 are retired
tables, not free slots) has to be intentional and reflected in TABLE_DEFS, or the next table
created silently lands on the wrong id (see the comment on _add_tables for what that looked like
the one time it happened).

Item edges is the normalized core: each link is stored once, with where it was found and whether
the target exists. Dependencies and Broken references are serving tables built from it for the
Dashboard, which cannot join tables. Every row carries refreshed_at, the time of the last run.

Usage:
    python create_enterprise_tables.py
Reads PORTAL_URL, PORTAL_USERNAME and PORTAL_PASSWORD from .env. Prints the new item ID
to put in DASHBOARD_TABLES_ID.
"""
import json
import os

from dotenv import load_dotenv
from arcgis.gis import GIS
from arcgis.features import FeatureLayerCollection

SERVICE_NAME = "Portal_Dependencies_Monitoring"


def str_field(name, alias, length=255):
    return {"name": name, "type": "esriFieldTypeString", "alias": alias, "length": length, "nullable": True, "editable": True}


def int_field(name, alias):
    return {"name": name, "type": "esriFieldTypeInteger", "alias": alias, "nullable": True, "editable": True}


def float_field(name, alias):
    return {"name": name, "type": "esriFieldTypeDouble", "alias": alias, "nullable": True, "editable": True}


def date_field(name, alias):
    return {"name": name, "type": "esriFieldTypeDate", "alias": alias, "nullable": True, "editable": True}


OID = {"name": "OBJECTID", "type": "esriFieldTypeOID", "alias": "OBJECTID", "nullable": False, "editable": False}

REFRESHED = date_field("refreshed_at", "Refreshed At")

MONITORING_FIELDS = [
    OID,
    str_field("item_id", "Item ID"),
    str_field("item_title", "Item Title"),
    str_field("item_owner", "Item Owner"),
    str_field("item_type", "Item Type"),
    str_field("item_page_url", "Item Page URL"),
    str_field("item_sharing", "Item Sharing"),
    str_field("access_level", "Access Level"),
    str_field("has_groups", "Shared With Groups"),
    # Wider than the 255-char default: an item shared with several groups can exceed 255
    # once their titles are joined, and that overflow rolled back the whole update the first
    # time this shipped (see the "; ".join(...) calls that populate this in the scan)
    str_field("item_groups", "Shared Group Names", length=4000),
    str_field("item_status", "Item Status"),
    date_field("item_date_created", "Item Date Created"),
    date_field("item_date_modified", "Item Date Modified"),
    str_field("has_dependencies", "Has Dependencies"),
    int_field("dependencies_count", "Dependencies Count"),
    str_field("dependencies_band", "Dependencies Band"),
    str_field("modified_band", "Modified Band"),
    str_field("cleanup_candidate", "Cleanup Candidate"),
    REFRESHED,
]

DEPENDENCIES_FIELDS = [
    OID,
    str_field("origin_id", "Origin ID"),
    str_field("origin_title", "Origin Title"),
    str_field("origin_item_page_url", "Origin Item Page URL"),
    str_field("dependent_id", "Dependent ID"),
    str_field("dependent_title", "Dependent Title"),
    str_field("dependent_owner", "Dependent Owner"),
    str_field("dependent_type", "Dependent Type"),
    str_field("dependent_item_page_url", "Dependent Item Page URL"),
    str_field("dependent_sharing", "Dependent Sharing"),
    str_field("dependent_groups", "Dependent Group Names", length=4000),
    str_field("dependent_status", "Dependent Status"),
    date_field("dependent_date_created", "Dependent Date Created"),
    date_field("dependent_date_modified", "Dependent Date Modified"),
    str_field("direction", "Direction"),
    str_field("origin_sharing", "Origin Sharing"),
    str_field("origin_groups", "Origin Group Names", length=4000),
    str_field("sharing_conflict", "Sharing Conflict"),
    str_field("conflict_reason", "Conflict Reason"),
    REFRESHED,
]

# Items whose data points at an item that no longer exists or cannot be read
BROKEN_FIELDS = [
    OID,
    str_field("origin_id", "Origin ID"),
    str_field("origin_title", "Origin Title"),
    str_field("origin_type", "Origin Type"),
    str_field("origin_owner", "Origin Owner"),
    str_field("origin_item_page_url", "Origin Item Page URL"),
    str_field("missing_id", "Missing Item ID"),
    str_field("evidence_path", "Found At"),
    REFRESHED,
]

# The normalized core: one row per link between two items (a map uses a layer)
EDGE_FIELDS = [
    OID,
    str_field("source_item_id", "Source Item ID"),
    str_field("target_item_id", "Target Item ID"),
    str_field("relation_kind", "Relation Kind"),
    str_field("target_state", "Target State"),
    str_field("evidence_path", "Found At"),
    REFRESHED,
]

# One row per run, so the dashboard can show whether the last run actually finished, how much
# of the portal it covered, and how many individual reads it could not complete. Rows accumulate
# (see append_and_prune in the scan) rather than being replaced in place like the tables above.
RUN_FIELDS = [
    OID,
    date_field("run_started", "Run Started"),
    date_field("run_completed", "Run Completed"),
    float_field("duration_seconds", "Duration (seconds)"),
    int_field("items_found", "Items Found"),
    int_field("items_scanned", "Items Scanned"),
    int_field("items_excluded", "Items Excluded"),
    int_field("cache_hits", "Cache Hits"),
    int_field("failed_reads", "Failed Reads"),
    int_field("failed_lookups", "Failed Lookups"),
    int_field("edges_found", "Item Edges Found"),
    int_field("dependencies_found", "Dependencies Found"),
    int_field("sharing_conflicts_found", "Sharing Conflicts Found"),
    int_field("broken_references_found", "Broken References Found"),
    REFRESHED,
]

# One row per item added or deleted, dependency added or removed, or dependency that newly
# became a sharing conflict, one run's worth per refreshed_at. Also accumulates; see
# append_and_prune. Not a full audit log: only entity_type/change_kind pairs the scan
# explicitly tracks (see sync_table's label_field/track_field) show up here.
CHANGE_FIELDS = [
    OID,
    str_field("entity_type", "Entity Type"),
    str_field("change_kind", "Change Kind"),
    str_field("entity_key", "Entity Key"),
    str_field("title", "Title"),
    str_field("detail", "Detail"),
    REFRESHED,
]

# Indirect impact: for an item with at least one upstream dependent, every item reachable by
# two or more hops (the dependent's own dependents, and so on) - "if I delete this, what
# eventually breaks, not just what breaks directly". One-hop impact is already the
# Dependencies table's "Used by" rows, so this table only stores hop_count >= 2, to add
# information instead of duplicating it. Rebuilt in full each run, like Dependencies.
IMPACT_FIELDS = [
    OID,
    str_field("origin_id", "Origin ID"),
    str_field("origin_title", "Origin Title"),
    str_field("affected_id", "Affected ID"),
    str_field("affected_title", "Affected Title"),
    str_field("affected_type", "Affected Type"),
    str_field("affected_owner", "Affected Owner"),
    str_field("affected_item_page_url", "Affected Item Page URL"),
    int_field("hop_count", "Hops"),
    str_field("via_id", "Via ID"),
    str_field("via_title", "Via"),
    REFRESHED,
]

# (table id, name, fields) for every table in the hosted item
TABLE_DEFS = [
    (0, "Items", MONITORING_FIELDS),
    (1, "Dependencies", DEPENDENCIES_FIELDS),
    (2, "Broken references", BROKEN_FIELDS),
    (4, "Item edges", EDGE_FIELDS),
    (6, "Run history", RUN_FIELDS),
    (7, "Changes", CHANGE_FIELDS),
    (8, "Indirect impact", IMPACT_FIELDS),
]

# Attribute indexes per table id: (name, comma-separated fields, unique). The unique ones are the
# keys; a hosted table has no other way to stop a duplicate row.
INDEX_SPECS = {
    0: [("item_id_uq", "item_id", True), ("item_owner_ix", "item_owner", False), ("item_type_ix", "item_type", False)],
    1: [("dependency_uq", "origin_id,dependent_id,direction", True), ("origin_id_ix", "origin_id", False),
        ("direction_ix", "direction", False)],
    2: [("broken_uq", "origin_id,missing_id", True)],
    4: [("edge_uq", "source_item_id,target_item_id,relation_kind", True),
        ("target_item_id_ix", "target_item_id", False), ("target_state_ix", "target_state", False)],
    6: [("run_started_ix", "run_started", False)],
    7: [("change_run_ix", "refreshed_at", False), ("change_type_ix", "entity_type", False)],
    8: [("impact_uq", "origin_id,affected_id", True), ("impact_origin_ix", "origin_id", False)],
}


def table_def(table_id, name, fields):
    return {
        "type": "Table",
        "id": table_id,
        "name": name,
        "displayField": "",
        "objectIdField": "OBJECTID",
        "fields": fields,
        "capabilities": "Create,Delete,Query,Update,Editing",
    }


# Colors shared with the dashboard: blue for items, teal for dependencies
ACCENT_ITEMS = "#007ac2"
ACCENT_DEPENDENCIES = "#2e9e8a"

_LABEL_STYLE = "color:#33475b;font-weight:700;padding:7px 10px 7px 0;width:36%;vertical-align:top;border-bottom:1px solid #e9edf1;"
_VALUE_STYLE = "color:#1f2d3a;padding:7px 0;vertical-align:top;border-bottom:1px solid #e9edf1;"
_MONO = "font-family:Consolas,Menlo,monospace;font-size:12px;color:#5f6b7a;"


def pill(text, background, color):
    """A small rounded label; `text` may hold a {field} token."""
    return (f'<span style="display:inline-block;background:{background};color:{color};border-radius:10px;'
            f'padding:1px 9px;font-size:11px;font-weight:600;margin-right:4px;">{text}</span>')


def _popup_table(rows):
    """rows: lists of cells (label, field, extra style). A row with two cells is drawn as
    two label/value pairs side by side; a row with one cell spans the width."""
    body = ""
    for row in rows:
        cells = ""
        for label, field, extra in row:
            span = ' colspan="3"' if len(row) == 1 else ""
            cells += (f'<td style="{_LABEL_STYLE}width:16%;">{label}</td>'
                      f'<td style="{_VALUE_STYLE}{extra}width:34%;"{span}>{{{field}}}</td>')
        body += f"<tr>{cells}</tr>"
    return f'<table style="width:100%;border-collapse:collapse;font-size:13px;">{body}</table>'


def _link(url_field):
    """The tables hold the raw page URL; the link is built here, so a changed portal address
    or wording never touches the data. Styled as a button so it reads as an action, not text."""
    return (f'<div style="margin-top:8px;">'
            f'<a href="{{{url_field}}}" target="_blank" rel="noopener noreferrer" '
            f'style="display:inline-block;padding:7px 14px;background:{ACCENT_ITEMS};color:#ffffff;'
            f'border-radius:4px;font-size:13px;font-weight:600;text-decoration:none;">Open item page</a></div>')


# Item Details panel: the item title is the popup title (shown at the top), then a type
# label and every other fact the Items table holds, in a compact grid, then the link.
ITEM_POPUP_ROWS = [
    [("Owner", "item_owner", ""), ("Sharing", "item_sharing", "")],
    [("Modified", "item_date_modified", ""), ("Created", "item_date_created", "")],
    [("Status", "item_status", ""), ("Access level", "access_level", "")],
    [("Shared with groups", "has_groups", ""), ("Has dependencies", "has_dependencies", "")],
    [("Dependencies", "dependencies_count", ""), ("Dependencies band", "dependencies_band", "")],
    [("Modified band", "modified_band", ""), ("Cleanup candidate", "cleanup_candidate", "")],
    [("Item ID", "item_id", _MONO)],
]
ITEM_POPUP_OTHER = [("item_title", "Item Title"), ("item_type", "Item Type"),
                    ("item_page_url", "Item Page URL")]

# Dependency Details panel: the dependency's title is the popup title, then its facts and
# the item it is shown for.
DEPENDENCY_POPUP_ROWS = [
    [("Owner", "dependent_owner", ""), ("Sharing", "dependent_sharing", "")],
    [("Modified", "dependent_date_modified", ""), ("Created", "dependent_date_created", "")],
    [("Conflict", "sharing_conflict", "")],
    [("Item ID", "dependent_id", _MONO)],
    [("For item", "origin_title", "")],
]
DEPENDENCY_POPUP_OTHER = [("dependent_title", "Dependent Title"), ("dependent_type", "Dependent Type"),
                          ("direction", "Direction"), ("origin_id", "Origin ID"), ("origin_sharing", "Origin Sharing"),
                          ("dependent_status", "Dependent Status"),
                          ("dependent_item_page_url", "Dependent Item Page URL")]


def _field_infos(rows, other):
    infos = [(field, label) for row in rows for label, field, _ in row] + list(other)
    result = []
    for name, label in infos:
        info = {"fieldName": name, "label": label, "isEditable": True, "visible": True}
        if "_date_" in name:
            info["format"] = {"dateFormat": "shortDateShortTime"}
        result.append(info)
    return result


def item_popup_info():
    html = (f'<div style="margin:2px 0 8px;">{pill("{item_type}", "#e6f2fb", "#005a94")}</div>'
            + _popup_table(ITEM_POPUP_ROWS) + _link("item_page_url"))
    return {
        "title": "{item_title}",
        "mediaInfos": [],
        "popupElements": [{"type": "text", "text": html}],
        "fieldInfos": _field_infos(ITEM_POPUP_ROWS, ITEM_POPUP_OTHER),
    }


def dependency_popup_info():
    html = (f'<div style="margin:2px 0 8px;">{pill("{dependent_type}", "#e3f4f1", "#1d6f61")}'
            f'{pill("{direction}", "#f1f3f5", "#495057")}</div>' + _popup_table(DEPENDENCY_POPUP_ROWS)
            + _link("dependent_item_page_url"))
    return {
        "title": "{dependent_title}",
        "mediaInfos": [],
        "popupElements": [{"type": "text", "text": html}],
        "fieldInfos": _field_infos(DEPENDENCY_POPUP_ROWS, DEPENDENCY_POPUP_OTHER),
    }


def set_item_popup(item):
    """Store the Item Details and Dependency Details popups on tables 0 and 1."""
    data = item.get_data() or {}
    existing_ids = {t.properties.id for t in item.tables}
    tables = [t for t in data.get("tables", []) if t.get("id") in existing_ids and t.get("id") not in (0, 1)]
    tables.append({"id": 0, "popupInfo": item_popup_info()})
    tables.append({"id": 1, "popupInfo": dependency_popup_info()})
    # Dashboards finds a table through this list, so every table of the item must be in it
    listed = {t["id"] for t in tables}
    tables.extend({"id": t.properties.id} for t in item.tables if t.properties.id not in listed)
    data["tables"] = sorted(tables, key=lambda t: t["id"])
    item.update(item_properties={"text": json.dumps(data)})


def drop_fields(table, names):
    """Delete the named fields from an existing table (fields dropped from the schema in a
    later version) and return the names that were removed."""
    existing = {f["name"].lower() for f in table.properties.fields}
    present = [n for n in names if n.lower() in existing]
    if present:
        table.manager.delete_from_definition({"fields": [{"name": n} for n in present]})
    return present


def ensure_fields(table, fields):
    """Add any of `fields` (field definitions) that an existing table does not have yet."""
    # Portals may lowercase field names (OBJECTID becomes objectid), so compare
    # case-insensitively; the object id field is managed by the service
    existing = {f["name"].lower() for f in table.properties.fields}
    missing = [f for f in fields
               if f["name"].lower() not in existing and f["type"] != "esriFieldTypeOID"]
    if missing:
        table.manager.add_to_definition({"fields": missing})
    return [f["name"] for f in missing]


def ensure_indexes(table, table_id):
    """Add the attribute indexes of INDEX_SPECS that the table lacks (the unique ones are its key)
    and return their names."""
    existing = {i["name"].lower() for i in (getattr(table.properties, "indexes", None) or [])}
    missing = [
        {"name": name, "fields": fields, "isAscending": True, "isUnique": unique,
         "description": "Key" if unique else "Filter field"}
        for name, fields, unique in INDEX_SPECS.get(table_id, []) if name.lower() not in existing
    ]
    if missing:
        table.manager.add_to_definition({"indexes": missing})
    return [m["name"] for m in missing]


def ensure_table_names(item):
    """Give each existing table the name in TABLE_DEFS and return the names that changed."""
    by_id = {t.properties.id: t for t in item.tables}
    changed = []
    for table_id, name, _ in TABLE_DEFS:
        table = by_id.get(table_id)
        if table is not None and table.properties.name != name:
            table.manager.update_definition({"name": name})
            changed.append(name)
    return changed


def _add_tables(flc, defs):
    """Add each table in its own add_to_definition call, and confirm the id it actually got.

    The hosted feature service assigns each new table the next sequential id in creation
    order; the "id" in table_def is not honored as a request. This bit us once already: adding
    all of TABLE_DEFS in one batched call when it still had a gap at id 3 (for a since-removed
    "Run History" table) made the service assign 3, 4, 5 to the tables meant for 4, 5, 6, and
    the rest of this module - which trusted TABLE_DEFS's ids - then silently mixed the wrong
    fields into the wrong tables under `ensure_table_names`/`ensure_fields`. TABLE_DEFS must
    stay a contiguous 0..N with no gaps; this also fails loudly instead of silently if it ever
    does not, rather than repeat that.
    """
    for expected_id, name, fields in defs:
        flc.manager.add_to_definition({"tables": [table_def(expected_id, name, fields)]})
        flc = FeatureLayerCollection(flc.url, gis=flc._gis)
        actual = {t.properties.name: t.properties.id for t in flc.tables}.get(name)
        if actual != expected_id:
            raise RuntimeError(
                f"Table {name!r} was created with id {actual}, not the expected {expected_id}. "
                "TABLE_DEFS ids must match the order tables are actually created in - fix the "
                "list and recreate the hosted item.")


def ensure_tables(item):
    """Add any table of TABLE_DEFS the hosted item lacks and return the refreshed item,
    with its tables in id order. Dashboards only finds a table listed in the item data,
    so the popup / table list is rewritten whenever a table is added."""
    existing = {t.properties.id for t in item.tables}
    missing = [d for d in TABLE_DEFS if d[0] not in existing]
    if missing:
        _add_tables(FeatureLayerCollection.fromitem(item), missing)
        item = item._gis.content.get(item.id)
        set_item_popup(item)
    return item


def share_with_org(item):
    """Share the item with the whole organization unless it already is (or is public).
    Returns True if the sharing changed."""
    if item.access in ("org", "public"):
        return False
    item.sharing.sharing_level = "ORG"
    return True


def create_tables(gis):
    """Create the hosted item with all the tables and return it."""
    item = gis.content.create_service(
        name=SERVICE_NAME,
        service_type="featureService",
        create_params={
            "name": SERVICE_NAME,
            "hasStaticData": False,
            "capabilities": "Create,Delete,Query,Update,Editing",
            "maxRecordCount": 2000,
        },
        tags=["dependencies", "monitoring"],
        snippet="Hosted tables populated by Portal_Monitoring_Dependencies.py",
    )

    flc = FeatureLayerCollection.fromitem(item)
    _add_tables(flc, TABLE_DEFS)

    item = gis.content.get(item.id)
    set_item_popup(item)
    return item


def main():
    load_dotenv()
    gis = GIS(os.environ["PORTAL_URL"], os.environ["PORTAL_USERNAME"], os.environ["PORTAL_PASSWORD"])
    item = create_tables(gis)
    print(f"Created item {item.id} with {len(item.tables)} tables")
    print(f"Set DASHBOARD_TABLES_ID={item.id} in .env")


if __name__ == "__main__":
    main()
