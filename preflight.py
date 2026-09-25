"""Check that this machine and account are ready to run the dependencies monitor.

Usage:
    python preflight.py
Reads PORTAL_URL, PORTAL_USERNAME and PORTAL_PASSWORD (and DASHBOARD_TABLES_ID, if set) from .env.
It changes nothing. It reports the login, the account's role and privileges, how many items the
account can see, the Dashboards versions already in the portal (to choose DASHBOARD_VERSION),
and whether ArcGIS Online can be reached (used to tell Esri basemaps from broken references).
"""
import os
import platform
import sys
import warnings

warnings.simplefilter("ignore")

import requests
from dotenv import load_dotenv
from arcgis.gis import GIS

load_dotenv()

# Privileges the scripts need, by what they are used for. Names can differ between versions, so a
# "missing" here is a prompt to check, not proof.
PRIVILEGES = {
    "portal:user:createItem": "create the tables and the dashboard",
    "portal:publisher:publishFeatures": "create hosted tables",
    "portal:user:shareToOrg": "share with the organization (SHARE_WITH_ORG=true)",
    "portal:admin:viewItems": "see every item, not just your own and shared ones",
}


def line(ok, text):
    print(f"  [{'ok' if ok else '!!'}] {text}")


print(f"Python {platform.python_version()} on {platform.system()}")

try:
    import arcgis
    print(f"arcgis {arcgis.__version__}")
except Exception:
    pass

missing_settings = [k for k in ("PORTAL_URL", "PORTAL_USERNAME", "PORTAL_PASSWORD") if not os.getenv(k)]
if missing_settings:
    print("\nSet these in .env first:", ", ".join(missing_settings))
    sys.exit(1)

print("\nLogin")
try:
    gis = GIS(os.environ["PORTAL_URL"], os.environ["PORTAL_USERNAME"], os.environ["PORTAL_PASSWORD"])
except Exception as error:
    line(False, f"could not sign in to {os.environ['PORTAL_URL']}: {str(error)[:160]}")
    sys.exit(1)
me = gis.users.me
line(True, f"signed in to {gis.url} as {me.username} (role {me.role}), portal version {'.'.join(map(str, gis.version))}")

print("\nPrivileges")
have = set(getattr(me, "privileges", []) or [])
for privilege, purpose in PRIVILEGES.items():
    line(privilege in have, f"{privilege}: {purpose}")

print("\nItems")
items = gis.content.search("*", max_items=10000)
owners = sorted({i.owner for i in items})
line(True, f"{len(items)} items visible, {len(owners)} owners: {', '.join(owners[:12])}{' ...' if len(owners) > 12 else ''}")
line(len(items) < 10000, "under the 10,000-item limit of the search" if len(items) < 10000
     else "10,000 items reached: the scan stops there, so some items are missed")
if not (have & {"portal:admin:viewItems"}) and me.role != "org_admin":
    line(False, "this account may not see other people's private items; use an administrator account")

print("\nDashboards versions already in this portal (use the highest one for DASHBOARD_VERSION)")
versions = {}
for dashboard in gis.content.search("type:Dashboard", max_items=50):
    try:
        version = dashboard.get_data().get("version")
    except Exception:
        continue
    versions.setdefault(version, []).append(dashboard.title)
if versions:
    for version, titles in sorted(versions.items(), key=lambda kv: str(kv[0])):
        line(True, f"{version}: {', '.join(titles[:3])}")
else:
    line(False, "no dashboard found: create a blank one in the portal, then run this again to read its version")

print("\nArcGIS Online (optional)")
try:
    requests.get("https://www.arcgis.com/sharing/rest/info", params={"f": "json"}, timeout=10)
    line(True, "reachable: Esri basemaps are told apart from broken references")
except Exception:
    line(False, "not reachable: references to Esri-hosted items such as basemaps will be flagged as broken")

tables_id = os.getenv("DASHBOARD_TABLES_ID", "").strip()
print("\nTables")
if tables_id:
    tables = gis.content.get(tables_id)
    line(tables is not None, f"DASHBOARD_TABLES_ID {tables_id}: "
         + (f"{tables.title}, {len(tables.tables)} tables, shared: {tables.access}" if tables else "not found"))
else:
    line(True, "DASHBOARD_TABLES_ID is empty: the first run of the main script creates the tables")
