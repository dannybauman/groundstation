"""groundstation MCP server: Earth data, agent-ready.

Run locally over stdio:
    uv run groundstation
Register with Claude Code:
    claude mcp add groundstation -- uv --directory /path/to/groundstation run groundstation
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from groundstation import tools

mcp = FastMCP(
    "groundstation",
    instructions=(
        "Tools for discovering, analyzing, and mapping Earth observation data "
        "via the cloud-native geospatial stack (STAC, TiTiler, Gazet). Typical "
        "flow: geocode a place -> search_datasets/describe_collection to pick "
        "data -> search_imagery for items -> preview_item / compute_statistics "
        "to analyze -> render_map to hand the user a shareable interactive map. "
        "active_events and weather_summary support monitoring and briefings."
    ),
)

for fn in (
    tools.geocode,
    tools.list_catalogs,
    tools.search_datasets,
    tools.describe_collection,
    tools.search_imagery,
    tools.preview_item,
    tools.compute_statistics,
    tools.tile_url_template,
    tools.render_map,
    tools.active_events,
    tools.weather_summary,
):
    mcp.tool()(fn)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
