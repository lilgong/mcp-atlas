"""Run osm-mcp-server with ordered, schema-preserving Overpass fallbacks."""

from __future__ import annotations

import asyncio
import inspect
import math
import os
from datetime import datetime
from importlib.metadata import version
from typing import Any

import aiohttp
from osm_mcp_server import server


EXPECTED_VERSION = "0.1.1"
UPSTREAM_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
SECONDARY_OVERPASS_URL = "https://overpass.kumi.systems/api/interpreter"
FALLBACK_OVERPASS_URL = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
RETRYABLE_STATUSES = frozenset({406, 429, 500, 502, 503, 504})
OVERPASS_ATTEMPT_TIMEOUT_SECONDS = 45
OVERPASS_HEADERS = {
    "User-Agent": "mcp-atlas/1.0 (MCP evaluation; Overpass read-only client)",
    "Accept": "application/json",
}

NEIGHBORHOOD_CATEGORIES = {
    "groceries": ("shop=supermarket", "shop=convenience", "shop=grocery"),
    "restaurants": ("amenity=restaurant", "amenity=cafe", "amenity=fast_food"),
    "healthcare": ("amenity=hospital", "amenity=doctors", "amenity=pharmacy"),
    "education": ("amenity=school", "amenity=kindergarten", "amenity=university"),
    "public_transport": ("public_transport=stop_position", "railway=station", "amenity=bus_station"),
    "parks": ("leisure=park", "leisure=garden", "leisure=playground"),
    "sports": ("leisure=sports_centre", "leisure=fitness_centre", "leisure=swimming_pool"),
    "entertainment": ("amenity=theatre", "amenity=cinema", "amenity=arts_centre"),
    "shopping": ("shop=mall", "shop=department_store", "shop=clothes"),
    "services": ("amenity=bank", "amenity=post_office", "amenity=atm"),
}


class _FallbackRequestContext:
    def __init__(self, session, request, urls, args, kwargs):
        self.session = session
        self.request = request
        self.urls = urls
        self.args = args
        self.kwargs = kwargs
        self.active = None

    async def __aenter__(self):
        last_error = None
        for index, url in enumerate(self.urls):
            kwargs = dict(self.kwargs)
            kwargs["headers"] = {
                **OVERPASS_HEADERS,
                **dict(kwargs.get("headers") or {}),
            }
            kwargs.setdefault(
                "timeout",
                aiohttp.ClientTimeout(total=OVERPASS_ATTEMPT_TIMEOUT_SECONDS),
            )
            context = self.request(self.session, url, *self.args, **kwargs)
            try:
                response = await context.__aenter__()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                continue
            if (
                response.status in RETRYABLE_STATUSES
                and index + 1 < len(self.urls)
            ):
                await context.__aexit__(None, None, None)
                continue
            self.active = context
            return response
        if last_error is not None:
            raise last_error
        raise RuntimeError("no Overpass endpoint was attempted")

    async def __aexit__(self, exc_type, exc, traceback):
        if self.active is None:
            return False
        return await self.active.__aexit__(exc_type, exc, traceback)


def install_overpass_redirect() -> None:
    installed = version("osm-mcp-server")
    if installed != EXPECTED_VERSION:
        raise RuntimeError(
            "OSM compatibility wrapper expected osm-mcp-server=="
            f"{EXPECTED_VERSION}, found {installed}"
        )
    if UPSTREAM_OVERPASS_URL not in inspect.getsource(server):
        raise RuntimeError(
            "OSM compatibility wrapper no longer matches the pinned upstream "
            "implementation; inspect osm_mcp_server.server before upgrading"
        )
    configured = (
        os.getenv("SYN_OSM_OVERPASS_URLS")
        or os.getenv("SYN_OSM_OVERPASS_URL")
        or (
            f"{UPSTREAM_OVERPASS_URL},{SECONDARY_OVERPASS_URL},"
            f"{FALLBACK_OVERPASS_URL}"
        )
    )
    targets = tuple(url.strip() for url in configured.split(",") if url.strip())
    if not targets or any(
        not target.startswith(("https://", "http://")) for target in targets
    ):
        raise RuntimeError(
            "SYN_OSM_OVERPASS_URLS must contain comma-separated HTTP(S) URLs"
        )

    original_get = aiohttp.ClientSession.get
    original_post = aiohttp.ClientSession.post

    def redirect(url: str) -> str:
        return targets[0] if url == UPSTREAM_OVERPASS_URL else url

    def redirected_get(self: aiohttp.ClientSession, url: str, *args: Any, **kwargs: Any):
        return original_get(self, redirect(url), *args, **kwargs)

    def redirected_post(self: aiohttp.ClientSession, url: str, *args: Any, **kwargs: Any):
        if url != UPSTREAM_OVERPASS_URL:
            return original_post(self, url, *args, **kwargs)
        return _FallbackRequestContext(
            self, original_post, targets, args, kwargs,
        )

    aiohttp.ClientSession.get = redirected_get
    aiohttp.ClientSession.post = redirected_post


async def optimized_analyze_neighborhood(latitude, longitude, ctx, radius=1000):
    """Preserve upstream scoring while collapsing ten serial queries into one."""
    client = ctx.request_context.lifespan_context.osm_client
    lat_delta = radius / 111000
    lon_delta = radius / (111000 * math.cos(math.radians(latitude)))
    bbox = (longitude - lon_delta, latitude - lat_delta, longitude + lon_delta, latitude + lat_delta)
    bbox_text = f"{bbox[1]},{bbox[0]},{bbox[3]},{bbox[2]}"
    filters = []
    for tags in NEIGHBORHOOD_CATEGORIES.values():
        for tag in tags:
            key, value = tag.split("=", 1)
            filters.extend((f'node["{key}"="{value}"]({bbox_text});', f'way["{key}"="{value}"]({bbox_text});'))
    query = "[out:json];(" + "".join(filters) + ");out body;"
    await ctx.report_progress(0, len(NEIGHBORHOOD_CATEGORIES))
    address_task = asyncio.create_task(client.reverse_geocode(latitude, longitude))
    try:
        async with client.session.post(UPSTREAM_OVERPASS_URL, data={"data": query}) as response:
            if response.status != 200:
                raise RuntimeError(f"Failed to analyze neighborhood: {response.status}")
            elements = (await response.json()).get("elements", [])
        address_info = await address_task
    except BaseException:
        if not address_task.done():
            address_task.cancel()
        raise
    grouped = {name: [] for name in NEIGHBORHOOD_CATEGORIES}
    for feature in elements:
        tags = feature.get("tags") or {}
        if feature.get("type") == "node":
            lat, lon = feature.get("lat"), feature.get("lon")
        else:
            center = feature.get("center") or {}
            lat, lon = center.get("lat"), center.get("lon")
        if lat is None or lon is None:
            continue
        distance = _haversine(latitude, longitude, lat, lon)
        item = {"id": feature.get("id"), "name": tags.get("name", "Unnamed"), "type": feature.get("type"), "coordinates": {"latitude": lat, "longitude": lon}, "distance": round(distance, 1), "tags": tags}
        for name, selectors in NEIGHBORHOOD_CATEGORIES.items():
            if any(tags.get(key) == value for key, value in (selector.split("=", 1) for selector in selectors)):
                grouped[name].append((item, distance))
    results, scores = {}, {}
    for index, (name, entries) in enumerate(grouped.items(), 1):
        entries.sort(key=lambda entry: entry[1])
        features = [entry[0] for entry in entries]
        distances = [entry[1] for entry in entries]
        count = len(entries)
        avg_distance = sum(distances) / count if count else None
        min_distance = min(distances) if count else None
        score = 0.0 if not count else min(count / 5, 1) * 5 + 5 - min(min_distance / radius, 1) * 5
        results[name] = {"count": count, "features": features[:10], "metrics": {"total_count": count, "avg_distance": round(avg_distance, 1) if avg_distance else None, "min_distance": round(min_distance, 1) if min_distance else None}}
        scores[name] = score
        await ctx.report_progress(index, len(grouped))
    walkable_amenities = walkable_categories = 0
    for category in results.values():
        walking_count = sum(item["distance"] <= 500 for item in category["features"])
        if walking_count:
            walkable_amenities += walking_count
            walkable_categories += 1
    overall = sum(scores.values()) / len(scores) if scores else 0
    return {"location": {"coordinates": {"latitude": latitude, "longitude": longitude}, "address": address_info.get("display_name", "Unknown location")}, "scores": {"overall": round(overall, 1), "walkability": min(walkable_amenities + walkable_categories, 10), "categories": {name: round(score, 1) for name, score in scores.items()}}, "categories": results, "analysis_radius": radius, "timestamp": datetime.now().isoformat()}


def _haversine(lat1, lon1, lat2, lon2):
    earth_radius = 6371000
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    value = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return earth_radius * 2 * math.asin(math.sqrt(value))


def install_neighborhood_optimization() -> None:
    tool = server.mcp._tool_manager._tools.get("analyze_neighborhood")
    if tool is None or tool.fn is not server.analyze_neighborhood:
        raise RuntimeError("OSM analyze_neighborhood registration changed; inspect the pinned server")
    tool.fn = optimized_analyze_neighborhood


def main() -> None:
    install_overpass_redirect()
    install_neighborhood_optimization()
    server.mcp.run()


if __name__ == "__main__":
    main()
