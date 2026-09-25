"""Create (or update) the dependencies Dashboard in your portal.

The dashboard is built from dashboard_template/dashboard_template.json, a saved Dashboards
definition (the lists, details panels and filters), which this script then extends with the
cards, charts and lists. That file is required: keep it next to this script and do not edit it.
Every data source in it points at a placeholder table item, so that item ID is replaced with
DASHBOARD_TABLES_ID from .env. It needs nothing else from the original item it was saved from.

Usage:
    python create_dashboard.py
Reads PORTAL_URL, PORTAL_USERNAME, PORTAL_PASSWORD and DASHBOARD_TABLES_ID from .env, and
DASHBOARD_ID (the dashboard item to update; saved there after the first create).
Optional settings in .env:
    DASHBOARD_VERSION       Dashboards version written into the definition (default 4.32.0). It must
                            not be newer than what your portal supports (Enterprise 11.5: 4.32.0).
    DASHBOARD_TITLE         item title (default "Dependencies Monitoring Dashboard")
    DASHBOARD_HEADER_TITLE  title in the dashboard header
Running it again updates the existing dashboard instead of creating a duplicate.
"""
import json
import os
import copy
import re
import uuid
from pathlib import Path

from dotenv import load_dotenv
from arcgis.gis import GIS

from create_enterprise_tables import ACCENT_DEPENDENCIES, ACCENT_ITEMS, pill, set_item_popup, share_with_org

# The titles below read .env, so it is loaded before they are set
load_dotenv()

TEMPLATE_PATH = Path(__file__).parent / "dashboard_template" / "dashboard_template.json"
TEMPLATE_TABLES_ID = "341ef3842d5a4abda4081b79a3f08cda"
TITLE = os.getenv("DASHBOARD_TITLE", "Dependencies Monitoring Dashboard").strip()
DEFAULT_VERSION = "4.32.0"
MAX_ROWS = 10000
PIE_MIN_PERCENT = 4  # item types under this share of the items are grouped into "Other"
HEADER_TITLE = os.getenv("DASHBOARD_HEADER_TITLE", "Portal Dependencies Monitoring").strip()


# Layer ids inside the tables item: 0 = monitoring (items), 1 = dependencies
ITEMS_LAYER = 0
DEPENDENCIES_LAYER = 1

# Widget ids from the template that the new layout reuses
ITEMS_LIST_ID = "e63f5cdd-c8a2-4f1d-a37b-3cad1a602204"
DEPENDENCIES_LIST_ID = "f22d1673-1010-40cd-887a-fbb2ed71ecdc"
ITEM_DETAILS_ID = "cd194387-f76f-43d6-bdeb-9c1ab2437e21"
DEPENDENCY_DETAILS_ID = "3dfcf76b-7cf8-4cd8-87c7-6a65ec71a3f2"

# Anchored on the dashboard's own accent colors (ACCENT_ITEMS, ACCENT_DEPENDENCIES) rather
# than a stock palette, so the pie chart reads as this dashboard's, not a generic template's
PALETTE = [
    [0, 122, 194], [46, 158, 138], [201, 138, 43], [125, 96, 152], [91, 122, 153],
    [178, 90, 62], [107, 144, 128], [138, 115, 96], [163, 102, 122], [61, 74, 87],
]

_STATE = {"verticalAlignment": "middle", "showTopCaption": True, "showBottomCaption": True}


def _new_id():
    return str(uuid.uuid4())


def _dataset(tables_id, layer, stat_field, stat_name, group_by=None, order_by=None, filter_=None,
             stat_type="count"):
    dataset = {
        "type": "serviceDataset",
        "name": "main",
        "dataSource": {"type": "layerDataSource", "itemId": tables_id, "layerId": layer},
        "groupByFields": group_by or [],
        "orderByFields": order_by or [],
        "statisticDefinitions": [
            {"onStatisticField": stat_field, "statisticType": stat_type, "outStatisticFieldName": stat_name}
        ],
        "clientSideStatistics": False,
        "outFields": ["*"],
        "returnDistinctValues": False,
        "allowSourceDownload": False,
        "allowSummaryDownload": False,
    }
    if filter_:
        dataset["filter"] = filter_
    return dataset


# (background, number color) per card kind; the Arcade expression below applies them.
# review/warning/problem step through gold -> orange -> red with a deliberate hue jump
# between each so severity is distinguishable at a glance, not just on close reading.
# These are the single source of truth: every heading, pill and highlight color that
# represents the same severity below reuses them instead of repeating its own hex value.
CARD_STYLES = {
    "items": ("#eaf4fb", "#007ac2"),
    "neutral": ("#f3f5f7", "#3d4a57"),
    "review": ("#fbf1d9", "#8a6d00"),
    "dependencies": ("#e8f6f3", "#1d7a6a"),
    "problem": ("#fbe2e0", "#b3261e"),
    "warning": ("#fbe6da", "#c1440e"),
}
REVIEW_BG, REVIEW_ACCENT = CARD_STYLES["review"]
WARNING_BG, WARNING_ACCENT = CARD_STYLES["warning"]
PROBLEM_BG, PROBLEM_ACCENT = CARD_STYLES["problem"]

# The exact palette _panel_header uses (neutral box, navy title, gray hint), for the handful of
# _heading_card indicators that should look like a plain _panel_header box instead of a colored
# card - as close as an indicator widget's fixed native layout can get to that look
PANEL_BG, PANEL_TITLE, PANEL_HINT = "#f5f8fb", "#12324a", "#5f6b7a"


def _highlight(hex_color, target_luminance=100, base_alpha=128, min_alpha=90, max_alpha=170):
    """Selection-highlight tint for a list. Alpha is scaled by perceived luminance so accent
    colors of very different lightness (e.g. a light teal vs. a dark rust) still read as
    roughly the same highlight strength when painted over a selected row."""
    r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    alpha = max(min_alpha, min(max_alpha, round(base_alpha * target_luminance / max(luminance, 1))))
    return f"{hex_color}{alpha:02x}"


def _indicator(tables_id, name, caption, layer, stat_field, stat_name, filter_=None, caption_size=16, style=None,
               stat_type="count"):
    indicator = {
        "id": _new_id(),
        "name": name,
        "showLastUpdate": False,
        "noDataState": dict(_STATE),
        "noFilterState": dict(_STATE),
        "datasets": [_dataset(tables_id, layer, stat_field, stat_name, filter_=filter_, stat_type=stat_type)],
        "type": "indicatorWidget",
        "defaultSettings": {
            "topSection": {"fontSize": 80, "textInfo": {}},
            "middleSection": {"fontSize": 32, "textInfo": {"text": "{calculated/value}"}},
            "bottomSection": {"fontSize": caption_size, "textInfo": {"text": caption}},
        },
        "comparison": "none",
        "valueFormat": {
            "name": "value", "prefix": False, "style": "decimal", "useGrouping": True,
            "minimumFractionDigits": 0, "maximumFractionDigits": 0,
        },
        "percentageFormat": {"name": "percentage", "prefix": False, "style": "percent", "useGrouping": True},
        "ratioFormat": {"name": "ratio", "prefix": False, "style": "decimal", "useGrouping": True, "maximumFractionDigits": 2},
        "valueType": "statistic",
        "noValueState": dict(_STATE),
    }
    if style:
        # Colors on an indicator are set with an Arcade expression: a tinted background, the
        # number in the accent color, the label underneath
        background, accent = CARD_STYLES[style]
        indicator["arcadeEnabled"] = True
        indicator["expression"] = (
            "\nreturn {\n"
            "  textColor: '#33475b',\n"
            f"  backgroundColor: '{background}',\n"
            f"  middleText: Text($datapoint.{stat_name}, '#,##0'),\n"
            f"  middleTextColor: '{accent}',\n"
            "  middleTextMaxSize: 'large',\n"
            f"  bottomText: '{caption}',\n"
            "  bottomTextMaxSize: 'small'\n"
            "}\n")
    return indicator


def _heading_card(indicator, title, hint, background, color, size=20, hint_color=None):
    """Turn an indicator into the heading of a list: the title with the count, e.g. "Used by (3)",
    and a one-line hint underneath. These bars run the full width of a column, so the font size is
    capped explicitly (both the base size and the Arcade max) or the text fills the width. `size`
    is bumped for a tab's primary heading so it outranks a secondary count badge next to a list.
    `hint_color` lets the hint use a muted gray while the title keeps its own accent, matching the
    two-tone look of the plain _panel_header boxes (Item details, All items); it defaults to
    `color` for callers that want the title and hint in one matching accent, as before."""
    stat_name = indicator["datasets"][0]["statisticDefinitions"][0]["outStatisticFieldName"]
    indicator["defaultSettings"]["middleSection"]["fontSize"] = size
    indicator["defaultSettings"]["bottomSection"]["fontSize"] = 12
    indicator["arcadeEnabled"] = True
    indicator["expression"] = (
        "\nreturn {\n"
        f"  textColor: '{hint_color or color}',\n"
        f"  backgroundColor: '{background}',\n"
        f"  middleText: '{title} (' + Text($datapoint.{stat_name}, '#,##0') + ')',\n"
        f"  middleTextColor: '{color}',\n"
        "  middleTextMaxSize: 'small',\n"
        f"  bottomText: '{hint}',\n"
        "  bottomTextMaxSize: 'small'\n"
        "}\n")


def _pie(tables_id, name, title, field, stat_name, values, min_slice_percent):
    """Pie chart of item counts per value of `field`. Slices under `min_slice_percent`
    are combined into one gray "Other" slice. `values` fixes the colors, largest first."""
    infos = [
        {
            "label": value,
            "symbol": {
                "type": "esriSFS", "style": "esriSFSSolid", "color": PALETTE[n % len(PALETTE)] + [255],
                "outline": {"type": "esriSLS", "style": "esriSLSSolid", "width": 0},
            },
            "value": value,
        }
        for n, value in enumerate(values)
    ]
    font = {"family": "inherit", "size": 11, "style": "normal", "weight": "normal"}
    grey = {"type": "esriSFS", "style": "esriSFSSolid", "color": [214, 214, 214, 255],
            "outline": {"type": "esriSLS", "style": "esriSLSSolid", "width": 0}}
    return {
        "id": _new_id(),
        "name": name,
        "title": title,
        "showLastUpdate": False,
        "noDataState": dict(_STATE),
        "noFilterState": dict(_STATE),
        "datasets": [_dataset(tables_id, ITEMS_LAYER, field, stat_name, group_by=[field],
                              order_by=[f"{stat_name} DESC"])],
        "actionMode": "none",
        "categoryType": "groupByValues",
        "type": "pieChartWidget",
        "chartConfig": {
            "version": "18.1.0",
            "type": "chart",
            "orderOptions": {},
            "colorMatch": True,
            "chartRenderer": {"type": "uniqueValue", "field1": field, "uniqueValueInfos": infos},
            "legend": {
                "type": "chartLegend", "visible": True,
                "body": {"type": "esriTS", "angle": 0, "font": dict(font)}, "position": "left",
                "displayPercentage": False, "displayNumericValue": True,
                "labelMaxWidth": 200, "valueLabelMaxWidth": 50,
            },
            "series": [{
                "type": "pieSeries", "id": "main", "name": "main", "x": field, "y": stat_name,
                "dataLabels": {"type": "chartText", "visible": True,
                               "content": {"type": "esriTS", "angle": 0, "font": dict(font)}},
                "dataTooltipVisible": True, "dataTooltipReverseColor": True,
                "optimizeDataLabelsOverlapping": True, "alignDataLabels": True,
                "innerRadius": 50, "startAngle": 270,
                "ticks": {"type": "pieTick",
                          "lineSymbol": {"type": "esriSLS", "style": "esriSLSSolid", "color": [214, 214, 214, 127.5]}},
                "fillSymbol": grey,
                "dataLabelsOffset": 10,
                "sliceGrouping": {"sliceId": "__other-slice__", "percentageThreshold": min_slice_percent,
                                  "fillSymbol": {**grey, "outline": {"type": "esriSLS", "style": "esriSLSSolid", "width": 1}}},
            }],
        },
    }


BROKEN_LAYER = 2
RUN_HISTORY_LAYER = 6
CHANGES_LAYER = 7
IMPACT_LAYER = 8
IMPACT_BG, IMPACT_ACCENT = "#f3edf9", "#7b5ea7"  # the same purple _top_depended_chart uses:
# both panels are about cascading risk from deleting something, so sharing a hue ties them together


def _yes_filter(field, value="Yes"):
    return {"type": "filterGroup", "condition": "OR", "rules": [{
        "type": "filterGroup", "condition": "AND", "rules": [{
            "type": "filterRule", "field": {"name": field, "type": "string"},
            "operator": "equal", "constraint": {"type": "value", "value": value},
        }],
    }]}


def _features_dataset(tables_id, layer, order_by, max_features, filter_=None, download=False):
    """A dataset that returns table rows as they are (no statistics)."""
    dataset = {
        "type": "serviceDataset",
        "name": "main",
        "dataSource": {"type": "layerDataSource", "itemId": tables_id, "layerId": layer},
        "groupByFields": [],
        "orderByFields": order_by,
        "statisticDefinitions": [],
        "maxFeatures": max_features,
        "clientSideStatistics": False,
        "outFields": ["*"],
        "returnDistinctValues": False,
        "allowSourceDownload": download,
        "allowSummaryDownload": False,
    }
    if filter_:
        dataset["filter"] = filter_
    return dataset


def _axis(text, angle=0, category=False):
    font = {"family": "inherit", "style": "normal", "weight": "normal"}
    line = {"type": "esriSLS", "style": "esriSLSSolid", "width": 1}
    axis = {
        "type": "chartAxis", "visible": True,
        "title": {"type": "chartText", "visible": True,
                  "content": {"type": "esriTS", "angle": angle, "font": {**font, "weight": "bold"}, "text": text}},
        "valueFormat": ({"type": "category", "characterLimit": 24} if category else
                        {"type": "number", "intlOptions": {"style": "decimal", "notation": "standard",
                                                            "minimumFractionDigits": 0, "maximumFractionDigits": 0}}),
        "lineSymbol": dict(line),
        # Category names are long item titles, so they are drawn vertically to stay readable
        "labels": {"type": "chartText", "visible": True,
                   "content": {"type": "esriTS", "angle": 270 if category else 0, "font": dict(font)}},
        "grid": dict(line), "guides": [],
    }
    if category:
        axis["scrollbar"] = {"width": 15, "gripSize": 22}
    else:
        axis["buffer"] = True
    return axis


def _serial_chart(name, title, dataset, category_type, series, category_title, value_title, legend=False):
    font = {"family": "inherit", "style": "normal", "weight": "normal"}
    return {
        "id": _new_id(),
        "name": name,
        "title": title,
        "showLastUpdate": False,
        "noDataState": dict(_STATE),
        "noFilterState": dict(_STATE),
        "datasets": [dataset],
        "actionMode": "monoSelection",
        "categoryType": category_type,
        "type": "serialChartWidget",
        "valueFormat": {"name": "value", "prefix": False, "style": "decimal", "useGrouping": True, "maximumFractionDigits": 0},
        "labelFormat": {"name": "label", "prefix": False, "style": "decimal", "useGrouping": True, "maximumFractionDigits": 0},
        "category": {"labelOverrides": [], "nullLabel": "Unknown", "blankLabel": "Unknown"},
        "parseDates": False,
        "minPeriod": "MM",
        "categoryAxisLabelsBehavior": "hide",
        "chartConfig": {
            "version": "18.1.0",
            "type": "chart",
            "orderOptions": {},
            "colorMatch": False,
            "axes": [_axis(category_title, category=True), _axis(value_title, angle=270)],
            "series": series,
            "legend": {"type": "chartLegend", "visible": legend,
                       "body": {"type": "esriTS", "angle": 0, "font": dict(font)}, "position": "bottom"},
            "horizontalAxisLabelsBehavior": "hide",
            "cursorCrosshair": {"type": "cursorCrosshair", "verticalLineVisible": False, "horizontalLineVisible": False},
            "stackedType": "sideBySide",
        },
    }


def _bar_series(series_id, name, x, y, color):
    text = {"type": "chartText", "visible": True,
            "content": {"type": "esriTS", "angle": 0,
                        "font": {"family": "inherit", "style": "normal", "weight": "normal"}}}
    return {
        "type": "barSeries", "id": series_id, "name": name, "x": x, "y": y, "dataLabels": text,
        "dataTooltipVisible": True, "dataTooltipReverseColor": True,
        "fillSymbol": {"type": "esriSFS", "style": "esriSFSSolid", "color": color + [255],
                       "outline": {"type": "esriSLS", "style": "esriSLSSolid", "color": color + [255], "width": 1}},
    }


def _top_depended_chart(tables_id):
    """The 10 items with the most dependencies: the riskiest ones to delete."""
    dataset = _features_dataset(tables_id, ITEMS_LAYER, ["dependencies_count DESC"], 10)
    chart = _serial_chart(
        "Most Depended-On Items Chart", None, dataset, "features",
        [_bar_series("dependencies_count", "Dependencies", "item_title", "dependencies_count", PALETTE[0])],
        "Item", "Dependencies")
    chart.pop("title")
    chart["topCaption"] = _panel_header(
        "Most depended-on items", "The 10 items with the most dependencies.", "#7b5ea7",
        "check what relies on these before changing or removing them.")
    return chart


def _table(name, caption, dataset, columns):
    """Table widget; `columns` is a list of (field, title)."""
    return {
        "id": _new_id(),
        "name": name,
        "topCaption": caption,
        "showLastUpdate": False,
        "noDataState": dict(_STATE),
        "noFilterState": dict(_STATE),
        "datasets": [dataset],
        "arcadeEnabled": False,
        "type": "tableWidget",
        "dataSettings": {"type": "features", "valueFields": [field for field, _ in columns]},
        "tableSettings": {
            "layout": "fit-data", "scale": "medium", "hoverText": False, "rowStriping": False,
            "selectionMode": "single", "verticalGrid": {"thickness": 1}, "horizontalGrid": {"thickness": 1},
            "headerSettings": {"rule": {"thickness": 3}},
        },
        "columnSettings": [
            {"title": title, "fieldName": field, "textAlign": "left", "isBold": False, "isItalic": False,
             "isUnderlined": False}
            for field, title in columns
        ],
        "editingEnabled": False,
        "editColumnPlacement": "end",
    }


def _open_link(url_field, color, label="Open item page"):
    """Small inline link to an item's page, for a list entry that is too dense for the
    full-size button _link() draws in a popup."""
    return (f'<a href="{{field/{url_field}}}" target="_blank" rel="noopener noreferrer" '
            f'style="font-size:11px;font-weight:600;color:{color};text-decoration:none;">{label} &#8594;</a>')


def _risk_list(template, tables_id, name, layer, order_by, text, note, filter_=None):
    """A searchable list of cards, built from the template list widget."""
    widget = copy.deepcopy(template)
    widget.update({"id": _new_id(), "name": name, "text": text, "topCaption": note, "showFilter": True})
    widget.pop("events", None)
    widget["datasets"] = [_features_dataset(tables_id, layer, order_by, MAX_ROWS, filter_=filter_)]
    return widget


def _panel_header(title, subtitle, color, action=None, inline=False):
    """Header shown above a panel: a colored bar, the title, a hint and optionally what to do.
    With inline=True the title and hint share one line, to save height."""
    if inline:
        return (f'<div style="padding:6px 12px;margin:6px 8px 4px;border-left:4px solid {color};'
                f'background:#f5f8fb;border-radius:4px;line-height:1.4;">'
                f'<span style="font-size:15px;font-weight:700;color:#12324a;">{title}</span>'
                f'<span style="font-size:12px;color:#5f6b7a;margin-left:10px;">{subtitle}</span></div>')
    action_html = (f'<div style="font-size:12px;color:#33475b;line-height:1.4;margin-top:4px;">'
                   f'<b>What to do:</b> {action}</div>') if action else ""
    return (f'<div style="padding:8px 12px;margin:6px 8px 4px;border-left:4px solid {color};'
            f'background:#f5f8fb;border-radius:4px;">'
            f'<div style="font-size:16px;font-weight:700;color:#12324a;line-height:1.3;">{title}</div>'
            f'<div style="font-size:12px;color:#5f6b7a;line-height:1.4;margin-top:2px;">{subtitle}</div>'
            f'{action_html}</div>')


def _item(element_id, width, height):
    return {"width": width, "height": height, "type": "itemLayoutElement", "id": element_id}


def _stack(orientation, width, height, elements):
    return {"width": width, "height": height, "id": _new_id(), "elements": elements,
            "type": "stackLayoutElement", "orientation": orientation}


HAS_DEPENDENCIES_SELECTOR_ID = "3331e148-1f0b-48c5-b057-e6370a364147"
ITEM_TYPE_SELECTOR_ID = "70a6e96c-26d5-4c66-8e2a-d6ea2c26ba70"
SHARING_SELECTOR_ID = "bb1454ba-1cec-42fa-9bbe-43b3f6a5968d"


def _category_selector(template, name, label, field):
    """Copy a template category selector and point it at another field of the items table."""
    selector = copy.deepcopy(template)
    selector["id"] = _new_id()
    selector["name"] = name
    selector["label"] = label
    dataset = selector["datasets"][0]
    dataset["groupByFields"] = [field]
    dataset["orderByFields"] = [f"{field} ASC"]
    stat_name = f"COUNT_{field.upper()}"
    dataset["statisticDefinitions"] = [
        {"onStatisticField": field, "statisticType": "count", "outStatisticFieldName": stat_name}
    ]
    return selector


def _svg(path):
    """Icon on the same fixed 16x16 canvas as every other filter icon, so they all render
    at the same visual weight."""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" class="icon " role="button" viewBox="0 0 16 16">'
            f'<path d="{path}"/></svg>')


FILTER_ICONS = {
    "Dependencies Count Selector": _svg(
        "M6 2h4v3H6zM1 11h4v3H1zM11 11h4v3h-4zM7.3 5h1.4v3H13v3h-1.4V9.4H4.4V11H3V8h4.3z"),
    "Item Type Selector": _svg(
        "M1 1v13h15V1H1zm6 12H2v-2h5v2zm0-3H2V8h5v2zm0-3H2V5h5v2zm4 6H8v-2h3v2zm0-3H8V8h3v2zm0-3H8V5h3v2zm4 6h-3v-2h3v2zm0-3h-3V8h3v2zm0-3h-3V5h3v2zm0-3H2V2h13v2z"),
    "Owner Selector": _svg("M8 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm0 1.2c-3.1 0-6 1.4-6 3.6V14h12v-1.2c0-2.2-2.9-3.6-6-3.6z"),
    "Sharing Selector": _svg(
        "M12.5 10a2.5 2.5 0 0 0-1.7.7L6.2 8.4a2.6 2.6 0 0 0 0-.8l4.5-2.3A2.5 2.5 0 1 0 10 3.5L5.4 5.8a2.5 2.5 0 1 0 0 4.4L10 12.5a2.5 2.5 0 1 0 2.5-2.5z"),
    "Last Modified Selector": _svg(
        "M8 3.998H7v-2h1zm7-1v1.25a.75.75 0 0 1-.75.75h-1.5a.75.75 0 0 1-.75-.75v-1.25H9v1.25a.75.75 0 0 1-.75.75h-1.5a.75.75 0 0 1-.75-.75v-1.25H4v4h13v-4zm-1-1h-1v2h1zm-10 6v9h13v-9zm4 8H5v-3h3zm0-4H5v-3h3zm4 4H9v-3h3zm0-4H9v-3h3zm4 4h-3v-3h3zm0-4h-3v-3h3z"),
}


def change_filters(selectors):
    """Drop the status and created-date filters, add owner and dependency-count filters.

    Status has almost no variety (2 of about 1100 items differ) and was not wired to anything
    in the template. Created date is rarely useful; modified date becomes a range filter.
    """
    by_id = {s["id"]: s for s in selectors}
    has_dependencies = by_id[HAS_DEPENDENCIES_SELECTOR_ID]
    item_type = by_id[ITEM_TYPE_SELECTOR_ID]
    sharing = by_id[SHARING_SELECTOR_ID]
    # The template's date selector relies on relative-date SQL (INTERVAL) that hosted
    # tables on this Enterprise version reject, so it filtered nothing. A category
    # selector on the precomputed modified_band field works everywhere.
    modified = _category_selector(has_dependencies, "Last Modified Selector", "Last modified", "modified_band")

    owner = _category_selector(sharing, "Owner Selector", "Filter by Owner", "item_owner")
    band = _category_selector(has_dependencies, "Dependencies Count Selector", "Dependencies",
                              "dependencies_band")
    # The owner selector was copied from the sharing one, so its "show all" text says sharing
    owner["selection"]["noneLabel"] = "All Owners"

    # "Has dependencies?" is dropped: the "None" range of the dependency-count filter
    # gives the same answer. Cleanup-candidate and sharing-conflict filters were tried
    # and dropped as redundant (two other filters give the shortlist, and the Risks tab
    # lists the sharing conflicts).
    selectors[:] = [band, item_type, owner, sharing, modified]

    # The panel is already titled "Filters", so drop "Filter:" / "Filter by" from each label
    for selector in selectors:
        selector["label"] = re.sub(r"^Filter(?: by|:)\s*", "", selector["label"])
        if selector["name"] in FILTER_ICONS:
            selector["icon"] = FILTER_ICONS[selector["name"]]


def build_health_section(tables_id, view, items_list_template, object_id_field="OBJECTID"):
    """Scan health (recent runs) and recent changes, as their own tab: operational context
    about the scan itself, not item data, so it is deliberately left out of `linked` in
    add_overview_and_merge_details and stays unaffected by the Item Type / Owner / ... filters."""
    runs_table = _table(
        "Recent Runs Table",
        _panel_header("Recent runs", "Newest first. Coverage is items scanned out of items found; a gap "
                      "there usually means EXCLUDE_OWNERS changed, not a problem.", "#3d4a57"),
        _features_dataset(tables_id, RUN_HISTORY_LAYER, ["run_started DESC"], 100),
        [(object_id_field, "ID"), ("run_started", "Run started"), ("duration_seconds", "Duration (s)"),
         ("items_found", "Items found"), ("items_scanned", "Items scanned"),
         ("cache_hits", "Cache hits"), ("failed_reads", "Failed reads"),
         ("failed_lookups", "Failed lookups"), ("dependencies_found", "Dependencies"),
         ("sharing_conflicts_found", "Conflicts"), ("broken_references_found", "Broken refs")])

    # A table instead of the card list every other list here uses: a card is fine for a
    # handful of risks, but Changes can grow to dozens of rows a run, and cards do not scan at
    # that volume. Sorted by run first (newest run's changes together, then the run before),
    # then by kind within a run so the "New" conflicts - the ones worth reacting to - sort
    # ahead of ordinary adds/removes instead of being scattered alphabetically by title.
    changes_table = _table(
        "Changes Table",
        _panel_header("Recent changes", "Items and dependencies added or removed, and dependencies that "
                      "newly became a sharing conflict, in roughly the last 2 weeks. Newest run first.",
                      "#3d4a57"),
        _features_dataset(tables_id, CHANGES_LAYER, ["refreshed_at DESC", "change_kind ASC", "title ASC"],
                          MAX_ROWS),
        [(object_id_field, "ID"), ("refreshed_at", "When"), ("change_kind", "Change"), ("entity_type", "Type"),
         ("title", "Title"), ("entity_key", "Key")])

    # Inactive has its own count card on the Explore tab already, but nowhere to actually see
    # which items it covers. A card list (not a table, like the other two here): the Dashboards
    # Table widget has no search box at all, and this is the one of the three someone is likely
    # to want to search by title, so it keeps the list widget's proven showFilter search instead.
    # Oldest modified first, since those are the most overdue for review.
    mono = "font-family:Consolas,Menlo,monospace;font-size:11px;"
    inactive_list = _risk_list(
        items_list_template, tables_id, "Inactive Items List", ITEMS_LAYER, ["item_date_modified ASC"],
        '<div style="padding:2px 0;">'
        '<div style="font-size:15px;font-weight:600;color:#12324a;line-height:1.3;">{field/item_title}</div>'
        f'<div style="margin-top:4px;">{pill("{field/item_type}", "#e6f2fb", "#005a94")}'
        f'{pill("{field/item_owner}", "#f1f3f5", "#495057")}</div>'
        f'<div style="margin-top:3px;{mono}color:#8a95a3;">Item ID {{field/item_id}}</div>'
        f'<div style="margin-top:4px;">{_open_link("item_page_url", REVIEW_ACCENT)}</div></div>',
        _panel_header("Inactive items", "No dependencies and not modified in over a year. A list to "
                      "review, not to delete: some of these are kept on purpose (reference data, "
                      "archives). Oldest modified first.", REVIEW_ACCENT),
        filter_=_yes_filter("cleanup_candidate"))
    inactive_list["highlightTextColor"] = _highlight(REVIEW_ACCENT)

    view["widgets"].extend([runs_table, changes_table, inactive_list])

    # Recent runs and Recent changes both have several columns that need width, so they share
    # a left column stacked top to bottom; Inactive only needs a narrow card, so it gets a
    # right column instead and uses the full tab height rather than splitting height 3 ways
    return _stack("col", 1, 1, [
        _stack("row", 0.62, 1, [
            _item(runs_table["id"], 1, 0.5),
            _item(changes_table["id"], 1, 0.5),
        ]),
        _item(inactive_list["id"], 0.38, 1),
    ])


def add_overview_and_merge_details(definition, tables_id, type_counts, object_id_field="OBJECTID"):
    """Add a row of indicators and charts on top and show both details panels at once."""
    view = definition["desktopView"]

    total_items = _indicator(tables_id, "Total Items Indicator", "Items", ITEMS_LAYER, "item_id", "COUNT_ITEM_ID",
                             style="items")
    no_dependencies = _indicator(
        tables_id, "Items Without Dependencies Indicator", "Items without dependencies", ITEMS_LAYER,
        "item_id", "COUNT_ITEM_ID",
        filter_={"type": "filterGroup", "condition": "OR", "rules": [{
            "type": "filterGroup", "condition": "AND", "rules": [{
                "type": "filterRule", "field": {"name": "has_dependencies", "type": "string"},
                "operator": "equal", "constraint": {"type": "value", "value": "No"},
            }],
        }]},
        style="neutral",
    )
    total_dependencies = _indicator(
        tables_id, "Total Dependencies Indicator", "Dependencies", DEPENDENCIES_LAYER,
        "dependent_id", "COUNT_DEPENDENT_ID", style="dependencies",
    )
    # Slices under PIE_MIN_PERCENT are grouped into "Other"; the info button lists exactly which
    # types that covers, since the pie itself has no room to label a slice this thin
    by_type = _pie(tables_id, "Items By Type Pie Chart", "Items by type", "item_type", "COUNT_ITEM_TYPE",
                   [name for name, _ in type_counts], PIE_MIN_PERCENT)
    total_type_count = sum(count for _, count in type_counts) or 1
    other_types = [(name, count) for name, count in type_counts
                   if 100 * count / total_type_count < PIE_MIN_PERCENT]
    if other_types:
        breakdown = ", ".join(f"{name} ({count:,})" for name, count in other_types)
        by_type["moreInfo"] = (
            f'<p><b>What is &quot;Other&quot;?</b></p>'
            f'<p>Every item type that makes up less than {PIE_MIN_PERCENT}% of the items is grouped into '
            f'one gray <b>Other</b> slice, so the chart stays readable. Today that is '
            f'{len(other_types)} types, {sum(c for _, c in other_types):,} items:</p>'
            f'<p>{breakdown}</p>'
            f'<p style="color:#8a95a3;">As of the last dashboard update.</p>')
    else:
        by_type["moreInfo"] = (
            f'<p><b>What is &quot;Other&quot;?</b></p>'
            f'<p>Every item type that makes up less than {PIE_MIN_PERCENT}% of the items is grouped into '
            f'one gray <b>Other</b> slice, so the chart stays readable. No type is currently that small.</p>')

    # Inactive items are only a "look at these" list: an item nothing uses and nobody has
    # edited for a year may still be kept on purpose (reference data, archives)
    inactive = _indicator(
        tables_id, "Inactive Items Indicator", "Inactive",
        ITEMS_LAYER, "item_id", "COUNT_ITEM_ID", filter_=_yes_filter("cleanup_candidate"), style="review")
    # The info button on the card carries the definition, so the label stays short
    inactive["moreInfo"] = (
        "<p><b>Inactive items</b> have no dependencies and were not modified in over a year.</p>"
        "<p>This is a list to review, not a list to delete: an item like this may be kept on purpose, "
        "for example as reference data or an archive.</p>")
    broken_references = _indicator(
        tables_id, "Broken References Indicator", "Broken references", BROKEN_LAYER,
        "missing_id", "COUNT_MISSING_ID", style="problem")
    sharing_conflicts = _indicator(
        tables_id, "Sharing Conflicts Indicator", "Sharing conflicts", DEPENDENCIES_LAYER,
        "dependent_id", "COUNT_DEPENDENT_ID", filter_=_yes_filter("sharing_conflict"), style="warning")

    overview = [total_items, no_dependencies, inactive, total_dependencies, broken_references,
                sharing_conflicts, by_type]

    top_depended = _top_depended_chart(tables_id)

    view["widgets"].extend(overview + [top_depended])
    items_list = next(w for w in view["widgets"] if w["id"] == ITEMS_LIST_ID)

    # Risks tab: same treatment as Used by / Uses / Indirect impact - a real _panel_header on
    # the list itself instead of a separate indicator card, so the heading matches All items
    # exactly (including a real bordered box) instead of an indicator's fixed card shape. The
    # count moves to the list's native row counter. The "what it means / what to do" boxes that
    # used to sit above each list, always visible, move into the info button instead, next to
    # what used to be the indicator's own "what's covered" popover - one info button per list
    # instead of two separate explanations in two different places.
    mono = "font-family:Consolas,Menlo,monospace;font-size:11px;"
    broken_list = _risk_list(
        items_list, tables_id, "Broken References List", BROKEN_LAYER, ["origin_title ASC"],
        '<div style="padding:2px 0;">'
        '<div style="font-size:15px;font-weight:600;color:#12324a;line-height:1.3;">{field/origin_title}</div>'
        f'<div style="margin-top:4px;">{pill("{field/origin_type}", "#e6f2fb", "#005a94")}'
        f'{pill("{field/origin_owner}", "#f1f3f5", "#495057")}</div>'
        f'<div style="margin-top:5px;padding:4px 8px;background:{PROBLEM_BG};border-radius:6px;font-size:11px;'
        f'color:{PROBLEM_ACCENT};">Missing item <span style="{mono}">{{field/missing_id}}</span></div>'
        f'<div style="margin-top:3px;{mono}color:#8a95a3;">Item ID {{field/origin_id}}</div>'
        f'<div style="margin-top:4px;">{_open_link("origin_item_page_url", ACCENT_ITEMS)}</div></div>',
        "")
    broken_list["topCaption"] = _panel_header(
        "Broken references", "Web maps, Dashboards and apps missing a data source", PROBLEM_ACCENT, inline=True)
    broken_list["moreInfo"] = (
        "<p><b>What's covered:</b></p>"
        "<ul style=\"margin:0;padding-left:18px;\">"
        "<li><b>Web maps</b>: their layers and tables (the basemap is left out)</li>"
        "<li><b>Experience Builder apps</b>: their web map, web scene, feature layer and feature "
        "service data sources</li>"
        "<li><b>Dashboards</b>: each Map widget's web map, and each widget's or selector's dataset</li>"
        "<li><b>Instant Apps / Web AppBuilder apps</b>: their configured web map</li>"
        "</ul>"
        "<p>Other item types (StoryMaps, Hub sites, ...) are not checked yet, to avoid noise. This "
        "list can be expanded: see <code>BROKEN_REFERENCE_TYPES</code> in "
        "Portal_Monitoring_Dependencies.py.</p>"
        "<p><b>What it means:</b> a web map uses a layer or table, or a Dashboard, Experience "
        "Builder app or Instant App uses a web map, feature layer or feature service, that was "
        "deleted or that this account cannot read, so that part fails to load. An ID can also be "
        "stale: the layer still exists under a new item ID.</p>"
        "<p><b>What to do:</b> open the item and re-select the missing layer, table or web map.</p>")
    conflicts_list = _risk_list(
        items_list, tables_id, "Sharing Conflicts List", DEPENDENCIES_LAYER, ["origin_title ASC"],
        '<div style="padding:2px 0;">'
        '<div style="font-size:13px;font-weight:600;color:#12324a;line-height:1.3;">{field/origin_title}</div>'
        f'<div style="margin-top:4px;">{pill("Shared: {field/origin_sharing}", WARNING_BG, WARNING_ACCENT)}</div>'
        f'<div style="margin-top:5px;padding:4px 8px;background:{PROBLEM_BG};border-radius:6px;font-size:11px;'
        f'color:{PROBLEM_ACCENT};">Uses <b>{{field/dependent_title}}</b> '
        f'{pill("Shared: {field/dependent_sharing}", PROBLEM_BG, PROBLEM_ACCENT)}</div>'
        f'<div style="margin-top:3px;font-size:11px;color:#8a95a3;"><b style="color:#5f6b7a;">Item ID:</b> '
        f'<span style="{mono}">{{field/origin_id}}</span></div>'
        # Which check(s) tripped: Level, Group, or both. Without this a card can look
        # unexplained, e.g. matching group names on both sides while it's still flagged
        # because the origin is also shared with Everyone and the dependent is not.
        f'<div style="margin-top:2px;font-size:11px;color:#8a95a3;"><b style="color:#5f6b7a;">Flagged by:</b> '
        '{field/conflict_reason}</div>'
        # Blank when the conflict is level-only (no groups on either side) - a group
        # mismatch is only one of the two ways a conflict can happen, not always present
        f'<div style="margin-top:2px;font-size:11px;color:#8a95a3;"><b style="color:#5f6b7a;">Groups:</b> '
        '{field/origin_groups} vs {field/dependent_groups}</div>'
        f'<div style="margin-top:4px;display:flex;gap:14px;">'
        f'{_open_link("origin_item_page_url", ACCENT_ITEMS)}'
        f'{_open_link("dependent_item_page_url", ACCENT_DEPENDENCIES, "Open used item")}'
        f'</div></div>',
        "",
        filter_=_yes_filter("sharing_conflict"))
    conflicts_list["topCaption"] = _panel_header(
        "Sharing conflicts", "Items shared more widely than something they use", WARNING_ACCENT, inline=True)
    conflicts_list["moreInfo"] = (
        "<p><b>What it means:</b> the item can be seen by more people than something it uses, so "
        "some viewers get an empty or broken result. \"Flagged by\" says why: <b>Level</b> means "
        "the item is shared more broadly (e.g. Everyone) than what it uses; <b>Group</b> means "
        "they're shared with different specific groups.</p>"
        "<p><b>What to do:</b> share the used item with the same audience, or narrow the item's "
        "sharing.</p>")
    view["widgets"].extend([broken_list, conflicts_list])

    # The template's Dependencies list becomes "Used by" (the items that would be affected if
    # the selected item were deleted); a copy becomes "Uses" (what the selected item is built
    # from). Only "Used by" drives Dependency details: two lists selecting into one panel would
    # combine their filters and match nothing.
    #
    # The heading is a real _panel_header on the list itself - the exact same mechanism "All
    # items" uses - rather than a separate indicator card: an indicator widget always renders
    # in its own fixed centered-card shape, so it can never take on _panel_header's bordered-box
    # look no matter what an Arcade expression returns. The trade-off is the count no longer
    # sits in the title text; it shows through the list's own native row counter instead, same
    # as "All items" already does.
    used_by_list = next(w for w in view["widgets"] if w["id"] == DEPENDENCIES_LIST_ID)
    used_by_list["name"] = "Used By List"
    uses_list = copy.deepcopy(used_by_list)
    uses_list["id"] = _new_id()
    uses_list["name"] = "Uses List"
    uses_list.pop("events", None)
    for widget, direction in ((used_by_list, "Used by"), (uses_list, "Uses")):
        dataset = widget["datasets"][0]
        dataset["filter"] = _yes_filter("direction", direction)
        dataset["orderByFields"] = ["dependent_type ASC", "dependent_title ASC"]
        widget["showFilter"] = True
    used_by_list["topCaption"] = _panel_header(
        "Used by", "Affected if you delete the item selected in All items.", WARNING_ACCENT, inline=True)
    used_by_list["moreInfo"] = (
        "<p><b>What it shows:</b> every item that would break if you deleted the item selected in "
        "All items - one step out. A web map appears here if it uses the selected layer; an app "
        "appears here if it uses the selected web map.</p>"
        "<p><b>Why it matters:</b> this is the direct blast radius of a delete. It answers "
        "\"who is using this, right now\" before you remove or unshare something.</p>"
        "<p><b>Use case:</b> you are about to delete an old feature layer. Used by lists the 2 web "
        "maps that reference it, so you know to check with their owners first, or update those maps "
        "before the layer disappears.</p>")
    uses_list["topCaption"] = _panel_header(
        "Uses", "What the item selected in All items is built from.", "#1d7a6a", inline=True)
    uses_list["moreInfo"] = (
        "<p><b>What it shows:</b> what the selected item is built from - the opposite direction "
        "from Used by. A web map's Uses list is its layers and tables; an app's Uses list is the "
        "web maps and services it was configured with.</p>"
        "<p><b>Why it matters:</b> an item's own dependencies are what make it work. If one of "
        "them is deleted, moved, or loses sharing, the selected item is what breaks.</p>"
        "<p><b>Use case:</b> a dashboard shows \"Data source error\" for one widget. Select the "
        "dashboard, open Uses, and check each referenced layer's sharing and whether it still "
        "exists - one of them is usually the cause.</p>")
    select_prompt = {
        "text": '<p style="text-align:center;">Select an item in All items.</p>',
        "verticalAlignment": "middle", "showTopCaption": True, "showBottomCaption": True,
    }
    for widget in (used_by_list, uses_list):
        widget["noFilterState"] = dict(select_prompt)
    view["widgets"].append(uses_list)

    # Indirect impact: items reachable two or more hops upstream of the selected item (the
    # layer's map's app, not just the map). Direct (1-hop) impact is already "Used by" /
    # Dependency details, so this list deliberately excludes hop_count == 1 instead of
    # repeating it. Wired the same way as Used by / Uses below (selection_targets, further
    # down) rather than a static filter here.
    impact_list = _risk_list(
        items_list, tables_id, "Indirect Impact List", IMPACT_LAYER, ["hop_count ASC", "affected_title ASC"],
        '<div style="padding:2px 0;">'
        '<div style="font-size:14px;font-weight:600;color:#12324a;line-height:1.3;">{field/affected_title}</div>'
        f'<div style="margin-top:4px;">{pill("{field/affected_type}", "#e6f2fb", "#005a94")}'
        f'{pill("{field/hop_count} hops", IMPACT_BG, IMPACT_ACCENT)}</div>'
        '<div style="margin-top:3px;font-size:11px;color:#8a95a3;">via {field/via_title}</div>'
        f'<div style="margin-top:2px;{mono}color:#8a95a3;">Item ID {{field/affected_id}}</div>'
        f'<div style="margin-top:4px;">{_open_link("affected_item_page_url", IMPACT_ACCENT)}</div></div>',
        "")
    impact_list["topCaption"] = _panel_header(
        "Indirect impact", "What breaks beyond direct use, for the item selected in All items.",
        IMPACT_ACCENT, inline=True)
    impact_list["moreInfo"] = (
        "<p><b>What it shows:</b> everything two or more steps out from the selected item - the "
        "map's app, not just the map. Used by only shows the first step; this is everything past "
        "that. The \"via\" line on each card names the direct item the chain passes through.</p>"
        "<p><b>Why it matters:</b> Used by can look small (\"only 2 web maps use this layer\") while "
        "hiding that those 2 maps power 5 apps between them. This is the only place that full chain "
        "shows up.</p>"
        "<p><b>Use case:</b> before deleting a hosted feature layer, Used by shows 2 direct web "
        "maps. Indirect impact shows those 2 maps are used by 5 different apps - so the real impact "
        "of deleting the layer is 7 items, not 2.</p>")
    impact_list["noFilterState"] = dict(select_prompt)
    view["widgets"].append(impact_list)

    # Layout: two tabs, Explore (overview row on top, then the three columns) and Risks.
    # The Explore tab is the original three columns, with Item Details above
    # Dependency Details in the right column instead of their own tabs.
    # The overview is a strip of the six number cards (item counts, then dependency-related
    # counts) and the type chart beside them.
    # The chart's actual size is capped by the row's height, not its width (the top row is only
    # 19% of the page tall), so giving it a wide slot just adds blank margin on both sides instead
    # of a bigger pie. Keep its width close to what it needs, and give the rest to the cards.
    # The cards' text is capped to a fixed size too (see _indicator's middleTextMaxSize), so a
    # 3-columns-x-2-rows grid left each card far wider than that capped text needed, with a lot
    # of colored empty space on either side. Laying the six out in a single row instead roughly
    # halves each card's width to match, at the cost of no longer visually grouping items-counts
    # above dependency-counts.
    # total_items and total_dependencies are the two headline metrics, so they get more width
    # than the four risk counts instead of splitting the row into six identical tiles
    pie_width = 0.30
    secondary_width = (1 - pie_width) / 6.6
    primary_width = 1.3 * secondary_width
    number_cards = [total_items, no_dependencies, inactive, total_dependencies, broken_references,
                     sharing_conflicts]
    primary_cards = {total_items["id"], total_dependencies["id"]}

    top = _stack("col", 1, 0.19, [
        *[_item(card["id"], primary_width if card["id"] in primary_cards else secondary_width, 1)
          for card in number_cards],
        _item(by_type["id"], pie_width, 1),
    ])

    def tab(name, layout):
        return {**layout, "tabName": name}

    # The overview row belongs to the Explore tab only, so the Risks tab shows nothing from it
    explore = tab("Explore", _stack("row", 1, 1, [
        top,
        _stack("col", 1, 0.81, [
            _item(ITEMS_LIST_ID, 0.28, 1),
            # Used By is the only thing left in this column now that Uses moved into the
            # tabs on the right, so its list gets the column's full height instead of half.
            # The heading is the list's own topCaption now (see above), so the list gets the
            # slot's full height - no separate heading row to carve out space for.
            _item(DEPENDENCIES_LIST_ID, 0.24, 1),
            # Details (Item Details on top of Dependency Details, as before) and Uses share
            # this slot as tabs, so each gets the full height when it's open
            {"width": 0.48, "height": 1, "id": _new_id(), "type": "tabsLayoutElement", "elements": [
                tab("Details", _stack("row", 1, 1, [
                    _item(ITEM_DETAILS_ID, 1, 0.5),
                    _item(DEPENDENCY_DETAILS_ID, 1, 0.5),
                ])),
                tab("Uses", _stack("row", 1, 1, [_item(uses_list["id"], 1, 1)])),
                tab("Indirect impact", _stack("row", 1, 1, [_item(impact_list["id"], 1, 1)])),
            ]},
        ]),
    ]))
    risks_tab = tab("Risks", _stack("col", 1, 1, [
        _item(top_depended["id"], 0.34, 1),
        _item(broken_list["id"], 0.33, 1),
        _item(conflicts_list["id"], 0.33, 1),
    ]))

    health_tab = tab("Health", build_health_section(tables_id, view, items_list, object_id_field))

    tabs = {"width": 1, "height": 1, "id": _new_id(), "type": "tabsLayoutElement",
            "elements": [explore, risks_tab, health_tab]}
    view["layout"]["rootElement"] = {
        "width": 1, "height": 1, "id": _new_id(), "elements": [tabs],
        "type": "stackLayoutElement", "orientation": "row",
    }

    change_filters(view["sidebar"]["selectors"])

    # The filters also filter the item-level widgets
    linked = [total_items, no_dependencies, inactive, by_type, top_depended]

    def link(node, widgets):
        if isinstance(node, dict):
            targets = node.get("targets")
            if isinstance(targets, list):
                source = next((t for t in targets if t.get("targetId") == f"{ITEMS_LIST_ID}#main"), None)
                if source:
                    targets.extend({**source, "targetId": f"{w['id']}#main"} for w in widgets)
            for value in node.values():
                link(value, widgets)
        elif isinstance(node, list):
            for value in node:
                link(value, widgets)

    for selector in view["sidebar"]["selectors"]:
        link(selector.get("events"), linked)

    # Styled list entries and panel headers (inline HTML/CSS: Dashboards ignores <style>
    # blocks and classes). The list's search box only searches the fields shown in each
    # entry, so the item ID stays visible to keep items searchable by ID as well as title.
    items_list["text"] = (
        '<div style="padding:2px 0;">'
        '<div style="font-size:15px;font-weight:600;color:#12324a;line-height:1.3;">{field/item_title}</div>'
        f'<div style="margin-top:4px;">{pill("{field/item_type}", "#e6f2fb", "#005a94")}'
        f'{pill("{field/dependencies_count} dependencies", "#f1f3f5", "#495057")}</div>'
        '<div style="margin-top:3px;font-size:11px;color:#8a95a3;"><b style="color:#5f6b7a;">Item ID:</b> '
        f'<span style="{mono}">{{field/item_id}}</span></div></div>')
    def dependency_entry(type_background, type_color, show_details=False):
        # Title and type always share one row (flex, title truncated with an ellipsis if it
        # doesn't fit) instead of stacking, so each card is a single compact line even in this
        # narrow column. Owner and item ID are optional: Used By drops them, since clicking a
        # row already shows the same facts (and more) in Dependency Details; Uses has no such
        # drill-down (clicking a row does nothing), so it keeps them - it's the only place
        # that information appears.
        details = (
            '<div style="margin-top:3px;font-size:11px;color:#8a95a3;">Owner: {field/dependent_owner}</div>'
            '<div style="margin-top:2px;font-size:11px;color:#8a95a3;"><b style="color:#5f6b7a;">Item ID:</b> '
            f'<span style="{mono}">{{field/dependent_id}}</span></div>'
        ) if show_details else ""
        return (
            '<div style="padding:2px 0;">'
            '<div style="display:flex;align-items:center;gap:8px;">'
            '<div style="font-size:14px;font-weight:600;color:#12324a;line-height:1.3;flex:1;'
            'min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'
            '{field/dependent_title}</div>'
            f'{pill("{field/dependent_type}", type_background, type_color)}'
            f'</div>{details}</div>')

    used_by_list["text"] = dependency_entry(WARNING_BG, WARNING_ACCENT)
    uses_list["text"] = dependency_entry("#e3f4f1", "#1d6f61", show_details=True)

    # The click/selected highlight on a list row is barely visible by default (a thin left
    # border). "highlightTextColor" is the JSON key behind the list widget's "Highlight color"
    # style setting (confirmed against a hand-authored dashboard in this portal) - an 8-digit
    # #RRGGBBAA hex painted over the selected row. Give each list a bolder, on-brand tint, with
    # alpha scaled per color (see _highlight) so the selection reads with the same strength
    # across lists even though their accent colors differ a lot in lightness.
    items_list["highlightTextColor"] = _highlight(ACCENT_ITEMS)
    used_by_list["highlightTextColor"] = _highlight(WARNING_ACCENT)
    uses_list["highlightTextColor"] = _highlight(ACCENT_DEPENDENCIES)
    broken_list["highlightTextColor"] = _highlight(PROBLEM_ACCENT)
    conflicts_list["highlightTextColor"] = _highlight(WARNING_ACCENT)

    headers = {
        ITEMS_LIST_ID: ("All items", "Search by item name or ID, then select an item to see its dependencies.",
                        ACCENT_ITEMS),
        ITEM_DETAILS_ID: ("Item details", "The item selected in All items.", ACCENT_ITEMS),
        DEPENDENCY_DETAILS_ID: ("Dependency details", "The selected row in Used by.", ACCENT_DEPENDENCIES),
    }
    for widget in view["widgets"]:
        if widget["id"] in headers:
            widget["topCaption"] = _panel_header(*headers[widget["id"]], inline=True)
    # Used by / Uses / Indirect impact set their own topCaption directly where they're built,
    # above; nothing to clear here now that they are not sharing a widget with a heading card.

    # Item Details should only react to a selection in the items list. The sidebar
    # filters also targeted it (without requiring a selection), so it listed every
    # matching item ("1 of 1493") before anything was clicked.
    def unlink_item_details(node):
        if isinstance(node, dict):
            targets = node.get("targets")
            if isinstance(targets, list):
                targets[:] = [t for t in targets if t.get("targetId") != f"{ITEM_DETAILS_ID}#main"]
            for value in node.values():
                unlink_item_details(value)
        elif isinstance(node, list):
            for value in node:
                unlink_item_details(value)

    unlink_item_details(view["sidebar"])

    # Dependency Details only depended on the dependencies list. That list is empty
    # until an item is picked, and the panel then showed every dependency row
    # ("1 of 1000"). Make it require an item selection as well.
    selection_targets = items_list["events"][0]["actions"][0]["targets"]
    for target_id in (DEPENDENCY_DETAILS_ID, uses_list["id"], impact_list["id"]):
        selection_targets.append({
            "targetId": f"{target_id}#main", "by": "whereClause", "requiresSelection": True,
            "fieldMap": [{"sourceName": "item_id", "targetName": "origin_id"}],
        })

    # Show the item / dependency title at the top of each details panel (from the popup title)
    details_more_info = {
        ITEM_DETAILS_ID: (
            "<p><b>What it shows:</b> every stored fact about the item selected in All items - "
            "owner, sharing, when it was created and last modified, its dependency count, and a "
            "link to open it in the portal.</p>"
            "<p><b>Why it matters:</b> the All items list only has room for a title, type and "
            "dependency count. This is where you see everything else without leaving the "
            "dashboard.</p>"
            "<p><b>Use case:</b> an item looks stale in the list. Item Details shows exactly when "
            "it was last modified and who owns it, so you know who to ask before touching it.</p>"),
        DEPENDENCY_DETAILS_ID: (
            "<p><b>What it shows:</b> every stored fact about whichever row you select in Used "
            "by - the same kind of detail as Item Details, but about that one relationship: the "
            "dependent item's owner, sharing, dates, and whether it is a sharing conflict.</p>"
            "<p><b>Why it matters:</b> Used by's cards are deliberately compact. This is where you "
            "confirm the specifics - especially the sharing conflict flag - before acting.</p>"
            "<p><b>Use case:</b> a Used by card is tagged \"sharing conflict\". Select it, then "
            "check Dependency Details to see the exact sharing level on each side and decide "
            "whether to widen the used item's sharing or narrow the dependent's.</p>"),
    }
    for widget in view["widgets"]:
        if widget["id"] in (ITEM_DETAILS_ID, DEPENDENCY_DETAILS_ID):
            widget["showTitle"] = True
            widget["moreInfo"] = details_more_info[widget["id"]]

    # Prompt shown by each details panel until something is selected
    prompts = {
        ITEM_DETAILS_ID: "Select an item in All items.",
        DEPENDENCY_DETAILS_ID: "Select an item in All items, then a row in Used by.",
    }
    for widget in view["widgets"]:
        if widget["id"] in prompts:
            widget["noFilterState"] = {
                "text": f'<p style="text-align:center;">{prompts[widget["id"]]}</p>',
                "verticalAlignment": "middle", "showTopCaption": True, "showBottomCaption": True,
            }

    # Move the filters from the left sidebar into the header bar; without the sidebar
    # the layout gets the full width
    view["header"]["selectors"] = view.pop("sidebar")["selectors"]


def build_definition(tables_id, version=DEFAULT_VERSION, type_counts=(), object_id_field="OBJECTID"):
    """Return the dashboard JSON with data sources repointed at our tables."""
    if not TEMPLATE_PATH.exists():
        raise SystemExit(f"Dashboard template not found: {TEMPLATE_PATH}")
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    if TEMPLATE_TABLES_ID not in text:
        raise SystemExit("Template does not contain the expected table item ID")

    definition = json.loads(text.replace(TEMPLATE_TABLES_ID, tables_id))

    # The template's logo points at an image hosted elsewhere; the dashboard has none
    definition["desktopView"]["header"].pop("logoImageURL", None)
    definition["desktopView"]["header"].update(
        title=HEADER_TITLE, backgroundColor=ACCENT_ITEMS, textColor="#ffffff")

    # The template was saved by a newer Dashboards (4.33) than Enterprise 11.5 opens
    definition["version"] = version
    definition["authoringAppVersion"] = version

    add_overview_and_merge_details(definition, tables_id, list(type_counts), object_id_field)

    # The template limits the lists and details panels to 1000 rows; this portal has more
    # items (and far more dependencies) than that
    view = definition["desktopView"]
    for widget in view["widgets"]:
        for dataset in widget.get("datasets", []):
            if dataset.get("maxFeatures") == 1000:
                dataset["maxFeatures"] = MAX_ROWS

    # Header filters (Item Type, Owner, Sharing, ...) are capped at 50 in the template: the
    # group-by/count query behind each filter is fed only its first 50 raw rows, so categories
    # outside that slice silently vanish from the filter and the ones that do show are
    # undercounted. There is no safe fixed number to swap in instead, so the cap is removed
    # entirely and the filters read the whole table, same as every other widget here.
    for selector in view["header"]["selectors"]:
        for dataset in selector.get("datasets", []):
            dataset.pop("maxFeatures", None)
    return definition


def main():
    load_dotenv()
    tables_id = os.environ["DASHBOARD_TABLES_ID"].strip()
    gis = GIS(os.environ["PORTAL_URL"], os.environ["PORTAL_USERNAME"], os.environ["PORTAL_PASSWORD"])

    tables_item = gis.content.get(tables_id)
    if tables_item is None:
        raise SystemExit(f"Tables item {tables_id} not found; run Portal_Monitoring_Dependencies.py first")

    # Item types in the items table with their counts, most common first, so the biggest
    # slices get the strongest colors and the "Other" list can be worked out
    items_table = {t.properties.id: t for t in tables_item.tables}[ITEMS_LAYER]
    rows = items_table.query(
        group_by_fields_for_statistics="item_type",
        out_statistics=[{"statisticType": "count", "onStatisticField": "item_id", "outStatisticFieldName": "n"}],
        order_by_fields="n DESC", return_all_records=True).features
    type_counts = [(row.attributes["item_type"], row.attributes["n"]) for row in rows if row.attributes["item_type"]]
    # Item Details shows the title and fields set in the table's popup, so make sure
    # existing tables have the current ordering
    set_item_popup(tables_item)

    # Some portals lowercase this to "objectid" (see ensure_fields in create_enterprise_tables.py);
    # the Health tab's ID columns need the real name, not an assumed one, or they render blank
    object_id_field = items_table.properties.objectIdField

    definition = build_definition(tables_id, os.getenv("DASHBOARD_VERSION", DEFAULT_VERSION).strip(), type_counts,
                                  object_id_field)
    item_properties = {
        "title": TITLE,
        "type": "Dashboard",
        "typeKeywords": "Dashboard, Operations Dashboard, ArcGIS Dashboards",
        "snippet": "Items in the portal and the dependencies between them.",
        "tags": "dependencies, monitoring",
        "text": json.dumps(definition),
    }

    # Update the dashboard whose item ID is in .env (DASHBOARD_ID); a title search is only
    # the fallback, since titles can match more than one item
    dashboard_id = os.getenv("DASHBOARD_ID", "").strip()
    if dashboard_id:
        existing = [gis.content.get(dashboard_id)]
    else:
        existing = [
            i for i in gis.content.search(f'title:"{TITLE}" AND type:Dashboard', max_items=50)
            if i.owner == gis.users.me.username and i.title == TITLE
        ]
    if existing:
        existing[0].update(item_properties=item_properties)
        item = existing[0]
        print(f"Updated dashboard {item.id}")
    else:
        # gis.content.add fails in arcgis 2.4.3 on Python 3.14 (_is_geoenabled is
        # missing), so call the addItem REST endpoint directly
        username = gis.users.me.username
        result = gis._con.post(
            f"{gis._portal.resturl}content/users/{username}/addItem",
            {**item_properties, "f": "json"},
        )
        if not result.get("success"):
            raise SystemExit(f"addItem failed: {result}")
        item = gis.content.get(result["id"])
        print(f"Created dashboard {item.id}")

    if not dashboard_id:
        from dotenv import find_dotenv, set_key
        env_path = find_dotenv(usecwd=True)
        if env_path:
            set_key(env_path, "DASHBOARD_ID", item.id)
            print("Saved DASHBOARD_ID to .env")

    # SHARE_WITH_ORG=true shares the dashboard with the whole organization, like the tables item
    if os.getenv("SHARE_WITH_ORG", "false").strip().lower() in ("1", "true", "yes") and share_with_org(item):
        print("Shared the dashboard with the organization")

    print(f"Open: {gis.url}/apps/opsdashboard/index.html#/{item.id}")


if __name__ == "__main__":
    main()
