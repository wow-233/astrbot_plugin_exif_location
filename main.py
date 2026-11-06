"""AstrBot 图片 EXIF 定位插件。

监听聊天中的图片，读取 EXIF 里的 GPS 坐标，逆地理编码并识别附近地点后回复，
也可以发送 /exif 查看图片的详细 EXIF。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star

from .exif_utils import analyze_image, format_exif_rows
from .formatting import (
    build_map_links,
    build_reply,
    detailed_region,
    format_nearby_line,
)
from .geocoder import ReverseGeocoder, sanitize_error


class ImageExifPlugin(Star):
    """读取图片 EXIF 定位并向群友“打招呼”的插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.geocoder = ReverseGeocoder(self.config, logger=logger)
        self._last_trigger: dict[str, float] = {}

    async def terminate(self):
        """插件被停用/卸载时释放 HTTP 连接。"""
        try:
            await self.geocoder.close()
        except Exception:
            logger.debug("关闭逆地理编码 HTTP 客户端失败", exc_info=True)

    def _conf(self, key: str, default: Any) -> Any:
        value = self.config.get(key, default)
        return default if value is None else value

    def _enabled_for(self, event: AstrMessageEvent) -> bool:
        if not bool(self._conf("enabled", True)):
            return False
        if event.is_private_chat():
            return bool(self._conf("enable_private", True))
        return bool(self._conf("enable_groups", True))

    def _cooldown_key(self, event: AstrMessageEvent) -> str:
        # 按“会话 + 发送者”冷却，避免一个人发图把其他人的图也冷却掉。
        return f"{event.unified_msg_origin}:{event.get_sender_id()}"

    def _on_cooldown(self, event: AstrMessageEvent) -> bool:
        try:
            seconds = float(self._conf("cooldown_seconds", 5) or 0)
        except (TypeError, ValueError):
            seconds = 5.0
        if seconds <= 0:
            return False

        if len(self._last_trigger) > 4096:
            cutoff = time.monotonic() - 86400
            self._last_trigger = {
                key: value
                for key, value in self._last_trigger.items()
                if value >= cutoff
            }
        last = self._last_trigger.get(self._cooldown_key(event), 0.0)
        return time.monotonic() - last < seconds

    def _collect_images(self, event: AstrMessageEvent) -> list[Image]:
        """收集消息中的图片，包括引用消息和合并转发里的图片。"""
        images: list[Image] = []
        seen: set[int] = set()

        def walk(components: Any) -> None:
            if not isinstance(components, (list, tuple)):
                return
            for component in components:
                marker = id(component)
                if marker in seen:
                    continue
                seen.add(marker)
                if isinstance(component, Image):
                    images.append(component)
                    continue
                for attribute in ("chain", "content"):
                    nested = getattr(component, attribute, None)
                    if isinstance(nested, (list, tuple)):
                        walk(nested)

        walk(event.get_messages())
        return images

    @staticmethod
    def _is_exif_command(event: AstrMessageEvent) -> bool:
        """避免 /exif 指令同时触发自动定位回复。"""
        try:
            activated = event.get_extra("activated_handlers") or []
        except Exception:
            activated = []
        if any(
            getattr(handler, "handler_name", "") == "exif_command"
            for handler in activated
        ):
            return True

        text = (event.message_str or "").strip().lower()
        first = text.split(maxsplit=1)[0] if text else ""
        return first in {"exif", "/exif", "!exif"}

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_image_message(self, event: AstrMessageEvent):
        """监听带图片的消息，读取 EXIF 中的 GPS 并回复地点。"""
        try:
            if not self._enabled_for(event):
                return
            if self._conf("only_when_mentioned", False) and not event.is_at_or_wake_command:
                return
            if self._is_exif_command(event):
                return
            if self._on_cooldown(event):
                return

            images = self._collect_images(event)
            if not images:
                return

            try:
                limit = max(1, int(self._conf("max_images", 2)))
            except (TypeError, ValueError):
                limit = 2

            replies: list[str] = []
            for image in images[:limit]:
                try:
                    path = await image.convert_to_file_path()
                except Exception as exc:
                    logger.debug("图片下载失败: %s", sanitize_error(exc))
                    continue

                try:
                    info = await asyncio.to_thread(analyze_image, path)
                except Exception as exc:
                    logger.debug("EXIF 解析失败: %s", sanitize_error(exc))
                    continue
                if info.gps is None:
                    continue

                geo = None
                try:
                    geo = await self.geocoder.lookup(
                        info.gps.latitude, info.gps.longitude
                    )
                except Exception as exc:
                    logger.warning("逆地理编码失败: %s", sanitize_error(exc))

                text = build_reply(
                    self.config,
                    info,
                    geo,
                    sender=str(event.get_sender_name() or ""),
                    group=str(event.get_group_id() or ""),
                )
                if text:
                    replies.append(text)

            if not replies:
                return

            self._last_trigger[self._cooldown_key(event)] = time.monotonic()
            for text in replies:
                yield event.plain_result(text)
            if bool(self._conf("stop_event_after_reply", False)):
                event.stop_event()
        except Exception:
            logger.exception("处理图片 EXIF 消息时出现未预期异常")

    @filter.command("exif")
    async def exif_command(self, event: AstrMessageEvent):
        """查看图片的 EXIF 信息并解析拍摄地址。发送 /exif 时附带或引用一张图片。"""
        images = self._collect_images(event)
        if not images:
            yield event.plain_result(
                "请在发送 /exif 时附带一张图片，或引用一张图片后再发送 /exif。"
            )
            return

        try:
            path = await images[0].convert_to_file_path()
        except Exception as exc:
            logger.warning("图片读取失败: %s", sanitize_error(exc))
            yield event.plain_result("图片读取失败，请稍后再试。")
            return

        try:
            info = await asyncio.to_thread(analyze_image, path)
        except Exception as exc:
            logger.warning("EXIF 读取失败: %s", sanitize_error(exc))
            yield event.plain_result("EXIF 读取失败，请稍后再试。")
            return

        if not info.has_exif and info.gps is None:
            yield event.plain_result(
                "没有读取到 EXIF 信息，平台可能已压缩图片并移除了元数据。"
            )
            return

        lines = ["📷 图片 EXIF 信息"]
        lines.extend(f"{label}：{value}" for label, value in format_exif_rows(info))

        gps = info.gps
        if gps is not None:
            geo = None
            try:
                geo = await self.geocoder.lookup(gps.latitude, gps.longitude)
            except Exception as exc:
                logger.warning("逆地理编码失败: %s", sanitize_error(exc))
            region = detailed_region(geo) if geo is not None else ""
            if geo is not None:
                address = (geo.address or "").strip() or region
                if address:
                    lines.append(f"解析地址：{address}")
                nearby = format_nearby_line(geo)
                if nearby:
                    lines.append(nearby)
            lines.extend(build_map_links(self.config, gps, geo, region))

        yield event.plain_result("\n".join(lines))
