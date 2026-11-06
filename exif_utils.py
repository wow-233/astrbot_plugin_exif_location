"""读取图片 EXIF 中的 GPS 坐标和常用拍摄信息。

这个模块只做本地解析，不会上传图片。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image as PILImage
from PIL.ExifTags import TAGS

try:  # 可选依赖：安装 pillow-heif 后可以读取 iPhone 的 HEIC 照片
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:  # pragma: no cover - 未安装时静默跳过
    pass

_GPS_IFD = 0x8825
_EXIF_IFD = 0x8769

_BASE_FIELDS = ("Make", "Model", "Software", "Artist", "Copyright", "DateTime")
_EXIF_FIELDS = (
    "DateTimeOriginal",
    "DateTimeDigitized",
    "LensMake",
    "LensModel",
    "ExifImageWidth",
    "ExifImageHeight",
    "ExposureTime",
    "FNumber",
    "ISOSpeedRatings",
    "PhotographicSensitivity",
    "FocalLength",
)

_LABELS = {
    "Make": "相机厂商",
    "Model": "相机型号",
    "Software": "处理软件",
    "Artist": "作者",
    "Copyright": "版权",
    "DateTime": "修改时间",
    "DateTimeOriginal": "拍摄时间",
    "DateTimeDigitized": "数字化时间",
    "LensMake": "镜头厂商",
    "LensModel": "镜头型号",
    "ExifImageWidth": "EXIF 宽度",
    "ExifImageHeight": "EXIF 高度",
    "ExposureTime": "曝光时间",
    "FNumber": "光圈",
    "ISOSpeedRatings": "ISO",
    "PhotographicSensitivity": "ISO",
    "FocalLength": "焦距",
}


@dataclass
class GpsInfo:
    """图片中的 GPS 信息（WGS-84）。"""

    latitude: float
    longitude: float
    altitude: float | None = None
    timestamp: str | None = None


@dataclass
class ImageInfo:
    """一次解析得到的图片元信息。"""

    width: int = 0
    height: int = 0
    has_exif: bool = False
    make: str = ""
    model: str = ""
    lens: str = ""
    software: str = ""
    datetime: str = ""
    datetime_original: str = ""
    gps: GpsInfo | None = None


def analyze_image(path: str | Path) -> ImageInfo:
    """解析图片的 EXIF；解析失败时返回空的 ImageInfo。"""
    info = ImageInfo()
    try:
        with PILImage.open(path) as image:
            info.width = int(image.width)
            info.height = int(image.height)
            exif = image.getexif()
            info.has_exif = bool(exif)
            if not exif:
                return info

            merged: dict[str, Any] = {}
            try:
                for tag, value in exif.items():
                    name = TAGS.get(tag)
                    if name:
                        merged[name] = value
            except Exception:
                pass
            try:
                for tag, value in exif.get_ifd(_EXIF_IFD).items():
                    name = TAGS.get(tag)
                    if name:
                        merged[name] = value
            except Exception:
                pass

            info.make = _text(merged.get("Make"))
            info.model = _text(merged.get("Model"))
            info.lens = _text(merged.get("LensModel"))
            info.software = _text(merged.get("Software"))
            info.datetime = _format_datetime(_text(merged.get("DateTime")))
            info.datetime_original = _format_datetime(
                _text(merged.get("DateTimeOriginal"))
            )
            info.gps = _gps_from_exif(exif)
    except Exception:
        return ImageInfo()
    return info


def extract_gps(path: str | Path) -> GpsInfo | None:
    """读取图片中的 GPS 信息，没有或格式异常时返回 None。"""
    return analyze_image(path).gps


def describe_exif(path: str | Path) -> list[tuple[str, str]]:
    """返回适合展示的 EXIF 字段列表，每一项为 (中文标签, 值)。"""
    return format_exif_rows(analyze_image(path))


def format_exif_rows(info: ImageInfo) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if info.width and info.height:
        rows.append(("图片尺寸", f"{info.width} × {info.height}"))

    device = device_name(info)
    if device:
        rows.append(("拍摄设备", device))
    if info.lens:
        rows.append(("镜头", info.lens))
    if info.software:
        rows.append(("处理软件", info.software))
    if info.datetime_original:
        rows.append(("拍摄时间", info.datetime_original))
    elif info.datetime:
        rows.append(("修改时间", info.datetime))

    gps = info.gps
    if gps is not None:
        rows.append(
            ("GPS 坐标", f"{gps.latitude:.6f}, {gps.longitude:.6f}（WGS-84）")
        )
        if gps.altitude is not None:
            rows.append(("海拔", f"{gps.altitude:.1f} 米"))
        if gps.timestamp:
            rows.append(("GPS 时间", gps.timestamp))
    return rows


def device_name(info: ImageInfo) -> str:
    """拼接厂商和型号，避免出现重复，例如 Apple Apple iPhone。"""
    make = (info.make or "").strip()
    model = (info.model or "").strip()
    if make and model:
        if model.lower().startswith(make.lower()):
            return model
        return f"{make} {model}"
    return make or model


def format_dms(latitude: float, longitude: float) -> str:
    """把十进制坐标转成 29°48′22.7″N 121°33′23.8″E 的形式。"""

    def convert(value: float, positive: str, negative: str) -> str:
        hemisphere = positive if value >= 0 else negative
        value = abs(value)
        degrees = int(value)
        minutes_float = (value - degrees) * 60
        minutes = int(minutes_float)
        seconds = (minutes_float - minutes) * 60
        return f"{degrees}°{minutes:02d}′{seconds:04.1f}″{hemisphere}"

    return (
        f"{convert(latitude, 'N', 'S')} {convert(longitude, 'E', 'W')}"
    )


def _gps_from_exif(exif: Any) -> GpsInfo | None:
    if not exif:
        return None
    try:
        gps = exif.get_ifd(_GPS_IFD)
    except Exception:
        return None
    if not gps:
        return None

    latitude = _dms_to_decimal(gps.get(2))
    longitude = _dms_to_decimal(gps.get(4))
    if latitude is None or longitude is None:
        return None

    if _text(gps.get(1)).upper() == "S":
        latitude = -latitude
    if _text(gps.get(3)).upper() == "W":
        longitude = -longitude

    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    # 有些设备在没有定位成功时会写入 0,0，这种坐标没有意义。
    if abs(latitude) < 1e-8 and abs(longitude) < 1e-8:
        return None

    altitude = None
    if gps.get(6) is not None:
        altitude = _number(gps.get(6))
        if _altitude_ref_is_below(gps.get(5)):
            altitude = -altitude

    return GpsInfo(
        latitude=latitude,
        longitude=longitude,
        altitude=altitude,
        timestamp=_gps_timestamp(gps),
    )


def _gps_timestamp(gps: dict[int, Any]) -> str | None:
    date = _text(gps.get(29))
    raw_time = gps.get(7)
    time_text = ""
    if isinstance(raw_time, (tuple, list)) and raw_time:
        hour = int(_number(raw_time[0]))
        minute = int(_number(raw_time[1])) if len(raw_time) > 1 else 0
        second = int(_number(raw_time[2])) if len(raw_time) > 2 else 0
        time_text = f"{hour:02d}:{minute:02d}:{second:02d}"
    parts = [part for part in (date, time_text) if part]
    if not parts:
        return None
    return " ".join(parts) + " UTC"


def _dms_to_decimal(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, (tuple, list)) or not value:
        return None
    degrees = _number(value[0])
    minutes = _number(value[1]) if len(value) > 1 else 0.0
    seconds = _number(value[2]) if len(value) > 2 else 0.0
    return degrees + minutes / 60.0 + seconds / 3600.0


def _number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (tuple, list)) and len(value) == 2:
        try:
            return float(value[0]) / float(value[1])
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _altitude_ref_is_below(value: Any) -> bool:
    if isinstance(value, bytes):
        return len(value) == 1 and value[0] == 1
    try:
        return int(value) == 1
    except (TypeError, ValueError):
        return False


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        for encoding in ("utf-8", "gbk", "latin-1"):
            try:
                return value.decode(encoding).strip("\x00").strip()
            except UnicodeDecodeError:
                continue
        return value.decode("utf-8", "ignore").strip("\x00").strip()
    if isinstance(value, (tuple, list)):
        return ", ".join(part for part in (_text(item) for item in value) if part)
    return str(value).strip()


def _format_datetime(text: str) -> str:
    """EXIF 时间形如 2024:05:01 12:00:00，转换成 2024-05-01 12:00:00。"""
    if len(text) >= 19 and text[4] == ":" and text[7] == ":":
        return f"{text[:4]}-{text[5:7]}-{text[8:]}"
    return text
