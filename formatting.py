"""生成最终的回复文案。

自动回复只输出一句话（模板 + 详细地址），不附加坐标、设备、地图链接等额外内容。
地图链接、附近地点等辅助函数仅供 /exif 指令使用。这里不依赖 AstrBot，方便单独测试。
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Mapping

from .geocoder import (
    GeoResult,
    compose_region,
    normalize_country,
    wgs84_to_gcj02,
)

DEFAULT_TEMPLATE = "请问是{region}的{sender}群友吗？🤔"

_MAP_LABELS = {
    "amap": "🗺️ 高德地图",
    "baidu": "🌍 百度地图",
    "osm": "🌐 OpenStreetMap",
}


def build_reply(
    config: Mapping[str, Any],
    info,
    geo: GeoResult | None,
    *,
    sender: str = "",
    group: str = "",
) -> str:
    """生成自动回复；只包含模板这一句话，没有可解析地点时返回空字符串。"""
    gps = info.gps
    if gps is None:
        return ""

    region = detailed_region(geo)
    if not region:
        if not _flag(config, "report_unresolved", False):
            return ""
        return (
            "📷 这张图片的 EXIF 中记录了拍摄坐标："
            f"{gps.latitude:.6f}, {gps.longitude:.6f}（WGS-84），"
            "但暂时没能解析出具体地点。"
        )

    template = str(config.get("reply_template", "") or "").strip()
    if not template:
        template = DEFAULT_TEMPLATE

    fields = {
        "region": region,
        "country": normalize_country(geo.country, geo.country_code)
        if geo is not None
        else "",
        "province": (geo.province if geo is not None else ""),
        "city": (geo.city if geo is not None else ""),
        "district": (geo.district if geo is not None else ""),
        "township": (geo.township if geo is not None else ""),
        "poi": (geo.poi if geo is not None else ""),
        "address": (geo.address if geo is not None else ""),
        "lat": f"{gps.latitude:.6f}",
        "lng": f"{gps.longitude:.6f}",
        "sender": sender,
        "group": group,
    }
    return _safe_format(template, fields)


def detailed_region(geo: GeoResult | None) -> str:
    """完整详细地址：国家+省+市+区+街道+地点，例如
    “中国浙江省宁波市鄞州区首南街道学府路地铁站”。"""
    region = compose_region(geo, "full")
    if not region and geo is not None:
        region = (geo.address or "").strip()
    return region


def format_nearby_line(geo: GeoResult | None) -> str | None:
    """生成“📍 附近地点：xxx（约 58 米）”一行；没有可用信息时返回 None。"""
    if geo is None or not geo.poi:
        return None
    label = "附近道路" if geo.poi_kind == "road" else "附近地点"
    distance_text = ""
    if geo.poi_distance_m is not None:
        distance_text = f"（约 {_format_distance(geo.poi_distance_m)}）"
    return f"📍 {label}：{geo.poi}{distance_text}"


def build_map_links(
    config: Mapping[str, Any],
    gps,
    geo: GeoResult | None,
    region: str,
) -> list[str]:
    """生成地图链接列表（供 /exif 使用），如 ["🗺️ 高德地图：https://..."]。"""
    services = config.get("map_links")
    if isinstance(services, str):
        services = [services]
    if not isinstance(services, list) or not services:
        services = ["amap"]

    name = (geo.poi if geo is not None else "") or region or "拍摄位置"
    links: list[str] = []
    for service in services:
        service = str(service).strip().lower()
        if service == "amap":
            url = _amap_link(gps, name)
        elif service == "baidu":
            url = _baidu_link(gps, name)
        elif service == "osm":
            url = _osm_link(gps)
        else:
            continue
        links.append(f"{_MAP_LABELS[service]}：{url}")
    return links


def _amap_link(gps, name: str) -> str:
    gcj_lng, gcj_lat = wgs84_to_gcj02(gps.longitude, gps.latitude)
    return (
        "https://uri.amap.com/marker?position="
        f"{gcj_lng:.6f},{gcj_lat:.6f}"
        f"&name={urllib.parse.quote(name, safe='')}"
        "&coordinate=gaode&callnative=1"
    )


def _baidu_link(gps, name: str) -> str:
    return (
        "https://api.map.baidu.com/marker?"
        f"location={gps.latitude:.6f},{gps.longitude:.6f}"
        f"&title={urllib.parse.quote(name, safe='')}"
        f"&content={urllib.parse.quote('图片 EXIF 拍摄位置', safe='')}"
        "&output=html&coord_type=wgs84&src=astrbot_plugin_image_exif"
    )


def _osm_link(gps) -> str:
    return (
        "https://www.openstreetmap.org/"
        f"?mlat={gps.latitude:.6f}&mlon={gps.longitude:.6f}"
        f"#map=18/{gps.latitude:.6f}/{gps.longitude:.6f}"
    )


def _format_distance(distance_m: float) -> str:
    if distance_m < 1000:
        return f"{distance_m:.0f} 米"
    return f"{distance_m / 1000:.1f} 公里"


def _flag(config: Mapping[str, Any], key: str, default: bool) -> bool:
    value = config.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _safe_format(template: str, values: Mapping[str, Any]) -> str:
    try:
        return template.format_map(_SafeDict(values))
    except (IndexError, KeyError, ValueError):
        return template
