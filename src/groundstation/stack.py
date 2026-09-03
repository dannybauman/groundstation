"""The stack layer: parse docs/stack.md, join it with what an artifact renders.

stack.md is curated by humans; this module only reads it. Each component says
when it is on screen (`when`, a small rule over the artifact's render facts),
which pipeline stage it belongs to, and which Development Seed island it sits
in. The join turns generic components into specific instance lines, so a
panel describes what is actually on screen, never a generic diagram.
Attribution is to projects and tools; no person names enter this layer.

Adding a tool is one entry in stack.md with a `when` rule. No code change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# the pipeline, in the order an artifact is made; the panel groups by it
STAGES = ("place", "catalog", "data", "pixels", "draw", "orbit")
STAGE_LABELS = {
    "place": "1 · place",
    "catalog": "2 · catalog",
    "data": "3 · data",
    "pixels": "4 · pixels",
    "draw": "5 · draw",
    "orbit": "6 · next look",
}
KINDS = STAGES  # older name, kept for callers
GROUP_ORDER = STAGES
# ds-role is a closed enum — attribution stays role-shaped, so a person's
# name can never ride in through this field
DS_ROLES = ("created", "maintains", "contributes", "uses")
# the two roles that mean "this is Development Seed's own" — panels mark
# these visibly (quietly: a filled badge, nothing louder) and list them first
DS_OWN = ("created", "maintains")
# Development Seed's islands, the strategic focus areas a component sits in
ISLANDS = {
    "cng": "Cloud Native Geospatial",
    "dib": "Data in the Browser",
    "geoai": "GeoAI",
    "agentic": "Agentic",
}

STACK_MD = Path(__file__).resolve().parents[2] / "docs" / "stack.md"

_CATALOG_COMPONENT = {
    "earth-search": "Earth Search",
    "veda": "NASA VEDA",
    "planetary-computer": "Planetary Computer",
}

# which bucket actually serves each catalog's pixels — the storage entry names
# only these, so a pure earth-search map never mentions Azure
_CATALOG_BUCKET = {
    "earth-search": ("AWS S3", "sentinel-cogs"),
    "veda": ("Azure Blob", "VEDA"),
    "planetary-computer": ("Azure Blob", "Planetary Computer"),
}

REQUIRED_FIELDS = ("what", "ds-role", "integration", "speaks-to", "link", "when")

# the facts a `when` rule may name. A list fact matches on overlap, a scalar on
# equality, a bare name on truthiness. An unknown name fails at parse time so a
# typo in stack.md cannot silently drop a component from every panel
FACTS = (
    "catalogs", "collections", "tiler_hosts", "maplibre", "terrain",
    "geocoded", "events", "weather", "passes", "mosaic_scenes", "snapshot",
)
_ALIASES = {"catalog": "catalogs", "collection": "collections", "host": "tiler_hosts", "mosaic": "mosaic_scenes"}


def _parse_when(text: str, name: str) -> list[list[tuple[bool, str, list[str] | None]]]:
    """`a=x,y & !b | c` -> OR of AND-clauses of (negated, fact, values-or-None)."""
    clauses = []
    for clause in text.split("|"):
        atoms = []
        for atom in clause.split("&"):
            atom = atom.strip()
            if not atom:
                raise ValueError(f"stack.md component {name!r} has an empty clause in when: {text!r}")
            negated = atom.startswith("!")
            atom = atom.lstrip("!").strip()
            key, _, values = atom.partition("=")
            key = _ALIASES.get(key.strip(), key.strip())  # catalog=veda reads better than catalogs=veda
            if key != "always" and key not in FACTS:
                raise ValueError(f"stack.md component {name!r} names an unknown fact {key!r} in when: {text!r}")
            atoms.append((negated, key, [v.strip() for v in values.split(",")] if values else None))
        clauses.append(atoms)
    return clauses


def matches(when: str, facts: dict[str, Any]) -> bool:
    """Evaluate a `when` rule against an artifact's facts. Unknown facts read as false."""
    for clause in _parse_when(when, "<rule>"):
        ok = True
        for negated, key, values in clause:
            if key == "always":
                val = True
            else:
                v = facts.get(key)
                if values is None:
                    val = bool(v)
                elif isinstance(v, list):
                    val = any(x in values for x in v)
                elif v is True and key == "geocoded":
                    val = True  # the older bare True claims every geocoder
                else:
                    val = v in values
            if negated:
                val = not val
            if not val:
                ok = False
                break
        if ok:
            return True
    return False


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
        if comp.get("stage") not in STAGES:
            raise ValueError(f"stack.md component {comp['name']!r} has unknown stage {comp.get('stage')!r}")
        if comp.get("ds-role") not in DS_ROLES:
            raise ValueError(f"stack.md component {comp['name']!r} has invalid ds-role {comp.get('ds-role')!r}")
        if comp.get("island") and comp["island"] not in ISLANDS:
            raise ValueError(f"stack.md component {comp['name']!r} has unknown island {comp['island']!r}")
        missing = [f for f in REQUIRED_FIELDS if not comp.get(f)]
        if missing:
            raise ValueError(f"stack.md component {comp['name']!r} is missing {missing}")
        _parse_when(comp["when"], comp["name"])
        comp["kind"] = comp["stage"]  # older name, kept for callers
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
            "tiler_hosts": [...], "maplibre": bool, "terrain": bool,
            "geocoded": bool | str (geocode's `source`), "events": bool,
            "weather": bool, "passes": bool, "mosaic_scenes": int, "snapshot": bool}
    Returns only the components whose `when` rule holds, in pipeline order with
    Development Seed's own first within a stage, each with an `instance` line —
    specific when facts allow, the generic integration line otherwise. A fact
    that isn't known is treated as false: the panel understates rather than
    fabricates. Even the renderer is a fact — a static postcard runs no map engine.
    """
    facts = dict(facts)
    by_catalog: dict[str, list[str]] = facts.get("collections_by_catalog") or {}
    if "collections" not in facts:
        facts["collections"] = sorted({c for cols in by_catalog.values() for c in cols})
    catalogs = facts.get("catalogs") or []
    collections = ", ".join(facts["collections"])
    hosts = ", ".join(facts.get("tiler_hosts") or [])

    instance_bits = {
        "TiTiler": f"serving {collections or 'this layer'}" + (f" via {hosts}" if hosts else ""),
        "STAC": f"found {collections}" if collections else None,
        "COG + HTTP range requests": "streaming only the bytes each tile needs",
    }
    pg = [c for c in catalogs if c in ("veda", "planetary-computer")]
    if pg:
        where = " and ".join("NASA VEDA" if c == "veda" else "Planetary Computer" for c in pg)
        instance_bits["titiler-pgstac"] = f"the tiler behind {where}, serving {collections or 'this layer'}"
        instance_bits["stac-fastapi"] = f"the STAC API {where} answers searches with"
        instance_bits["pgstac"] = f"the database {where} keeps its catalog in"
    if facts.get("mosaic_scenes"):
        n = facts["mosaic_scenes"]
        instance_bits["rio-tiler"] = f"mosaicked {n} scenes into one frame, first valid pixel wins"
    buckets: dict[str, list[str]] = {}
    for c in catalogs:
        if c in _CATALOG_BUCKET:
            provider, detail = _CATALOG_BUCKET[c]
            buckets.setdefault(provider, []).append(detail)
    if facts.get("terrain"):
        buckets.setdefault("AWS S3", []).append("terrain tiles")
    if buckets:
        instance_bits["Cloud object storage"] = "streaming from " + " + ".join(
            f"{provider} ({', '.join(details)})" for provider, details in buckets.items()
        )
    for c in catalogs:
        cols = ", ".join(by_catalog.get(c) or [])
        if c in _CATALOG_COMPONENT and cols:
            instance_bits[_CATALOG_COMPONENT[c]] = f"source of {cols}"

    entries = []
    for comp in components:
        if not matches(comp["when"], facts):
            continue
        entries.append({**comp, "instance": instance_bits.get(comp["name"]) or comp.get("integration", "")})
    entries.sort(key=lambda e: (STAGES.index(e["stage"]), e["ds-role"] not in DS_OWN, e["name"].lower()))
    return entries


def islands_exercised(entries: list[dict[str, Any]]) -> list[str]:
    """Island names present in a panel, in island order."""
    seen = {e.get("island") for e in entries if e.get("island")}
    return [label for key, label in ISLANDS.items() if key in seen]


def summary(entries: list[dict[str, Any]]) -> str:
    """One line for the top of a panel: how much of what is on screen is ours, and where it sits."""
    ds = sum(1 for e in entries if e["ds-role"] in DS_OWN)
    line = f"{len(entries)} components on screen · {ds} built by Development Seed"
    islands = islands_exercised(entries)
    if islands:
        line += " · islands: " + ", ".join(islands)
    return line
