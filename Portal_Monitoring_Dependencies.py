
# ---------------------------------------------------------------------------
# Settings, imports and connection
# ---------------------------------------------------------------------------

# Settings come from .env. To use your own tables, set DASHBOARD_TABLES_ID to the
# item ID of the hosted tables. If it is empty, or the item no longer exists,
# the tables are created later in this script.
import hashlib
import logging
import os
import sys
import time
from datetime import datetime, timezone
from fnmatch import fnmatch
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
from dotenv import load_dotenv

from reference_cache import ReferenceCache

load_dotenv()

# Timer for the whole run; the last log lines report the elapsed time
script_start = time.perf_counter()

# Logging goes to the console and to a rotating file (LOG_FILE, default
# logs/dependencies_monitor.log). LOG_LEVEL=DEBUG adds more detail.
log_file = Path(os.getenv("LOG_FILE", "logs/dependencies_monitor.log"))
log_file.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8"),
    ],
)
# The arcgis and urllib3 libraries are chatty below WARNING
logging.getLogger("arcgis").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
log = logging.getLogger("dependencies_monitor")


def format_duration(seconds):
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(int(minutes), 60)
    if hours:
        return f"{hours}h {minutes}m {secs:.0f}s"
    if minutes:
        return f"{minutes}m {secs:.1f}s"
    return f"{secs:.1f}s"


def log_step(step, started):
    log.info("%s finished in %s", step, format_duration(time.perf_counter() - started))


# Record any uncaught error in the log, with the time the run had taken
def log_uncaught(exc_type, exc, tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    log.critical("Run failed after %s", format_duration(time.perf_counter() - script_start),
                 exc_info=(exc_type, exc, tb))

sys.excepthook = log_uncaught

log.info("Run started")

dashboard_tables_id = os.getenv("DASHBOARD_TABLES_ID", "").strip()

# CHECK_URLS=true also looks for dependencies referenced by service URL only.
# In the original author's org this found about 5% more dependencies but made the
# run take about twice as long. See get_id_from_url for details.
check_urls = os.getenv("CHECK_URLS", "false").strip().lower() in ("1", "true", "yes")

# EXCLUDE_OWNERS is a comma-separated list of item owners to leave out, with * as a
# wildcard. The default skips the esri_* system accounts (esri_apps, esri_nav,
# esri_webstyles), whose items (styles, license type extensions and similar) are not
# content anyone manages or deletes.
exclude_owner_patterns = [
    p.strip().lower() for p in os.getenv("EXCLUDE_OWNERS", "esri_*").split(",") if p.strip()
]

def is_excluded_owner(owner):
    return any(fnmatch(str(owner).lower(), pattern) for pattern in exclude_owner_patterns)

# Counts for the Run history table: failed_reads is an item whose own JSON could not be read
# (item.get_data()); failed_lookups is a referenced item ID that could not be resolved back to
# an Item (related-item lookups, or an unknown ID looked up by get_dependencies /
# get_reference_edges). Both are already caught and logged individually as warnings; this just
# adds up how many there were so a run's health can be read at a glance instead of grepped for.
scan_stats = {"cache_hits": 0, "failed_reads": 0, "failed_lookups": 0}

# Silence the InsecureRequestWarning that the API raises when max_items is above 200
# (https://github.com/Esri/arcgis-python-api/issues/2164). It is a false alarm.
from arcgis.gis import GIS
from arcgis.features import FeatureLayerCollection

import warnings
from urllib3.exceptions import InsecureRequestWarning
warnings.simplefilter("ignore", InsecureRequestWarning)

step_start = time.perf_counter()
portal_url = os.getenv("PORTAL_URL")
if portal_url:
    gis = GIS(portal_url, os.environ["PORTAL_USERNAME"], os.environ["PORTAL_PASSWORD"])
else:
    gis = GIS("home")
log.info("Connected to %s", gis.url)
log_step("Connecting", step_start)

# Base URL for the item and map links in the tables (Enterprise: https://host/portal)
portal_base = (portal_url or gis.url).rstrip("/")

# SHARE_WITH_ORG=true shares the tables item with the whole organization (default: leave the
# sharing as it is). The dashboard is shared the same way by create_dashboard.py.
share_with_org_enabled = os.getenv("SHARE_WITH_ORG", "false").strip().lower() in ("1", "true", "yes")

# Item types whose data is JSON and worth scanning for item IDs. Other types are
# skipped because get_data would download the whole file.
JSON_ITEM_TYPES = {
    "Web Map", "Web Scene", "Dashboard", "Web Experience", "Web Experience Template",
    "StoryMap", "Web Mapping Application", "Form", "Hub Site Application", "Hub Page",
    "Hub Initiative", "Site Application", "Site Page", "Application", "Operation View",
    "Mobile Application", "Workforce Project", "Insights Workbook", "Insights Model",
    "Feature Collection", "Solution",
}

# The Broken References table only checks two kinds of reference, which is enough to catch
# the breaks that matter without the noise of every item ID an app stores:
# * a Web Map: the layers and tables it uses (its basemap is left out)
# * a Web Experience (Experience Builder): the data sources it loads by item ID. An app can
#   use a web map or a feature layer directly, without any web map in between, so both count
BROKEN_REFERENCE_TYPES = {"Web Map", "Web Experience", "Dashboard", "Web Mapping Application"}
EXPERIENCE_DATA_SOURCE_TYPES = {"WEB_MAP", "WEB_SCENE", "FEATURE_LAYER", "FEATURE_SERVICE"}
# ArcGIS Dashboards: node types whose itemId is a data reference. mapWidget.itemId is the web
# map a Map widget shows; a layerDataSource is a widget's or selector's dataset, which can point
# straight at a feature layer or table without going through a web map at all.
DASHBOARD_DATA_SOURCE_TYPES = {"mapWidget", "layerDataSource"}
# Instant Apps and (classic) Web AppBuilder apps: the config JSON stores the web map (or, for a
# map series, the group layer) under these keys inside "values", found by key name since the
# rest of the JSON differs by app template.
WEB_MAPPING_APP_KEYS = {"webmap", "group"}

# How many hops the indirect-impact walk (get_indirect_impact_rows) follows upstream before
# giving up on an item. Real dependency chains in this data are shallow (layer -> map -> app is
# already 2 hops); this is a safety cap against a pathological or cyclic graph, not a tuning knob
# meant to be raised in normal use.
MAX_IMPACT_DEPTH = 6

# ---------------------------------------------------------------------------
# Part I: Finding dependencies
# ---------------------------------------------------------------------------

# Resolve a Feature Service URL found in JSON to its item ID.
#
# find_id_paths calls this only when check_urls is on. Some references hold a REST
# endpoint URL and no item ID, so the ID-based scan misses them. Custom geoprocessing
# tools can be built that way. So could older web maps: in older versions of ArcGIS
# Pro (possibly before 3.x), swapping a layer's service URL and saving back to the
# portal dropped the item ID for that layer. Pro no longer does this, but maps saved
# that way may still exist.
#
# Only Feature Service URLs are checked, and Map Services are skipped. Returns the
# item ID, or False if none is found.

def get_id_from_url(url):

    flc_id = False

    # The URL may point at a layer (...FeatureServer/0) or at the service
    # (...FeatureServer). If the last segment is a number, drop it.
    check = url.rsplit("/", maxsplit=1)
    if check[1].isnumeric():
        url = check[0]

    if url.endswith("FeatureServer"):

        # Fetching the service can fail in several ways (outside the org, no
        # permission, service down), so failures are caught and reported
        try:
            flc_obj = FeatureLayerCollection(url, gis=gis)
            flc_id = flc_obj.properties.serviceItemId

        except Exception as e:
            log.warning("Could not retrieve item ID for URL %s (may be outside the org); skipping: %s", url, e)

    return flc_id

# Collect every item ID found in one item's JSON, with the path where it was first found.
#
# This runs once per item. It walks the structure recursively instead of converting
# it to one big string, and keeps any string that looks like an item ID (32
# alphanumeric characters). Some of those will not be item IDs (folder IDs have the
# same format), but they never match a real item when the dictionary is searched,
# and filtering them out would cost about as much as keeping them. The path (for
# example operationalLayers[3].itemId) is stored with each link as evidence.

def find_id_paths(item_data):

    found = {}
    # Every REST service URL seen, regardless of check_urls: cheap to collect while the walk
    # is already visiting each string, and it lets a cached item be resolved for URLs later
    # (e.g. if CHECK_URLS is turned on) without re-fetching and re-walking its JSON.
    urls = {}

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else key)

        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

        elif isinstance(node, str):
            if len(node) == 32 and node.isalnum():
                found.setdefault(node, path)

            if "/rest/services/" in node:
                urls.setdefault(node, path)
                if check_urls:
                    id_from_url = get_id_from_url(node)
                    if id_from_url:
                        found.setdefault(id_from_url, path)

    walk(item_data, "")
    return found, urls

# Item IDs that an item must have for its own parts to work, by item type, for the Broken
# References table:
# * Web Map: the layers and tables it uses (its basemap is left out)
# * Web Experience (Experience Builder): the web maps, web scenes, feature layers and feature
#   services it loads as data sources, with or without a web map in between
# * Dashboard: each Map widget's web map, and each widget's or selector's dataset, which can
#   point straight at a feature layer or table without going through a web map
# * Web Mapping Application (Instant Apps, and classic Web AppBuilder apps): the web map (or,
#   for a map series, the group layer) the app is configured to show
# Returns {item ID: JSON path}.

def collect_references(item_type, item_data):

    found = {}

    def add(value, path):
        if isinstance(value, str) and len(value) == 32 and value.isalnum():
            found.setdefault(value, path)

    def walk(node, path, skip_basemap):
        if isinstance(node, dict):
            if item_type == "Web Experience" and node.get("type") in EXPERIENCE_DATA_SOURCE_TYPES:
                add(node.get("itemId"), f"{path}.itemId" if path else "itemId")
            if item_type == "Dashboard" and node.get("type") in DASHBOARD_DATA_SOURCE_TYPES:
                add(node.get("itemId"), f"{path}.itemId" if path else "itemId")
            for key, value in node.items():
                child = f"{path}.{key}" if path else key
                if item_type == "Web Map" and key == "itemId":
                    add(value, child)
                if item_type == "Web Mapping Application" and key in WEB_MAPPING_APP_KEYS:
                    add(value, child)
                if skip_basemap and key == "baseMap":
                    continue
                walk(value, child, skip_basemap)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]", skip_basemap)

    if item_type in BROKEN_REFERENCE_TYPES:
        walk(item_data, "", skip_basemap=(item_type == "Web Map"))
    return found

# Build the downstream dictionary: item ID to the set of IDs found in its data.
# evidence_dict keeps, for every link, its kind and where it was found (the path, or the
# relationship it came from). references_dict holds the checked references per item.
#
# Upstream dependencies are added afterwards by get_dependencies_dict.

def get_downstream_dict():

    downstream_dict = {}
    cache_hits = 0

    for count, item in enumerate(all_items, start=1):

        if count % 100 == 0:
            log.info("Scanned %d of %d items", count, len(all_items))

        downstream_set = set()
        evidence = {}

        # Only JSON-based types are scanned. For file-based items (shapefiles,
        # PDFs, packages), get_data would download the whole file.
        if item.type in JSON_ITEM_TYPES:

            # item.modified only changes when the item's own data changes, so an item the
            # cache already parsed at this modified date needs no network call at all: its
            # last result is still correct. Only a changed or never-seen item is fetched.
            cached = reference_cache.get(item.id, item.modified, item.type)
            if cached is not None:
                cache_hits += 1
                paths = cached["paths"]
                references_dict[item.id] = cached["references"]
            else:
                # One unreadable item should not stop the whole run
                try:
                    item_data = item.get_data()
                    paths, urls = find_id_paths(item_data)
                    references_dict[item.id] = collect_references(item.type, item_data)
                    reference_cache.put(item.id, item.modified, item.type, paths, references_dict[item.id], urls)
                except Exception as e:
                    log.warning("Could not read data for item %s (%s); skipping: %s", item.id, item.type, e)
                    scan_stats["failed_reads"] += 1
                    paths = {}

            downstream_set = set(paths)
            evidence = {found_id: ("uses", path) for found_id, path in paths.items()}

        # Hosted feature layer views are tied to their source layer through item
        # relationships, not JSON. Both directions are queried, and the upstream
        # pass in get_dependencies_dict makes the link work both ways.
        if item.type == "Feature Service" and "Hosted Service" in item.typeKeywords:
            for direction in ("forward", "reverse"):
                try:
                    for related in item.related_items("Service2Service", direction):
                        downstream_set.add(related.id)
                        evidence.setdefault(related.id, ("related", f"Service2Service relationship ({direction})"))
                except Exception as e:
                    log.warning("Could not read related items for item %s; skipping: %s", item.id, e)
                    scan_stats["failed_lookups"] += 1

        # An item's own ID inside its JSON is not a dependency
        downstream_set.discard(item.id)
        evidence.pop(item.id, None)
        references_dict.get(item.id, {}).pop(item.id, None)

        downstream_dict[item.id] = downstream_set
        evidence_dict[item.id] = evidence

    log.info("Downstream dictionary assembled: %d items (%d served from the reference cache)",
             len(downstream_dict), cache_hits)
    scan_stats["cache_hits"] = cache_hits

    return downstream_dict

# Add the upstream dependencies and finish the dictionary.
#
# Downstream is the easy direction, because it only needs the item's own JSON: a
# web map's data holds the IDs of its layers. Upstream needs the JSON of every other
# item: a web map's ID shows up in the dashboards and apps that use it. This
# function does that in one pass over the downstream dictionary.

def get_dependencies_dict():

    # For every ID in an item's JSON that is itself an item in the org, record the
    # referencing item as an upstream dependency of it
    upstream = {key: set() for key in downstream_dict}
    for k, v in downstream_dict.items():
        for referenced_id in v:
            if referenced_id in upstream and referenced_id != k:
                upstream[referenced_id].add(k)

    for key, ups in upstream.items():
        downstream_dict[key].update(ups)

    # Items with no dependencies would only add empty lookups later, so drop them
    dependencies_dict = {k:v for k, v in downstream_dict.items() if v}

    log.info("Dependencies dictionary assembled: %d items have dependencies", len(dependencies_dict))

    return dependencies_dict

# Return the Item objects for one item's dependencies.
#
# The table rows need properties such as title, owner and type, so the IDs in the
# dependencies dictionary have to be turned back into Item objects.

def get_dependencies(item_id):

    items_list = []

    # Items with no dependencies are not in the dictionary
    if item_id in dependencies_dict:

        ids_list = dependencies_dict[item_id]

        for i in ids_list:

            # A few items raised permission errors, so failures are caught and the
            # ID is skipped
            try:
                # Use the items already downloaded. Only IDs missing from the org
                # search (Living Atlas layers, folders) go to the network, once each.
                if i in excluded_ids:
                    continue

                if i in items_by_id:
                    item = items_by_id[i]
                else:
                    if i not in unknown_id_cache:
                        unknown_id_cache[i] = gis.content.get(i)
                    item = unknown_id_cache[i]

                # gis.content.get returns None when nothing matches the ID, for
                # example a folder ID
                if item and not is_excluded_owner(item.owner):
                    items_list.append(item)

            except Exception as e:
                log.warning("Item ID %s wasn't retrieved (may be outside the org); skipping: %s", i, e)
                scan_stats["failed_lookups"] += 1

    return items_list

# ---------------------------------------------------------------------------
# Part II: Helpers for the table attributes
# ---------------------------------------------------------------------------

# These helpers turn raw item properties into values that read well in the Dashboard.

# The URL of the item's page in the portal. The tables hold the plain URL; the Dashboard
# builds the link, so a changed portal address or link text never touches the data.

def get_item_page_url(item_id):
    return f"{portal_base}/home/item.html?id={item_id}"

# Read an item's sharing settings as two facts: the access level (Private, Organization or
# Everyone) and the titles of the groups it is shared with (empty if none). shared_with comes
# from the SharingManager (item.sharing). get_sharing_label below only cares whether groups is
# empty or not, same as when this was a plain boolean; the actual titles are used for the
# group-identity conflict check (get_group_conflict) and stored for display.

def get_sharing_parts(sharing):

    groups = tuple(sorted(g.title for g in sharing.shared_with["groups"]))

    # "level" is an enum; its value is PRIVATE, ORGANIZATION or EVERYONE
    level = sharing.shared_with["level"].value.title()

    return level, groups

# The short label the Dashboard filter and lists show, built from the two facts.

def get_sharing_label(level, groups):

    if groups:
        if level == "Private":
            return "Groups only"
        else: return f"Groups & {level}"

    else:
        return level

# Cached read of the two facts. Reading item.sharing.shared_with makes a network
# request, and the same item can appear in many rows.

def get_item_sharing_parts(item):
    if item.id not in sharing_cache:
        sharing_cache[item.id] = get_sharing_parts(item.sharing)
    return sharing_cache[item.id]

def get_item_sharing(item):
    return get_sharing_label(*get_item_sharing_parts(item))

def get_item_groups(item):
    return get_item_sharing_parts(item)[1]

# The group_names fields are esriFieldTypeString(4000); an item shared with many groups can
# still overflow that once titles are joined, and a single oversize row rolls back its whole
# write batch (see logs/dependencies_monitor.log, 2026-09-23) - not just that row - so this
# clips defensively rather than trusting the field length to be enough.
GROUP_NAMES_MAX = 3990

def join_group_names(groups):
    joined = "; ".join(groups)
    if len(joined) <= GROUP_NAMES_MAX:
        return joined
    return joined[:GROUP_NAMES_MAX - 1] + "…"

# How widely an item is visible, from the label get_item_sharing returns. A map that is
# visible to more people than a layer it uses breaks for the extra people. This only tells
# levels apart (Private/Organization/Everyone); two items both shared with different groups
# land on the same "Groups only" rank here, which is exactly what get_group_conflict below
# is for - it compares which groups, not just whether there are any.
SHARING_REACH = {
    "Private": 0,
    "Groups only": 1,
    "Organization": 2, "Groups & Organization": 2,
    "Everyone": 3, "Groups & Everyone": 3,
}

def get_sharing_reach(item):
    return SHARING_REACH.get(get_item_sharing(item), 0)

# True if the origin is shared with a group the dependent is not also shared with: someone in
# that extra group could see the origin but not what it depends on. This is deliberately a set
# comparison of group titles, not actual group membership - see the "group-aware sharing
# conflicts" plan for why membership-level comparison was not taken on: it would need every
# group's member list (more API calls, more sensitive data) for a case this already covers.
#
# Only meaningful when the dependent's own reach is exactly "Groups only" (SHARING_REACH 1):
# if the dependent is also shared Organization-wide or with Everyone, that already covers
# every member of any org group the origin is shared with, so a missing group name here isn't
# a real gap - the caller must gate on that before calling this.

def get_group_conflict(origin_groups, dependent_groups):
    return bool(set(origin_groups) - set(dependent_groups))

# Fill the "Status" field: Authoritative or Deprecated. The item's content_status is
# "org_authoritative" or "org_deprecated" (or empty, which is stored as no value), so the
# "org_" prefix is removed and the rest is title-cased.

def get_status(status):
    if not status:
        return None
    else: return status.removeprefix("org_").title()

# ---------------------------------------------------------------------------
# Part III: Building the table rows
# ---------------------------------------------------------------------------

# Every row is a dictionary of field name to value, which is the format edit_features
# expects. has_dependencies, dependencies_count and the bands are derived from the data and
# are stored only because the Dashboard cannot compute them.

# Group the dependency count into ranges for the Dashboard filter. The number
# prefix keeps the ranges in order when the values are sorted alphabetically.

def get_dependencies_band(count):
    if count == 0:
        return "1. None"
    elif count < 5:
        return "2. 1 to 4"
    elif count < 10:
        return "3. 5 to 9"
    elif count < 25:
        return "4. 10 to 24"
    else:
        return "5. 25 or more"

# Group the last-modified date into ranges for the Dashboard filter. Ranges are
# calendar months counted from the day the script runs, so the values only change
# when the tables are refreshed. The number prefix keeps them in order.

def get_modified_band(modified_ms):
    modified = datetime.fromtimestamp(modified_ms / 1000)
    today = datetime.now()
    months_ago = (today.year - modified.year) * 12 + today.month - modified.month

    if months_ago <= 0:
        return "1. This month"
    elif months_ago == 1:
        return "2. Last month"
    elif months_ago <= 6:
        return "3. 2 to 6 months ago"
    elif months_ago <= 12:
        return "4. 6 to 12 months ago"
    else:
        return "5. Over 1 year ago"

# An item is a cleanup candidate when nothing depends on it or uses it and it has not
# been modified for over a year. This is a shortlist to review, not a deletion list.

def get_cleanup_candidate(dependencies_count, modified_band):
    if dependencies_count == 0 and modified_band == "5. Over 1 year ago":
        return "Yes"
    return "No"

# Build one row for the Items table.

def add_attributes(item, dependencies):

    items_dict = {}

    items_dict["item_id"] = item.id
    items_dict["item_title"] = item.title
    items_dict["item_owner"] = item.owner
    items_dict["item_type"] = item.type

    items_dict["item_page_url"] = get_item_page_url(item.id)

    level, groups = get_item_sharing_parts(item)
    items_dict["item_sharing"] = get_sharing_label(level, groups)
    items_dict["access_level"] = level
    items_dict["has_groups"] = "Yes" if groups else "No"
    items_dict["item_groups"] = join_group_names(groups)
    items_dict["item_status"] = get_status(item.content_status)

    items_dict["item_date_created"] = item.created
    items_dict["item_date_modified"] = item.modified
    items_dict["modified_band"] = get_modified_band(item.modified)

    if not dependencies:
        items_dict["has_dependencies"] = "No"
        items_dict["dependencies_count"] = 0

    else:
        items_dict["has_dependencies"] = "Yes"
        items_dict["dependencies_count"] = len(dependencies)

    items_dict["dependencies_band"] = get_dependencies_band(items_dict["dependencies_count"])
    items_dict["cleanup_candidate"] = get_cleanup_candidate(
        items_dict["dependencies_count"], items_dict["modified_band"])

    items_dict["refreshed_at"] = refreshed_at

    return items_dict


# Build one row for the Dependencies table.
#
# This works like add_attributes, but each row describes a pair: the origin item
# (origin_id, origin_title) and one item that depends on it or that it depends on
# (the dependent_* fields, all read from the dependency). It is a serving table for the
# Dashboard lists; the links themselves are stored once in the Item edges table.

def add_dependency_attributes(item, dependency):

    dependency_dict = {}

    dependency_dict["origin_id"] = item.id
    dependency_dict["origin_title"] = item.title
    dependency_dict["origin_item_page_url"] = get_item_page_url(item.id)
    dependency_dict["dependent_id"] = dependency.id
    dependency_dict["dependent_title"] = dependency.title
    dependency_dict["dependent_owner"] = dependency.owner
    dependency_dict["dependent_type"] = dependency.type

    dependent_groups = get_item_groups(dependency)
    dependency_dict["dependent_item_page_url"] = get_item_page_url(dependency.id)
    dependency_dict["dependent_sharing"] = get_item_sharing(dependency)
    dependency_dict["dependent_groups"] = join_group_names(dependent_groups)
    dependency_dict["dependent_status"] = get_status(dependency.content_status)

    dependency_dict["dependent_date_created"] = dependency.created
    dependency_dict["dependent_date_modified"] = dependency.modified

    # "Uses": the origin's own data points at the dependency (a map and its layers).
    # "Used by": the dependency's data points at the origin (an app and its map).
    used = dependency.id in uses_dict.get(item.id, ())
    dependency_dict["direction"] = "Uses" if used else "Used by"
    origin_groups = get_item_groups(item)
    dependency_dict["origin_sharing"] = get_item_sharing(item)
    dependency_dict["origin_groups"] = join_group_names(origin_groups)
    # A conflict is either kind of mismatch: the origin outranks the dependent by level
    # (Private/Organization/Everyone), or the origin is shared with a group the dependent
    # is not also shared with (get_group_conflict) - two different groups both read as
    # "Groups only" by level alone, so the level check can't tell them apart on its own.
    # The group check only applies when the dependent's reach is exactly "Groups only":
    # if it's also Organization- or Everyone-shared, that already reaches everyone the
    # origin's groups could reach, so a missing group name there isn't a real gap.
    dependent_reach = get_sharing_reach(dependency)
    level_conflict = dependent_reach < get_sharing_reach(item)
    group_conflict = dependent_reach == SHARING_REACH["Groups only"] and get_group_conflict(origin_groups, dependent_groups)
    dependency_dict["sharing_conflict"] = "Yes" if used and (level_conflict or group_conflict) else "No"
    reasons = []
    if used and level_conflict:
        reasons.append("Level")
    if used and group_conflict:
        reasons.append("Group")
    dependency_dict["conflict_reason"] = " & ".join(reasons)

    dependency_dict["refreshed_at"] = refreshed_at

    return dependency_dict


# Build one row of the Item edges table: a link from one item to another, stored once.
# relation_kind is "uses" (found in the item's data) or "related" (a service relationship);
# target_state is "found", "external" (an ArcGIS Online item) or "missing".

def make_edge(source_id, target_id, kind, state, evidence):
    return {
        "source_item_id": source_id,
        "target_item_id": target_id,
        "relation_kind": kind,
        "target_state": state,
        "evidence_path": (evidence or "")[:255] or None,
        "refreshed_at": refreshed_at,
    }


# Items on ArcGIS Online that the portal itself cannot return, for example Esri's basemaps
# (World Street Map and similar). A map that uses one works fine, so it is not broken.
arcgis_online_cache = {}

def exists_on_arcgis_online(item_id):
    if item_id not in arcgis_online_cache:
        try:
            response = requests.get(f"https://www.arcgis.com/sharing/rest/content/items/{item_id}",
                                    params={"f": "json"}, timeout=15).json()
            arcgis_online_cache[item_id] = "error" not in response
        except Exception as e:
            # No internet from here: cannot tell, so the reference stays flagged
            log.warning("Could not check ArcGIS Online for item %s: %s", item_id, e)
            arcgis_online_cache[item_id] = False
    return arcgis_online_cache[item_id]

# Check the references that must resolve (see BROKEN_REFERENCE_TYPES). One that is not in the
# portal search, that the portal cannot return either (deleted, or not readable by this
# account) becomes an edge to a "missing" target, unless it is an ArcGIS Online item (then
# "external"). The missing ones also become rows of the Broken References table.
# Returns (edge rows, broken reference rows).

def get_reference_edges():

    edge_rows = []
    broken_rows = []

    for item in all_items:
        for target_id, path in references_dict.get(item.id, {}).items():

            if target_id in items_by_id or target_id in excluded_ids:
                continue

            if target_id not in unknown_id_cache:
                try:
                    unknown_id_cache[target_id] = gis.content.get(target_id)
                except Exception as e:
                    log.warning("Could not look up referenced item %s: %s", target_id, e)
                    scan_stats["failed_lookups"] += 1
                    unknown_id_cache[target_id] = None

            # An item the portal can return already has a "found" edge from the main loop
            if unknown_id_cache[target_id] is not None:
                continue

            state = "external" if exists_on_arcgis_online(target_id) else "missing"
            edge_rows.append(make_edge(item.id, target_id, "uses", state, path))

            if state == "missing":
                broken_rows.append({
                    "origin_id": item.id,
                    "origin_title": item.title,
                    "origin_type": item.type,
                    "origin_owner": item.owner,
                    "origin_item_page_url": get_item_page_url(item.id),
                    "missing_id": target_id,
                    "evidence_path": (path or "")[:255] or None,
                    "refreshed_at": refreshed_at,
                })

    return edge_rows, broken_rows


# Indirect impact: for every item with at least one upstream dependent, walk the upstream
# ("used by") graph breadth-first to find every item reachable two or more hops out - the
# layer's map's app, not just the map. One-hop impact is already the Dependencies table's
# "Used by" rows, so only hop_count >= 2 is kept here; it adds information instead of
# duplicating it. uses_dict (item ID to the set of IDs it directly uses) is the same downstream
# data get_dependencies_dict starts from, before upstream links are merged into it.
# Returns a list of row dicts, deduplicated to the shortest hop count per (origin, affected)
# pair (a BFS visits each node in non-decreasing hop order, so the first visit is shortest).

def get_indirect_impact_rows():

    upstream_of = {}
    for source_id, targets in uses_dict.items():
        for target_id in targets:
            if target_id != source_id:
                upstream_of.setdefault(target_id, set()).add(source_id)

    rows = []
    for origin_id, direct_upstream in upstream_of.items():

        origin_item = items_by_id.get(origin_id)
        if origin_item is None or is_excluded_owner(origin_item.owner):
            continue

        # Breadth-first from the one-hop neighbors; best_hop also doubles as the visited set,
        # seeded with origin_id itself so a cycle can never walk back through it.
        best_hop = {origin_id: 0}
        via = {}
        frontier = [(node, node, 1) for node in direct_upstream]
        while frontier:
            next_frontier = []
            for node, first_hop_node, hop in frontier:
                if node in best_hop:
                    continue
                best_hop[node] = hop
                via[node] = first_hop_node
                if hop >= MAX_IMPACT_DEPTH:
                    continue
                for parent in upstream_of.get(node, ()):
                    if parent not in best_hop:
                        next_frontier.append((parent, first_hop_node, hop + 1))
            frontier = next_frontier

        for affected_id, hop in best_hop.items():
            if hop < 2:
                continue
            affected_item = items_by_id.get(affected_id)
            if affected_item is None or is_excluded_owner(affected_item.owner):
                continue
            via_item = items_by_id.get(via[affected_id])
            rows.append({
                "origin_id": origin_id,
                "origin_title": origin_item.title,
                "affected_id": affected_id,
                "affected_title": affected_item.title,
                "affected_type": affected_item.type,
                "affected_owner": affected_item.owner,
                "affected_item_page_url": get_item_page_url(affected_id),
                "hop_count": hop,
                "via_id": via[affected_id],
                "via_title": via_item.title if via_item else None,
                "refreshed_at": refreshed_at,
            })

    return rows


# ---------------------------------------------------------------------------
# Part IV: Get all items and build the dependencies dictionary
# ---------------------------------------------------------------------------

# Get every item the signed-in account can see. Check that the login points at the
# org or portal you mean to scan.

step_start = time.perf_counter()
found_items = gis.content.search(query="*", max_items=10000)
all_items = [item for item in found_items if not is_excluded_owner(item.owner)]
excluded_ids = {item.id for item in found_items} - {item.id for item in all_items}
log.info("Total portal items found: %d (%d left out by EXCLUDE_OWNERS=%s)",
         len(found_items), len(excluded_ids), ",".join(exclude_owner_patterns))
log_step("Searching portal items", step_start)

# Lookups that save repeated network calls later
items_by_id = {item.id: item for item in all_items}
unknown_id_cache = {}
sharing_cache = {}
references_dict = {}
evidence_dict = {}

# Disk cache of each item's extracted references, keyed by item ID and valid only while its
# modified date, type and the extraction logic below all match (see reference_cache.py). This
# skips item.get_data() - the slowest step per item - for anything unchanged since the last
# run. The signature folds in the settings and type sets that affect what gets extracted, so
# changing CHECK_URLS or any of those sets invalidates the whole cache instead of silently
# reusing results computed under different rules.
cache_signature = [
    portal_base,
    hashlib.sha256(repr((
        sorted(JSON_ITEM_TYPES), sorted(BROKEN_REFERENCE_TYPES), sorted(EXPERIENCE_DATA_SOURCE_TYPES),
        sorted(DASHBOARD_DATA_SOURCE_TYPES), sorted(WEB_MAPPING_APP_KEYS), check_urls,
    )).encode()).hexdigest(),
]
reference_cache = ReferenceCache(os.getenv("REFERENCE_CACHE_FILE", ".cache/references.json"), cache_signature)
log.info("Reference cache: %d entries loaded", len(reference_cache.entries))

# Time of this run, stored on every row so the tables show how current they are
refreshed_at = int(time.time() * 1000)

# Build the dependencies dictionary: item ID to the set of IDs that depend on it or
# that it depends on. The first call collects downstream IDs (found in the item's
# own JSON). The second adds upstream IDs (items whose JSON contains this item's ID).
#
# The tables do not say which direction a dependency runs. The item types make it
# clear: a feature layer is always downstream of a map.

step_start = time.perf_counter()
downstream_dict = get_downstream_dict()
log_step("Scanning item data", step_start)

reference_cache.save()
log.info("Reference cache: %d entries saved", len(reference_cache.current))

# Keep the "uses" direction before upstream links are merged into downstream_dict
uses_dict = {key: set(value) for key, value in downstream_dict.items()}

step_start = time.perf_counter()
dependencies_dict = get_dependencies_dict()
log_step("Adding upstream dependencies", step_start)

# ---------------------------------------------------------------------------
# Part V: Assemble the rows to write
# ---------------------------------------------------------------------------

# Go through the items once more. For each one, look up its dependencies as Item
# objects and build one row for the item, one row per dependency, and one edge for every
# link the item itself uses. Rows are plain dictionaries of field name to value.
items_list = []
dependencies_list = []
edges = {}

step_start = time.perf_counter()
for count, item in enumerate(all_items, start=1):

    if count % 100 == 0:
        log.info("Built rows for %d of %d items", count, len(all_items))

    dependencies = get_dependencies(item.id)

    # The item's own row needs the dependencies too, for the count
    items_list.append(add_attributes(item, dependencies))

    for dependency in dependencies:

        dependencies_list.append(add_dependency_attributes(item, dependency))

        # Each link is stored once, from the item whose own data holds it
        if dependency.id in uses_dict.get(item.id, ()):
            kind, evidence = evidence_dict.get(item.id, {}).get(dependency.id, ("uses", None))
            edges[(item.id, dependency.id, kind)] = make_edge(item.id, dependency.id, kind, "found", evidence)

log.info("Rows to write: %d items, %d dependencies", len(items_list), len(dependencies_list))
log_step("Building table rows", step_start)

step_start = time.perf_counter()
reference_edges, broken_list = get_reference_edges()
for edge in reference_edges:
    edges[(edge["source_item_id"], edge["target_item_id"], edge["relation_kind"])] = edge
edges_list = list(edges.values())
log.info("Item edges: %d (%d external or missing); broken references found: %d",
         len(edges_list), len(reference_edges), len(broken_list))
log_step("Checking references", step_start)

# ---------------------------------------------------------------------------
# Part VI: Find the tables and write the rows
# ---------------------------------------------------------------------------

# The tables live in one hosted item (see create_enterprise_tables.py for the data model).
# If DASHBOARD_TABLES_ID is empty, or the item no longer exists, the tables are created here
# and the new ID is saved to .env so later runs reuse them.
dashboard_tables_item = None
if dashboard_tables_id:
    try:
        dashboard_tables_item = gis.content.get(dashboard_tables_id)
    except Exception:
        dashboard_tables_item = None

if dashboard_tables_item is None:
    from create_enterprise_tables import create_tables
    from dotenv import find_dotenv, set_key

    log.warning("Dashboard tables not found; creating them now")
    dashboard_tables_item = create_tables(gis)
    dashboard_tables_id = dashboard_tables_item.id
    log.info("Created tables item %s", dashboard_tables_id)

    env_path = find_dotenv(usecwd=True)
    if env_path:
        set_key(env_path, "DASHBOARD_TABLES_ID", dashboard_tables_id)
        log.info("Saved DASHBOARD_TABLES_ID to .env")

# Tables, names and fields added in later versions are created on an existing item
from create_enterprise_tables import (BROKEN_FIELDS, CHANGE_FIELDS, DEPENDENCIES_FIELDS, EDGE_FIELDS,
                                      IMPACT_FIELDS, MONITORING_FIELDS, RUN_FIELDS,
                                      drop_fields, ensure_fields, ensure_indexes, ensure_table_names,
                                      ensure_tables, share_with_org)
dashboard_tables_item = ensure_tables(dashboard_tables_item)
try:
    renamed = ensure_table_names(dashboard_tables_item)
    if renamed:
        log.info("Renamed tables: %s", renamed)
        dashboard_tables_item = gis.content.get(dashboard_tables_id)
except Exception as e:
    log.warning("Could not rename the tables (they keep their old names): %s", e)

tables_by_id = {t.properties.id: t for t in dashboard_tables_item.tables}
monitoring_table = tables_by_id[0]
dependencies_table = tables_by_id[1]
broken_table = tables_by_id[2]
edges_table = tables_by_id[4]
run_history_table = tables_by_id[6]
changes_table = tables_by_id[7]
impact_table = tables_by_id[8]

for table, fields, label in ((monitoring_table, MONITORING_FIELDS, "items"),
                             (dependencies_table, DEPENDENCIES_FIELDS, "dependencies"),
                             (broken_table, BROKEN_FIELDS, "broken references"),
                             (edges_table, EDGE_FIELDS, "item edges"),
                             (run_history_table, RUN_FIELDS, "run history"),
                             (changes_table, CHANGE_FIELDS, "changes"),
                             (impact_table, IMPACT_FIELDS, "indirect impact")):
    added_fields = ensure_fields(table, fields)
    if added_fields:
        log.info("Added missing fields to the %s table: %s", label, added_fields)

# Fields removed from the schema in later versions are dropped from existing tables
for table, names, label in ((monitoring_table, ["sharing_conflict", "item_url"], "items"),
                            (dependencies_table, ["dependent_item_url"], "dependencies"),
                            (broken_table, ["reason"], "broken references")):
    dropped_fields = drop_fields(table, names)
    if dropped_fields:
        log.info("Removed fields no longer used from the %s table: %s", label, dropped_fields)

# Bring a table in line with the new rows without ever emptying it: rows whose key already
# exists are updated, new keys are added, and only then are the keys that are gone deleted
# (with any duplicate of a key). Viewers never see an empty table, a failed run leaves the
# old data in place, and the object IDs stay stable. Batches of 500 keep requests small.
#
# label_field and track_field are optional and only feed the Changes table (see below); they
# have no effect on the sync itself. label_field is a field from the row used purely to
# describe a change in human terms (e.g. an item's title). track_field is a field compared
# between a key's old and new row; when it newly equals track_value (default: any change), that
# key's change is reported as "New" instead of "Added" or "Removed" - used for a dependency that
# newly became a sharing conflict, whether the dependency itself is new or an existing one whose
# sharing_conflict flipped to "Yes".

def sync_table(table, rows, key_fields, label, batch_size=500, label_field=None, track_field=None,
               track_value=None):
    started = time.perf_counter()
    object_id_field = table.properties.objectIdField
    out_fields = list(key_fields) + [object_id_field]
    for field in (label_field, track_field):
        if field and field not in out_fields:
            out_fields.append(field)

    # New rows by key; a repeated key in the new data is a bug in the run, so keep the first
    new_by_key = {}
    for attributes in rows:
        key = tuple(attributes[field] for field in key_fields)
        if key in new_by_key:
            log.error("Duplicate key %s in the new rows for %s; keeping the first", key, label)
            continue
        new_by_key[key] = attributes

    # Rows in the table now: a key that is still wanted keeps its first row, everything else
    # (keys that are gone, duplicates of a key) is deleted at the end
    existing_id_by_key = {}
    existing_track_by_key = {}
    delete_ids = []
    removed = []
    existing = table.query(where="1=1", out_fields=",".join(out_fields), return_all_records=True).features
    for feature in existing:
        key = tuple(feature.attributes.get(field) for field in key_fields)
        object_id = feature.attributes[object_id_field]
        if key in new_by_key and key not in existing_id_by_key:
            existing_id_by_key[key] = object_id
            if track_field:
                existing_track_by_key[key] = feature.attributes.get(track_field)
        else:
            delete_ids.append(object_id)
            if key not in new_by_key:
                removed.append((key, feature.attributes.get(label_field) if label_field else None))

    updates = [{"attributes": {**attributes, object_id_field: existing_id_by_key[key]}}
               for key, attributes in new_by_key.items() if key in existing_id_by_key]
    adds = [{"attributes": attributes} for key, attributes in new_by_key.items() if key not in existing_id_by_key]

    failed = 0
    def apply(kind, items):
        nonlocal failed
        for start in range(0, len(items), batch_size):
            batch = items[start:start + batch_size]
            result = table.edit_features(**{kind: batch})
            for r in result.get(kind[:-1] + "Results", []):
                if not r.get("success"):
                    failed += 1
                    log.error("Failed %s in %s: %s", kind, label, r.get("error"))

    apply("updates", updates)
    apply("adds", adds)
    apply("deletes", delete_ids)
    log.info("%s: %d updated, %d added, %d deleted (failed: %d)", label, len(updates), len(adds),
             len(delete_ids), failed)
    log_step(f"Writing {label}", started)

    if not (label_field or track_field):
        return []
    changes = []
    for key, attributes in new_by_key.items():
        label_value = attributes.get(label_field) if label_field else None
        if key not in existing_id_by_key:
            changes.append({"change_kind": "Added", "key": key, "label": label_value})
        if track_field:
            old_value = existing_track_by_key.get(key)
            new_value = attributes.get(track_field)
            target = new_value if track_value is None else track_value
            if new_value == target and old_value != target:
                changes.append({"change_kind": "New", "key": key, "label": label_value})
    for key, label_value in removed:
        changes.append({"change_kind": "Removed", "key": key, "label": label_value})
    return changes

write_start = time.perf_counter()
item_changes = sync_table(monitoring_table, items_list, ("item_id",), "items table", label_field="item_title")
dependency_changes = sync_table(
    dependencies_table, dependencies_list, ("origin_id", "dependent_id", "direction"), "dependencies table",
    label_field="dependent_title", track_field="sharing_conflict", track_value="Yes")
sync_table(broken_table, broken_list, ("origin_id", "missing_id"), "broken references table")
sync_table(edges_table, edges_list, ("source_item_id", "target_item_id", "relation_kind"), "item edges table")

impact_rows = get_indirect_impact_rows()
sync_table(impact_table, impact_rows, ("origin_id", "affected_id"), "indirect impact table")
log_step("Populating the hosted tables", write_start)


# Run history and Changes are append-only logs, not a synced snapshot: every run adds its own
# rows (keyed to this run by refreshed_at) and old rows are pruned by age, so past runs stay
# visible instead of being overwritten like the tables above.

def append_and_prune(table, rows, label, retention_days, batch_size=500):
    started = time.perf_counter()
    failed = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        result = table.edit_features(adds=[{"attributes": r} for r in batch])
        for r in result.get("addResults", []):
            if not r.get("success"):
                failed += 1
                log.error("Failed add in %s: %s", label, r.get("error"))

    # A raw epoch-millisecond integer is rejected by the service's SQL parser when compared
    # against a Date field ("Invalid data type for expression"); a TIMESTAMP literal is the
    # portable way to write a date comparison in an ArcGIS REST where clause. refreshed_at is
    # stored as UTC (int(time.time() * 1000)), so the cutoff is formatted the same way.
    cutoff_ms = refreshed_at - retention_days * 24 * 60 * 60 * 1000
    cutoff_text = datetime.fromtimestamp(cutoff_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    pruned = 0
    try:
        result = table.delete_features(where=f"refreshed_at < TIMESTAMP '{cutoff_text}'")
        pruned = len(result.get("deleteResults", [])) if result else 0
    except Exception as e:
        log.warning("Could not prune old rows from %s: %s", label, e)

    log.info("%s: %d added (failed: %d), %d rows older than %d days pruned",
             label, len(rows), failed, pruned, retention_days)
    log_step(f"Writing {label}", started)

# Item changes: entity_key is just the item_id. Dependency changes: change_kind "New" means the
# dependency newly became a sharing conflict (see sync_table's track_field), so it is logged as
# its own entity_type rather than as an ordinary dependency change.
change_rows = [
    {"refreshed_at": refreshed_at, "entity_type": "Item", "change_kind": change["change_kind"],
     "entity_key": change["key"][0], "title": change["label"], "detail": None}
    for change in item_changes
]
for change in dependency_changes:
    origin_id, dependent_id, direction = change["key"]
    change_rows.append({
        "refreshed_at": refreshed_at,
        "entity_type": "Sharing conflict" if change["change_kind"] == "New" else "Dependency",
        "change_kind": change["change_kind"],
        "entity_key": f"{origin_id}|{dependent_id}",
        "title": change["label"],
        "detail": direction,
    })
append_and_prune(changes_table, change_rows, "changes", retention_days=14)
log.info("Changes since last scan: %d", len(change_rows))

run_row = {
    "run_started": refreshed_at,
    "run_completed": int(time.time() * 1000),
    "duration_seconds": round(time.perf_counter() - script_start, 1),
    "items_found": len(found_items),
    "items_scanned": len(all_items),
    "items_excluded": len(excluded_ids),
    "cache_hits": scan_stats["cache_hits"],
    "failed_reads": scan_stats["failed_reads"],
    "failed_lookups": scan_stats["failed_lookups"],
    "edges_found": len(edges_list),
    "dependencies_found": len(dependencies_list),
    "sharing_conflicts_found": sum(1 for row in dependencies_list if row["sharing_conflict"] == "Yes"),
    "broken_references_found": len(broken_list),
    "refreshed_at": refreshed_at,
}
append_and_prune(run_history_table, [run_row], "run history", retention_days=14)

# Unique indexes are the keys: they stop a duplicate row from ever being written. They are
# added after the sync so that a table that still held duplicates has been cleaned first.
for table_id, table in tables_by_id.items():
    try:
        added_indexes = ensure_indexes(table, table_id)
        if added_indexes:
            log.info("Added indexes to %s: %s", table.properties.name, added_indexes)
    except Exception as e:
        log.warning("Could not add indexes to %s: %s", table.properties.name, e)

# Who can see the tables (and the dashboard): SHARE_WITH_ORG=true shares them with the
# organization. Anyone who can open the tables sees the titles and owners of all the items.
if share_with_org_enabled:
    try:
        if share_with_org(dashboard_tables_item):
            log.info("Shared the tables item with the organization")
    except Exception as e:
        log.warning("Could not share the tables item with the organization: %s", e)

log.info("Run completed in %s (script start to hosted tables populated)",
         format_duration(time.perf_counter() - script_start))
