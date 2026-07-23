"""The stack layer: parse docs/stack.md, join it with what an artifact renders.

stack.md is curated by humans; this module only reads it. The join takes the
artifact's real parameters (catalog, collection, tiler host, terrain) and
turns generic components into specific instance lines — the panel describes
what is actually on screen, never a generic diagram. Attribution is to
projects and tools; no person names enter this layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

KINDS = ("data", "access", "tiling", "viz", "standard", "infra")
# ds-role is a closed enum — attribution stays role-shaped, so a person's
# name can never ride in through this field
DS_ROLES = ("created", "maintains", "contributes", "uses")
# pipeline order the panel groups by; standard/infra trail as context
GROUP_ORDER = ("data", "access", "tiling", "viz", "standard", "infra")

STACK_MD = Path(__file__).resolve().parents[2] / "docs" / "stack.md"

_CATALOG_COMPONENT = {
    "earth-search": "Earth Search",
    "veda": "NASA VEDA",
    "planetary-computer": "Planetary Computer",
}


REQUIRED_FIELDS = ("what", "ds-role", "integration", "speaks-to", "link")


def parse_stack(path: str | Path = STACK_MD) -> list[dict[str, str]]:
    """Parse stack.md into component dicts. Any curation mistake fails loudly."""
    # leading newline so a file that opens directly with "## " still splits
    text = "\n" + Path(path).read_text(encoding="utf-8")
    components = []
    for block in text.split("\n## ")[1:]:
        lines = block.splitlines()  # no strip first — a blank heading line must stay visible
        if not lines or not lines[0].strip():
            raise ValueError("stack.md has an empty '## ' heading")
        comp: dict[str, str] = {"name": lines[0].strip()}
        for line in lines[1:]:
            if line.startswith("- ") and ": " in line:
                key, _, value = line[2:].partition(": ")
                comp[key.strip()] = value.strip()
        if comp.get("kind") not in KINDS:
            raise ValueError(f"stack.md component {comp['name']!r} has unknown kind {comp.get('kind')!r}")
        if comp.get("ds-role") not in DS_ROLES:
            raise ValueError(f"stack.md component {comp['name']!r} has invalid ds-role {comp.get('ds-role')!r}")
        missing = [f for f in REQUIRED_FIELDS if not comp.get(f)]
        if missing:
            raise ValueError(f"stack.md component {comp['name']!r} is missing {missing}")
        components.append(comp)
    names = [c["name"] for c in components]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"stack.md has duplicate components: {sorted(dupes)}")
    return components


def stack_instances(
    components: list[dict[str, str]], facts: dict[str, Any]
) -> list[dict[str, Any]]:
    """Join components with an artifact's real render facts.

    facts: {"catalogs": [...], "collections_by_catalog": {catalog: [...]},
            "tiler_hosts": [...], "terrain": bool, "geocoded": bool, "events": bool}
    Returns only the components this artifact actually exercised, in
    GROUP_ORDER, each with an `instance` line — specific when facts allow,
    the generic integration line otherwise. A fact that isn't known is
    treated as false: the panel understates rather than fabricates.
    """
    catalogs = facts.get("catalogs") or []
    by_catalog: dict[str, list[str]] = facts.get("collections_by_catalog") or {}
    collections = ", ".join(sorted({c for cols in by_catalog.values() for c in cols}))
    hosts = ", ".join(facts.get("tiler_hosts") or [])

    active_names = {"MapLibre GL"}  # the renderer is always on screen
    if catalogs:  # raster pixels on screen -> the whole raster pipeline is live
        active_names |= {_CATALOG_COMPONENT[c] for c in catalogs if c in _CATALOG_COMPONENT}
        active_names |= {"STAC", "COG + HTTP range requests", "TiTiler", "Cloud object storage"}
    if facts.get("terrain"):
        active_names.add("AWS Terrarium terrain")
    if facts.get("geocoded"):
        active_names |= {"Gazet", "Nominatim"}
    if facts.get("events"):
        active_names |= {"NASA EONET", "GDACS", "Open-Meteo"}

    instance_bits = {
        "TiTiler": f"serving {collections or 'this layer'}" + (f" via {hosts}" if hosts else ""),
        "STAC": f"found {collections}" if collections else None,
        "COG + HTTP range requests": "streaming only the bytes each tile needs",
    }
    for c in catalogs:
        cols = ", ".join(by_catalog.get(c) or [])
        if c in _CATALOG_COMPONENT and cols:
            instance_bits[_CATALOG_COMPONENT[c]] = f"source of {cols}"

    entries = []
    for comp in components:
        if comp["name"] not in active_names:
            continue
        entries.append(
            {
                **comp,
                "instance": instance_bits.get(comp["name"]) or comp.get("integration", ""),
            }
        )
    entries.sort(key=lambda e: GROUP_ORDER.index(e["kind"]))
    return entries
