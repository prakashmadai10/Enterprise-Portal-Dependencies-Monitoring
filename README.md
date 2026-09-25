# Portal dependencies monitor

See how items in your ArcGIS Enterprise portal depend on each other before you change or delete them.

> If I delete this, what breaks?

The monitor scans portal content, writes the results to hosted tables, and displays them in an ArcGIS Dashboard. Use it to:

- Find the maps and apps that depend on an item, directly or through a chain of other items.
- Find broken references in web maps, Experience Builder apps, Dashboards, and Instant Apps.
- Spot sharing conflicts where an item is shared more widely than something it uses.
- Review older items with no detected dependencies. The scan reads references and modification dates; it does not measure usage.
- Browse content by item type or owner, with filters that apply across the dashboard.

Sharing checks compare access level and group names, not actual group membership. See [Limits and dependencies](#9-limits-and-dependencies) for this and other constraints.

## Contents

1. [Requirements](#1-requirements)
2. [Setup](#2-setup)
3. [Scheduling](#3-scheduling)
4. [Configuration](#4-configuration)
5. [Using the dashboard](#5-using-the-dashboard)
6. [How it works](#6-how-it-works)
7. [Data model](#7-data-model)
8. [Troubleshooting](#8-troubleshooting)
9. [Limits and dependencies](#9-limits-and-dependencies)
10. [Files](#10-files)

## 1. Requirements

| Requirement | Details |
|---|---|
| Portal | Tested on ArcGIS Enterprise 11.5. Other versions are unverified. It should also work with ArcGIS Online (set `PORTAL_URL` to your organization's URL) to get the same all-in-one dashboard there, but that has not been tested. |
| Account | An administrator account, preferably a dedicated service account. It must be able to read all items, including other users' private content, create items, and publish hosted layers. Organization-wide sharing also requires sharing privileges. `preflight.py` checks these requirements. |
| Machine | Windows or Linux with Python and network access to the portal. Tested with Python 3.14 and `arcgis` 2.4.3. Access to `www.arcgis.com` is optional; see [section 9](#9-limits-and-dependencies). |
| Time | Allow about 30 minutes for setup. A scan of roughly 1,100 items takes about 8 minutes; larger portals take longer. |

## 2. Setup

### Step 1: Copy the project files

Copy the entire project folder to the machine that will run the scan. Keep the scripts, `requirements.txt`, `.env.example`, and the `dashboard_template` folder together. The [file list](#10-files) explains each file's purpose.

The dashboard requires `dashboard_template/dashboard_template.json`. Keep this file unchanged: the scripts refer to widget IDs inside it.

### Step 2: Create a Python environment

In PowerShell:

```powershell
cd "path\to\this\folder"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

On Linux or macOS:

```bash
cd "path/to/this/folder"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Step 3: Configure `.env`

Copy `.env.example` to `.env` and enter the portal URL, username, and password. Keep `.env` private because it contains credentials. It is included in `.gitignore`.

Environment variables take precedence over values in `.env`, so a scheduler or secret store can supply the credentials instead.

| Setting | Description |
|---|---|
| `PORTAL_URL` | Portal address ending in `/portal`, for example `https://gis.example.com/portal`. Leave blank only when running in an ArcGIS Notebook on the portal. |
| `PORTAL_USERNAME`, `PORTAL_PASSWORD` | Credentials for the account that runs the scan. |
| `EXCLUDE_OWNERS` | Comma-separated owners to exclude; `*` is supported as a wildcard. The default, `esri_*`, excludes Esri system accounts and their content, such as styles and license extensions. |
| `SHARE_WITH_ORG` | Set to `true` to share the tables and dashboard with the organization when their scripts run. Default: `false`. Read step 9 before enabling it. |
| `DASHBOARD_VERSION` | Dashboard definition version. See step 7. |
| `DASHBOARD_TITLE`, `DASHBOARD_HEADER_TITLE` | Optional titles for the dashboard item and its header. |
| `CHECK_URLS` | Also detect references that contain a service URL without an item ID. This roughly doubles scan time and usually finds few additional links. Default: `false`. |
| `LOG_FILE`, `LOG_LEVEL` | Log path and verbosity. The default path is `logs/dependencies_monitor.log`; use `INFO` or `DEBUG` for the level. |
| `DASHBOARD_TABLES_ID`, `DASHBOARD_ID` | Leave blank during setup. The scripts save these IDs after creating the tables and dashboard. |

### Step 4: Run the preflight check

```bash
python preflight.py
```

This check makes no changes. It reports the portal version, account role and privileges, visible item counts and owners, existing dashboard versions, and connectivity to ArcGIS Online.

Every check should show `[ok]`. Resolve any `[!!]` messages before continuing:

- **Fewer items than expected:** check that the account is an administrator.
- **Exactly 10,000 items:** the search may have reached its configured limit and missed content. See [section 9](#9-limits-and-dependencies).
- **No dashboard found:** create and save an empty dashboard in the portal, then rerun the check.

### Step 5: Run the scan

```bash
python Portal_Monitoring_Dependencies.py
```

On its first run, the scan creates the hosted tables item, fills the tables, adds their keys, and saves `DASHBOARD_TABLES_ID` in `.env`. If `SHARE_WITH_ORG=true`, it also shares the tables with the organization.

Progress is logged every 100 items. A run of roughly 1,100 items takes about 8 minutes. The output includes dependency totals, table update counts, and elapsed time. For example, a later run might report:

```text
Item edges: 1496 (4 external or missing); broken references found: 4
items table: 1141 updated, 0 added, 0 deleted (failed: 0)
Run completed in 7m 50.9s (script start to hosted tables populated)
```

The first run reports added rows. Check the log for any `WARNING` or `ERROR` entries.

### Step 6: Check the tables

In the portal, open `Portal_Dependencies_Monitoring`, owned by the account that ran the scan. It should contain seven tables:

- Items
- Dependencies
- Broken references
- Item edges
- Run history
- Changes
- Indirect impact

Check that their row counts match the log before creating the dashboard.

### Step 7: Set the dashboard version

A dashboard definition newer than the portal supports will fail to open with this message:

> This is a newer dashboard item version than your app supports

Use the highest version reported by `preflight.py` for a dashboard created in this portal.

| Portal | `DASHBOARD_VERSION` |
|---|---|
| Enterprise 11.5 | `4.32.0` (default; confirmed) |
| Other Enterprise versions | Start with the highest locally created dashboard version reported by preflight. Lower it if the dashboard will not open. |

Set the value in `.env`, for example:

```dotenv
DASHBOARD_VERSION=4.32.0
```

### Step 8: Create the dashboard

```bash
python create_dashboard.py
```

The script creates the dashboard, saves `DASHBOARD_ID` in `.env`, and prints the dashboard URL. Open it and check the **Explore**, **Risks**, and **Health** tabs.

Running the script again updates the same dashboard.

### Step 9: Choose who can see the results

**Anyone with access to the tables or dashboard can see the titles and owners of scanned items, including private items.** Choose the audience accordingly.

To share with the whole organization, set `SHARE_WITH_ORG=true` and run the scan and dashboard scripts. For a smaller audience, share both items manually with the appropriate group.

The tables and dashboard must have the same audience. Viewers who can open the dashboard but cannot access its tables will see “Data source error.”

## 3. Scheduling

Schedule `Portal_Monitoring_Dependencies.py` to keep the results current. Once a day is a reasonable starting point. A frequently changing portal may need two or three runs a day; larger portals may need a less frequent schedule because scans take longer.

The scan updates existing rows, adds new rows, and deletes removed rows last. It does not empty the tables before writing, so the dashboard stays populated during updates and previous data remains available if a run fails.

For Windows, follow the [Esri guide to scheduling a Python script with Task Scheduler](https://community.esri.com/t5/python-documents/schedule-a-python-script-using-windows-task/ta-p/915861).

Run `create_dashboard.py` only when the dashboard design changes. It does not need a schedule.

## 4. Configuration

Most settings live in `.env`. The following changes require editing the scan or dashboard script.

| Change | Setting or location |
|---|---|
| Excluded owners | `EXCLUDE_OWNERS` in `.env` |
| Item types scanned for references | `JSON_ITEM_TYPES` in `Portal_Monitoring_Dependencies.py` |
| Item types checked for broken references | `BROKEN_REFERENCE_TYPES` and `EXPERIENCE_DATA_SOURCE_TYPES` in the scan |
| Cleanup criteria | `get_cleanup_candidate` and the bands in the scan. Default: no dependencies and no modification in over a year. |
| Item types grouped into the pie chart's “Other” slice | `PIE_MIN_PERCENT` in `create_dashboard.py`. Default: types below 4% each. |
| Maximum indirect-impact depth | `MAX_IMPACT_DEPTH` in the scan. Default: 6 hops. |
| History retention | `retention_days` in the `append_and_prune` calls near the end of the scan. Default: 14 days each for Run history and Changes. |
| Dashboard colors, layout, and cards | `create_dashboard.py` |
| Search limit | `max_items=10000` in both the scan and `preflight.py`. See [section 9](#9-limits-and-dependencies). |

## 5. Using the dashboard

### Explore

Start in **All items**, where you can search by name or item ID. Select an item to see:

| View | What it shows |
|---|---|
| Used by | Items that depend directly on the selected item. These are directly affected if it is deleted. |
| Uses | Items the selected item depends on. |
| Indirect impact | Items affected two or more hops up the dependency chain. Each card's “via” line identifies the direct item the chain passes through. |
| Details | Item details and dependency details. |

For example, selecting a layer may show its web map under **Used by** and the map's app under **Indirect impact**.

Summary cards count Items, Items without dependencies, Inactive items, Dependencies, Broken references, and Sharing conflicts. The item-type pie chart filters the dashboard when you select a type. **Types in Other** lists the smaller types grouped into the gray slice.

Use the header filters to narrow results by Dependencies, Item Type, Owner, Sharing, or Last modified.

### Risks

Review the ten most depended-on items, broken references, and sharing conflicts.

Broken-reference cards show where a missing item ID appears. An ID may be stale if a layer still exists under a new item ID. Sharing-conflict cards identify items shared more widely than something they use, which can cause failures for viewers without access to the dependency. Each card says whether it's a **Level** conflict (broader sharing, like Everyone) or a **Group** conflict (different specific groups).

### Health

- **Recent runs** shows duration, items found and scanned, cache hits, failed reads and lookups, and dependency, conflict, and broken-reference counts. Use it to check whether the latest run finished and how much content it covered.
- **Recent changes** shows added or removed items and dependencies, plus dependencies that became sharing conflicts. Results cover roughly the last two weeks, newest run first.
- **Inactive items** lists the items counted by the Inactive card, oldest modification first, with search. Review these candidates before deciding what to remove: the scan cannot tell whether people still open them.

## 6. How it works

The scan reads item JSON and looks for 32-character item IDs. It records each reference and builds a reverse lookup so the dashboard can show both directions:

- **Uses (downstream):** a web map references a layer, so the map uses the layer.
- **Used by (upstream):** a dashboard references that web map, so the map is used by the dashboard.

The scan walks the JSON recursively using `find_id_paths` and `collect_references`. This records where each ID was found as `evidence_path` in the Item edges table.

### Why scan item data?

The built-in dependency tools did not provide reliable results for this project's needs:

- `ItemDependency` in the ArcGIS API for Python has had reliability issues; see the [Esri Community discussion](https://community.esri.com/t5/arcgis-api-for-python-questions/dependencies-arcgis-gis-itemdependency-to/m-p/1652176).
- `Item.get_dependencies` returns downstream dependencies, such as a map's layers. It does not return the apps that use the map.
- Esri's Portal for ArcGIS script in support article ARC-000021183 uses `dependent_upon` and `dependent_to`. These are available in Enterprise but returned no useful results in the version tested for this project.

### Performance and caching

Reading each item's JSON once and building a lookup avoids repeatedly searching every item's data for every other item. Network calls still account for most of the runtime, including a sharing-settings request for each item.

`reference_cache.py` stores extracted references in `.cache/references.json`, keyed by item ID. The scan reuses an entry and skips `item.get_data()` while the item's modification timestamp and type remain unchanged.

Edits, type changes, and extraction-related settings such as `CHECK_URLS` invalidate cached entries. Deleted items and failed reads are removed from the cache on the next save.

## 7. Data model

A single hosted item contains seven tables. Each row includes `refreshed_at`, which records its refresh time.

| ID | Table | One row per | Key |
|---|---|---|---|
| 0 | Items | Item | `item_id` |
| 1 | Dependencies | Item, related item, and direction; includes both directions | `origin_id, dependent_id, direction` |
| 2 | Broken references | Reference to a missing item | `origin_id, missing_id` |
| 4 | Item edges | Link between two items, stored once | `source_item_id, target_item_id, relation_kind` |
| 6 | Run history | Scan run, including duration, coverage, and error counts | `run_started` |
| 7 | Changes | Added or removed item or dependency, or a new sharing conflict | None; append-only |
| 8 | Indirect impact | Item reachable at least two hops upstream of another item | `origin_id, affected_id` |

**Item edges** is the core relationship table. **Dependencies** and **Broken references** are derived from it for dashboard use, since Dashboards cannot join tables. Precomputed fields such as `dependencies_count`, bands, and `cleanup_candidate` support calculations the dashboard cannot perform itself.

Tables store plain URLs; the dashboard turns them into links. Unique indexes on the keys prevent duplicate rows.

Most tables are updated in place. **Run history** and **Changes** append rows on each run and prune entries by age. Both retain 14 days by default; adjust their `append_and_prune` calls to change that period.

## 8. Troubleshooting

| Problem | What to check or do |
|---|---|
| “This is a newer dashboard item version than your app supports” | Lower `DASHBOARD_VERSION` and rerun `create_dashboard.py`. |
| “Data source error” | Share the tables with the same audience as the dashboard. If a table is missing from the hosted item's data definition, rerun `create_dashboard.py` to rewrite it. |
| “Unable to execute Arcade script” | Rerun `create_dashboard.py`. If the error persists, check whether the card expression's field names match its query. |
| SSL or certificate error during sign-in | Set `REQUESTS_CA_BUNDLE` to your organization's certificate file so Python can trust the portal certificate. |
| Fewer items than expected | Check administrator access, excluded owners, and the 10,000-item search limit. |
| Slow scan | Network calls account for most of the runtime. About 8 minutes for 1,100 items is the observed baseline. |
| An Esri basemap is reported as broken | Check connectivity to `www.arcgis.com`. Without it, the scan cannot distinguish an Esri-hosted item from a missing one. |
| `Duplicate key` in the log | The scan keeps the first row and reports duplicates. Investigate the cause; the tables remain usable. |
| Empty dependency lists | Select an item in **All items** to populate them. |
| `gis.content.add` fails with `_is_geoenabled` | This is a known issue in the tested combination of `arcgis` 2.4.3 and Python 3.14. The scripts include a workaround. |

## 9. Limits and dependencies

- **Required local template.** `dashboard_template/dashboard_template.json` is a saved dashboard definition. It has no live connection to its original portal or ArcGIS Online items. Keep it unchanged because the scripts rely on its widget IDs.
- **Optional ArcGIS Online lookup.** The scan checks unknown IDs against `www.arcgis.com` to distinguish public items, such as Esri basemaps, from deleted content. Without connectivity, references subject to broken-reference checks can be reported as broken even when the public item exists.
- **Limited testing.** The project was built and tested on one Enterprise 11.5 portal with about 1,100 items after exclusions. Other Enterprise versions are expected to work but have not been verified.
- **Missing and inaccessible items look alike.** A reference is considered broken when its ID is absent from the portal scan, cannot be fetched by the account, and cannot be found on ArcGIS Online. Content the account cannot read and layers moved to new item IDs can therefore appear missing.
- **Targeted broken-reference checks.** Checks cover web-map layers and tables, excluding the basemap; Experience Builder web map, web scene, feature layer, and feature service data sources; Dashboard Map widgets and widget or selector datasets; and configured web maps in Instant Apps and classic Web AppBuilder apps. See `BROKEN_REFERENCE_TYPES` for the configured scope.
- **Group conflicts compare names, not membership.** The group check compares group titles, not who is actually in each group, and only runs when the dependency is shared "Groups only" — if it is also Organization- or Everyone-shared, that already covers anyone a specific group could reach, so no group-level gap is possible.
- **Configured search limit.** The scan and preflight each pass `max_items=10000` to `gis.content.search(...)`. For portals with more than 10,000 items, raise the value in both files.
- **No usage measurement.** “Inactive” is based on detected links and modification dates. An item may still be in use even if nothing in the scan references it.
- **Private-item metadata is visible in the results.** Anyone who can access the tables or dashboard can see scanned item titles and owners, including those of private items.

## 10. Files

| File | Purpose |
|---|---|
| `Portal_Monitoring_Dependencies.py` | Scans references, checks for problems, updates tables, and records run history and changes. |
| `create_enterprise_tables.py` | Defines tables, keys, popups, and sharing helpers. Can also create empty tables when run separately. |
| `create_dashboard.py` | Builds and publishes the dashboard from the local template. |
| `preflight.py` | Checks the account, portal, and network without making changes. |
| `reference_cache.py` | Caches extracted item references between runs. |
| `dashboard_template/dashboard_template.json` | Required base dashboard definition. |
| `requirements.txt` | Python dependencies: `arcgis`, `python-dotenv`, and `requests`. |
| `.env.example` | Configuration template to copy to `.env`. |
| `.cache/references.json` | Cached references used to avoid repeated item-data reads. |
| `logs/` | Scan logs. |

## Future enhancements

- **Relational database and graph view.** The scan could also write its data (starting with the item edges table) into an RDBMS such as PostgreSQL. From there, Neo4j or another graph library (NetworkX, PyVis, D3) could draw the dependency diagram, so relationships can be explored visually. This needs no ArcGIS Knowledge license.

## Credits

The idea of scanning item data for references began with AlderMaps' open `AGOL_Monitoring_Dependencies` notebook in [arcgis-python-api](https://github.com/AlderMaps/arcgis-python-api), written for ArcGIS Online. Esri has since added a native “Used by” portal view that covers part of the direct-dependency use case.

This project has since rebuilt the table schema and dashboard and added Enterprise support, in-place table updates, broken-reference and sharing checks, inactive-item tracking, run history, change tracking, and indirect-impact analysis.
