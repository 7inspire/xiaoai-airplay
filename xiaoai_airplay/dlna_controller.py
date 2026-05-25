"""DLNA 控制器 - 通过 UPnP AVTransport 控制小爱音箱播放"""

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from xml.etree import ElementTree as ET

from async_upnp_client.aiohttp import AiohttpRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.client import UpnpDevice, UpnpService, UpnpAction

from .dlna_discover import DiscoveredDevice

logger = logging.getLogger(__name__)

DIDL_TEMPLATE = """<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
    <item id="1" parentID="0" restricted="1">
        <dc:title>{title}</dc:title>
        <upnp:class>object.item.audioItem.musicTrack</upnp:class>
        <res protocolInfo="http-get:*:{mime_type}:*">{url}</res>
    </item>
</DIDL-Lite>"""


class TransportState(Enum):
    STOPPED = "STOPPED"
    PLAYING = "PLAYING"
    PAUSED = "PAUSED_PLAYBACK"
    TRANSITIONING = "TRANSITIONING"
    NO_MEDIA = "NO_MEDIA_PRESENT"
    UNKNOWN = "UNKNOWN"


@dataclass
class PlaybackInfo:
    state: TransportState
    current_uri: str = ""
    track_duration: str = "00:00:00"
    rel_time: str = "00:00:00"


class DLNAController:
    """DLNA 播放控制器"""

    def __init__(self):
        self._requester: Optional[AiohttpRequester] = None
        self._device: Optional[UpnpDevice] = None
        self._av_transport: Optional[UpnpService] = None
        self._rendering_control: Optional[UpnpService] = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self, device: DiscoveredDevice) -> bool:
        """连接到 DLNA 设备"""
        logger.info("正在连接到 %s (%s)...", device.name, device.ip)

        try:
            self._requester = AiohttpRequester()
            factory = UpnpFactory(self._requester)
            self._device = await factory.async_create_device(device.location)

            # 查找 AVTransport 服务
            for svc in self._device.services.values():
                if "AVTransport" in svc.service_id:
                    self._av_transport = svc
                elif "RenderingControl" in svc.service_id:
                    self._rendering_control = svc

            if not self._av_transport:
                logger.error("设备不支持 AVTransport 服务")
                return False

            self._connected = True
            logger.info("已连接到 %s", device.name)

            # 列出支持的 actions
            actions = list(self._av_transport.actions.keys())
            logger.info("支持的 AVTransport Actions: %s", actions)

            return True

        except Exception as e:
            logger.error("连接设备失败: %s", e)
            await self.disconnect()
            return False

    async def disconnect(self):
        """断开连接"""
        if self._requester:
            await self._requester.async_close()
        self._device = None
        self._av_transport = None
        self._rendering_control = None
        self._connected = False
        logger.info("已断开连接")

    async def play_url(
        self,
        url: str,
        title: str = "Audio",
        mime_type: str = "audio/mpeg",
    ) -> bool:
        """播放指定 URL 的音频"""
        if not self._av_transport:
            logger.error("未连接到设备")
            return False

        metadata = DIDL_TEMPLATE.format(title=title, url=url, mime_type=mime_type)
        logger.info("推送音频: %s (%s)", title, url)

        try:
            # SetAVTransportURI
            action = self._av_transport.action("SetAVTransportURI")
            await action.async_call(
                InstanceID=0,
                CurrentURI=url,
                CurrentURIMetaData=metadata,
            )
            logger.info("SetAVTransportURI 成功")

            # Play
            await self.play()
            return True

        except Exception as e:
            logger.error("播放失败: %s", e)
            return False

    async def play(self) -> bool:
        """播放/恢复"""
        if not self._av_transport:
            return False
        try:
            action = self._av_transport.action("Play")
            await action.async_call(InstanceID=0, Speed="1")
            logger.info("Play 指令发送成功")
            return True
        except Exception as e:
            logger.error("Play 失败: %s", e)
            return False

    async def pause(self) -> bool:
        """暂停"""
        if not self._av_transport:
            return False
        try:
            action = self._av_transport.action("Pause")
            await action.async_call(InstanceID=0)
            logger.info("Pause 指令发送成功")
            return True
        except Exception as e:
            logger.error("Pause 失败 (设备可能不支持): %s", e)
            return False

    async def stop(self) -> bool:
        """停止"""
        if not self._av_transport:
            return False
        try:
            action = self._av_transport.action("Stop")
            await action.async_call(InstanceID=0)
            logger.info("Stop 指令发送成功")
            return True
        except Exception as e:
            logger.error("Stop 失败: %s", e)
            return False

    async def get_transport_info(self) -> Optional[PlaybackInfo]:
        """获取播放状态"""
        if not self._av_transport:
            return None
        try:
            action = self._av_transport.action("GetTransportInfo")
            result = await action.async_call(InstanceID=0)

            state_str = result.get("CurrentTransportState", "UNKNOWN")
            try:
                state = TransportState(state_str)
            except ValueError:
                state = TransportState.UNKNOWN

            return PlaybackInfo(state=state)
        except Exception as e:
            logger.error("获取状态失败: %s", e)
            return None

    async def get_position_info(self) -> Optional[PlaybackInfo]:
        """获取播放位置信息"""
        if not self._av_transport:
            return None
        try:
            action = self._av_transport.action("GetPositionInfo")
            result = await action.async_call(InstanceID=0)

            return PlaybackInfo(
                state=TransportState.UNKNOWN,
                current_uri=result.get("TrackURI", ""),
                track_duration=result.get("TrackDuration", "00:00:00"),
                rel_time=result.get("RelTime", "00:00:00"),
            )
        except Exception as e:
            logger.error("获取位置信息失败: %s", e)
            return None

    async def set_volume(self, volume: int) -> bool:
        """设置音量 (0-100)"""
        if not self._rendering_control:
            logger.warning("设备不支持 RenderingControl")
            return False
        try:
            volume = max(0, min(100, volume))
            action = self._rendering_control.action("SetVolume")
            await action.async_call(
                InstanceID=0,
                Channel="Master",
                DesiredVolume=volume,
            )
            logger.info("音量设置为 %d", volume)
            return True
        except Exception as e:
            logger.error("设置音量失败: %s", e)
            return False

    async def get_volume(self) -> Optional[int]:
        """获取当前音量"""
        if not self._rendering_control:
            return None
        try:
            action = self._rendering_control.action("GetVolume")
            result = await action.async_call(InstanceID=0, Channel="Master")
            return int(result.get("CurrentVolume", 0))
        except Exception as e:
            logger.error("获取音量失败: %s", e)
            return None
