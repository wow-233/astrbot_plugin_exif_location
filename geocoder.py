"""逆地理编码与附近地点识别。

地址解析支持三种公开服务：
- BigDataCloud：免 Key，国内可访问，默认使用；
- 高德地图 Web 服务：需要 Key，国内 POI 精度最好；
- OpenStreetMap Nominatim：免 Key，海外地址更丰富。

附近地点使用 Photon（OpenStreetMap 的免费检索服务，免 Key、国内可直连），
返回拍摄点最近的地标 / 道路并计算距离。

注意：EXIF 中的 GPS 坐标通常为 WGS-84。高德、百度使用各自的偏移坐标系，
请求前会调用对应的坐标转换。
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

_BDC_URL = "https://api.bigdatacloud.net/data/reverse-geocode-client"
_AMAP_URL = "https://restapi.amap.com/v3/geocode/regeo"
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_PHOTON_URL = "https://photon.komoot.io/reverse"

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_ADMIN_SUFFIX_RE = re.compile(
    r"(特别行政区|自治区|自治州|自治县|省|市|区|县|旗|镇|乡|街道|苏木)$"
)

# Photon 返回的这些类别是纯行政/区域名，不适合作为“附近地点”。
_PHOTON_IGNORED_KEYS = {"place", "boundary"}
# highway 里的这些值仍然是值得报告的地点（车站等），而不是普通道路。
_PHOTON_ROADlike_VALUES = {"bus_stop", "bus_station", "platform"}

_POI_BREAKER_THRESHOLD = 3
_POI_BREAKER_COOLDOWN = 600.0


class GeocodeError(Exception):
    """逆地理编码失败。"""


def sanitize_error(exc: BaseException) -> str:
    """生成可安全写入日志的错误摘要。

    httpx 等库的异常文本会包含完整请求 URL，其中可能带有 GPS 坐标和
    高德 API Key，因此日志里只保留错误类别或已脱敏的描述。
    """
    if isinstance(exc, GeocodeError):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "请求超时"
    if isinstance(exc, httpx.HTTPError):
        return type(exc).__name__
    return type(exc).__name__


@dataclass
class GeoResult:
    """解析后的地址。字段为空字符串表示该级地址不存在。"""

    provider: str
    country: str = ""
    country_code: str = ""
    province: str = ""
    city: str = ""
    district: str = ""
    township: str = ""
    poi: str = ""
    poi_kind: str = ""  # "poi"=地标，"road"=道路
    poi_distance_m: float | None = None
    address: str = ""


class ReverseGeocoder:
    """带缓存、限速和熔断的逆地理编码器。

    logger 必须由调用方传入 AstrBot 提供的日志记录器
    （``astrbot.api.logger`` 或插件专属 logger），本模块不使用
    Python 内置 logging 模块。
    """

    def __init__(self, config: dict[str, Any], logger) -> None:
        if logger is None:
            raise ValueError("ReverseGeocoder 需要传入 AstrBot 的 logger")
        self.config = config or {}
        self.logger = logger
        try:
            timeout = float(self.config.get("timeout", 10) or 10)
        except (TypeError, ValueError):
            timeout = 10.0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(max(3.0, timeout)),
            headers={
                "User-Agent": "AstrBot-ImageExifPlugin/1.2 (AstrBot plugin)",
                "Accept": "application/json",
            },
        )
        self._cache: dict[str, tuple[float, GeoResult]] = {}
        self._throttle_lock = asyncio.Lock()
        self._last_request_at = 0.0

        self._poi_enabled = bool(self.config.get("enable_poi_lookup", True))
        try:
            self._poi_radius = max(50.0, float(self.config.get("poi_radius", 300) or 300))
        except (TypeError, ValueError):
            self._poi_radius = 300.0
        self._poi_fail_count = 0
        self._poi_disabled_until = 0.0
        self._warned_no_amap_key = False

    async def close(self) -> None:
        await self._client.aclose()

    async def lookup(self, latitude: float, longitude: float) -> GeoResult | None:
        """把坐标解析成地址并补充附近地点；全部失败时返回 None。"""
        if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
            return None

        try:
            ttl = float(self.config.get("cache_ttl", 86400) or 0)
        except (TypeError, ValueError):
            ttl = 86400.0
        chain = self._provider_chain(longitude, latitude)
        key = f"{'+'.join(chain)}:{latitude:.5f},{longitude:.5f}"
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and ttl > 0 and now - cached[0] <= ttl:
            return cached[1]

        result = None
        for provider in chain:
            try:
                if provider == "amap":
                    result = await self._lookup_amap(latitude, longitude)
                elif provider == "nominatim":
                    result = await self._lookup_nominatim(latitude, longitude)
                else:
                    result = await self._lookup_bigdatacloud(latitude, longitude)
            except Exception as exc:
                self.logger.warning(
                    "逆地理编码服务 %s 请求失败: %s", provider, sanitize_error(exc)
                )
                continue
            if result is not None:
                break

        # 用 Photon 补充“附近地点”，地址完全失败时也能给出一个大致位置。
        if self._poi_enabled:
            nearby = await self._lookup_nearby(latitude, longitude)
            if nearby is not None:
                name, kind, distance = nearby
                if result is None:
                    result = GeoResult(
                        provider="photon",
                        poi=name,
                        poi_kind=kind,
                        poi_distance_m=distance,
                        address=name,
                    )
                elif not result.poi:
                    result.poi = name
                    result.poi_kind = kind
                    result.poi_distance_m = distance

        if result is not None and ttl > 0:
            self._cache[key] = (now, result)
            self._trim_cache()
        return result

    def _provider_chain(self, longitude: float, latitude: float) -> list[str]:
        """根据配置和坐标位置决定尝试顺序。高德对海外坐标无效，海外优先 BDC。"""
        configured = str(self.config.get("provider", "auto") or "auto").strip().lower()
        has_key = bool(str(self.config.get("amap_api_key", "") or "").strip())
        in_china = is_in_china(longitude, latitude)
        fallback = ["bigdatacloud", "nominatim"]

        if configured == "auto":
            if in_china and has_key:
                return ["amap"] + fallback
            return list(fallback)
        if configured == "amap":
            if not has_key:
                if not self._warned_no_amap_key:
                    self._warned_no_amap_key = True
                    self.logger.warning("未配置高德 API Key，已回退到 BigDataCloud")
                return list(fallback)
            # 高德对海外坐标返回空结果，自动落到 BigDataCloud。
            return ["amap"] + fallback
        if configured == "nominatim":
            return ["nominatim"]
        if configured == "bigdatacloud":
            return ["bigdatacloud"]
        self.logger.warning("未知的逆地理编码服务 %s，使用 BigDataCloud", configured)
        return ["bigdatacloud"]

    async def _lookup_bigdatacloud(
        self, latitude: float, longitude: float
    ) -> GeoResult | None:
        await self._throttle(0.6)
        response = await self._client.get(
            _BDC_URL,
            params={
                "latitude": f"{latitude:.6f}",
                "longitude": f"{longitude:.6f}",
                "localityLanguage": "zh-Hans",
            },
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise GeocodeError("BigDataCloud 返回了无法解析的数据")

        country = _as_text(data.get("countryName"))
        country_code = _as_text(data.get("countryCode")).upper()
        province = _as_text(data.get("principalSubdivision"))
        city = _as_text(data.get("city"))
        district = _as_text(data.get("locality"))
        township = ""

        locality_info = data.get("localityInfo")
        admin_items: list[tuple[int, str]] = []
        if isinstance(locality_info, dict):
            for item in locality_info.get("administrative") or []:
                if not isinstance(item, dict):
                    continue
                name = _as_text(item.get("name"))
                level = item.get("adminLevel")
                if name and isinstance(level, int):
                    admin_items.append((level, name))
            admin_items.sort(key=lambda entry: entry[0])
        for level, name in admin_items:
            if level > 6 and name not in {district, city, province, country}:
                township = name

        if not any((country, province, city, district, township)):
            return None

        return GeoResult(
            provider="bigdatacloud",
            country=country,
            country_code=country_code,
            province=province,
            city=city,
            district=district,
            township=township,
            address=_join_address(
                normalize_country(country, country_code),
                province,
                city,
                district,
                township,
            ),
        )

    async def _lookup_amap(
        self, latitude: float, longitude: float
    ) -> GeoResult | None:
        key = str(self.config.get("amap_api_key", "") or "").strip()
        if not key:
            raise GeocodeError("未配置高德 API Key")

        await self._throttle(0.35)
        gcj_lng, gcj_lat = wgs84_to_gcj02(longitude, latitude)
        response = await self._client.get(
            _AMAP_URL,
            params={
                "key": key,
                "location": f"{gcj_lng:.6f},{gcj_lat:.6f}",
                "extensions": "all",
                "output": "JSON",
            },
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise GeocodeError("高德接口返回了无法解析的数据")
        if str(data.get("status")) != "1":
            raise GeocodeError(
                f"高德接口返回错误：{data.get('info')}（{data.get('infocode')}）"
            )

        regeocode = data.get("regeocode")
        if not isinstance(regeocode, dict):
            return None
        component = regeocode.get("addressComponent")
        if not isinstance(component, dict):
            component = {}

        province = _as_text(component.get("province"))
        city = _as_text(component.get("city"))
        district = _as_text(component.get("district"))
        township = _as_text(component.get("township"))
        address = _as_text(regeocode.get("formatted_address"))

        poi, poi_distance, poi_kind = self._pick_amap_poi(regeocode, component)

        if not any((province, city, district, township, address, poi)):
            return None

        return GeoResult(
            provider="amap",
            country="中国",
            country_code="CN",
            province=province,
            city=city,
            district=district,
            township=township,
            poi=poi,
            poi_kind=poi_kind,
            poi_distance_m=poi_distance,
            address=address,
        )

    @staticmethod
    def _pick_amap_poi(
        regeocode: dict[str, Any], component: dict[str, Any]
    ) -> tuple[str, float | None, str]:
        """从高德逆地理结果中挑选最合适的地点：楼栋 > 附近 POI（按距离）> 街区。"""
        candidates: list[tuple[int, float, str]] = []

        building = component.get("building")
        if isinstance(building, dict):
            name = _as_text(building.get("name"))
            if name:
                candidates.append((0, 0.0, name))

        pois = regeocode.get("pois")
        if isinstance(pois, list):
            for item in pois:
                if not isinstance(item, dict):
                    continue
                name = _as_text(item.get("name"))
                if not name:
                    continue
                try:
                    distance = float(item.get("distance"))
                except (TypeError, ValueError):
                    distance = 1e9
                candidates.append((1, distance, name))

        neighborhood = component.get("neighborhood")
        if isinstance(neighborhood, dict):
            name = _as_text(neighborhood.get("name"))
            if name:
                candidates.append((2, 1e9, name))

        if not candidates:
            return "", None, ""
        _, distance, name = min(candidates, key=lambda item: (item[0], item[1]))
        shown_distance = distance if 5.0 <= distance < 1e8 else None
        return name, shown_distance, "poi"

    async def _lookup_nominatim(
        self, latitude: float, longitude: float
    ) -> GeoResult | None:
        await self._throttle(1.1)
        params: dict[str, Any] = {
            "format": "jsonv2",
            "lat": f"{latitude:.6f}",
            "lon": f"{longitude:.6f}",
            "zoom": 18,
            "addressdetails": 1,
            "accept-language": "zh-CN",
        }
        email = str(self.config.get("contact_email", "") or "").strip()
        if email:
            params["email"] = email
        response = await self._client.get(
            _NOMINATIM_URL,
            params=params,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get("error"):
            raise GeocodeError(str((data or {}).get("error") or "Nominatim 无结果"))

        address = data.get("address")
        if not isinstance(address, dict):
            address = {}

        country = _as_text(address.get("country"))
        country_code = _as_text(address.get("country_code")).upper()
        province = _as_text(address.get("state") or address.get("region"))
        city = _as_text(
            address.get("city")
            or address.get("town")
            or address.get("municipality")
        )
        district = _as_text(
            address.get("district")
            or address.get("county")
            or address.get("city_district")
            or address.get("suburb")
        )
        township = _as_text(
            address.get("suburb")
            or address.get("town")
            or address.get("village")
            or address.get("neighbourhood")
        )
        poi = _as_text(
            address.get("amenity")
            or address.get("tourism")
            or address.get("leisure")
            or address.get("building")
            or address.get("shop")
            or address.get("office")
        )
        if not poi:
            name = _as_text(data.get("name"))
            road = _as_text(address.get("road"))
            if name and name not in {city, province, district, township} and name != road:
                poi = name
        if not any((country, province, city, district, township, poi)):
            return None

        return GeoResult(
            provider="nominatim",
            country=country,
            country_code=country_code,
            province=province,
            city=city,
            district=district,
            township=township,
            poi=poi,
            address=_as_text(data.get("display_name")),
        )

    async def _lookup_nearby(
        self, latitude: float, longitude: float
    ) -> tuple[str, str, float | None] | None:
        """用 Photon 查询最近的地点 / 道路，失败时静默降级（带熔断）。"""
        if time.monotonic() < self._poi_disabled_until:
            return None
        try:
            return await self._lookup_photon(latitude, longitude)
        except Exception as exc:
            self._poi_fail_count += 1
            if self._poi_fail_count >= _POI_BREAKER_THRESHOLD:
                self._poi_fail_count = 0
                self._poi_disabled_until = time.monotonic() + _POI_BREAKER_COOLDOWN
                self.logger.warning(
                    "附近地点服务连续失败，已暂停 10 分钟: %s",
                    sanitize_error(exc),
                )
            else:
                self.logger.debug("附近地点查询失败: %s", sanitize_error(exc))
            return None

    async def _lookup_photon(
        self, latitude: float, longitude: float
    ) -> tuple[str, str, float | None] | None:
        await self._throttle(0.5)
        response = await self._client.get(
            _PHOTON_URL,
            params={
                "lat": f"{latitude:.6f}",
                "lon": f"{longitude:.6f}",
                "limit": 5,
            },
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise GeocodeError("Photon 返回了无法解析的数据")

        best_landmark: tuple[str, float] | None = None
        best_road: tuple[str, float] | None = None
        for feature in data.get("features") or []:
            props = feature.get("properties")
            if not isinstance(props, dict):
                continue
            name = _as_text(props.get("name"))
            if not name:
                continue
            osm_key = _as_text(props.get("osm_key")).lower()
            osm_value = _as_text(props.get("osm_value")).lower()
            if osm_key in _PHOTON_IGNORED_KEYS:
                continue

            distance: float | None = None
            geometry = feature.get("geometry")
            if isinstance(geometry, dict):
                coords = geometry.get("coordinates")
                if isinstance(coords, (list, tuple)) and len(coords) == 2:
                    try:
                        distance = haversine_m(
                            longitude, latitude, float(coords[0]), float(coords[1])
                        )
                    except (TypeError, ValueError):
                        distance = None
            if distance is not None and distance > self._poi_radius:
                continue

            is_road = osm_key == "highway" and osm_value not in _PHOTON_ROADlike_VALUES
            if is_road:
                if best_road is None or (distance is not None and distance < best_road[1]):
                    best_road = (name, distance if distance is not None else 1e9)
            else:
                if best_landmark is None or (
                    distance is not None and distance < best_landmark[1]
                ):
                    best_landmark = (name, distance if distance is not None else 1e9)

        chosen = best_landmark or best_road
        if chosen is None:
            return None
        name, distance = chosen
        kind = "road" if best_landmark is None else "poi"
        shown = distance if distance < 1e8 else None
        return name, kind, shown

    async def _throttle(self, min_interval: float) -> None:
        """串行化请求，避免触发公共服务限速。"""
        async with self._throttle_lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < min_interval:
                await asyncio.sleep(min_interval - elapsed)
            self._last_request_at = time.monotonic()

    def _trim_cache(self) -> None:
        if len(self._cache) <= 512:
            return
        ordered = sorted(self._cache.items(), key=lambda item: item[1][0])
        for key, _ in ordered[: len(ordered) // 2]:
            self._cache.pop(key, None)


def haversine_m(
    longitude1: float, latitude1: float, longitude2: float, latitude2: float
) -> float:
    """两坐标间的球面距离（米），输入输出均为十进制度。"""
    lon1, lat1, lon2, lat2 = map(
        math.radians, (longitude1, latitude1, longitude2, latitude2)
    )
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(a))


def normalize_country(country: str, country_code: str = "") -> str:
    name = (country or "").strip()
    code = (country_code or "").strip().upper()
    if code == "CN" or name in {
        "中华人民共和国",
        "中國",
        "中国",
        "People's Republic of China",
        "China",
    }:
        return "中国"
    return name


def compose_region(result: GeoResult | None, level: str = "district") -> str:
    """把省市区拼接成“中国浙江省宁波市鄞州区宁波大学”这样的字符串。"""
    if result is None:
        return ""

    country = normalize_country(result.country, result.country_code)
    if level == "province_city":
        raw_parts = [country, result.province, result.city, result.poi]
    elif level == "full":
        raw_parts = [
            country,
            result.province,
            result.city,
            result.district,
            result.township,
            result.poi,
        ]
    else:
        raw_parts = [
            country,
            result.province,
            result.city,
            result.district,
            result.poi,
        ]

    names: list[str] = []
    seen: set[str] = set()
    for part in raw_parts:
        name = (part or "").strip()
        if not name:
            continue
        key = _dedupe_key(name)
        if key and key in seen:
            continue
        seen.add(key)
        names.append(name)

    if not names:
        return ""
    if any(_has_cjk(name) for name in names):
        return "".join(names)
    return " ".join(names)


def _join_address(*parts: str) -> str:
    names = [part.strip() for part in parts if part and part.strip()]
    if not names:
        return ""
    if any(_has_cjk(name) for name in names):
        return "".join(names)
    return ", ".join(names)


def _dedupe_key(name: str) -> str:
    return _ADMIN_SUFFIX_RE.sub("", name.strip())


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def _as_text(value: Any) -> str:
    """高德接口有时用 [] 表示空字段，这里统一成字符串。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _as_text(item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        return ""
    return str(value).strip()


_GCJ_A = 6378245.0
_GCJ_EE = 0.00669342162296594323


def is_in_china(longitude: float, latitude: float) -> bool:
    return 72.004 <= longitude <= 137.8347 and 0.8293 <= latitude <= 55.8271


def wgs84_to_gcj02(longitude: float, latitude: float) -> tuple[float, float]:
    """WGS-84 → GCJ-02（火星坐标），高德地图使用 GCJ-02。"""
    if not is_in_china(longitude, latitude):
        return longitude, latitude

    delta_lat = _transform_lat(longitude - 105.0, latitude - 35.0)
    delta_lng = _transform_lng(longitude - 105.0, latitude - 35.0)

    rad_lat = latitude / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - _GCJ_EE * magic * magic
    sqrt_magic = math.sqrt(magic)

    delta_lat = (delta_lat * 180.0) / (
        (_GCJ_A * (1 - _GCJ_EE)) / (magic * sqrt_magic) * math.pi
    )
    delta_lng = (delta_lng * 180.0) / (
        _GCJ_A / sqrt_magic * math.cos(rad_lat) * math.pi
    )
    return longitude + delta_lng, latitude + delta_lat


def _transform_lat(x: float, y: float) -> float:
    value = (
        -100.0
        + 2.0 * x
        + 3.0 * y
        + 0.2 * y * y
        + 0.1 * x * y
        + 0.2 * math.sqrt(abs(x))
    )
    value += (
        (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi))
        * 2.0
        / 3.0
    )
    value += (
        (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi))
        * 2.0
        / 3.0
    )
    value += (
        (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0))
        * 2.0
        / 3.0
    )
    return value


def _transform_lng(x: float, y: float) -> float:
    value = (
        300.0
        + x
        + 2.0 * y
        + 0.1 * x * x
        + 0.1 * x * y
        + 0.1 * math.sqrt(abs(x))
    )
    value += (
        (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi))
        * 2.0
        / 3.0
    )
    value += (
        (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi))
        * 2.0
        / 3.0
    )
    value += (
        (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi))
        * 2.0
        / 3.0
    )
    return value
