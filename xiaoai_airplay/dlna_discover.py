"""SSDP 设备发现 - 扫描局域网中的 DLNA/UPnP 设备"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from async_upnp_client.aiohttp import AiohttpRequester
from async_upnp_client.search import async_search
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.client import UpnpDevice

logger = logging.getLogger(__name__)

SSDP_TARGET_MEDIA_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
SSDP_TARGET_AV_TRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"


@dataclass
class DiscoveredDevice:
    """发现的 DLNA 设备"""
    name: str
    ip: str
    location: str  # UPnP description URL
    model: str = ""
    manufacturer: str = ""
    udn: str = ""
    av_transport_supported: bool = False


class DLNADiscovery:
    """DLNA 设备发现器"""

    def __init__(self, timeout: int = 5):
        self.timeout = timeout
        self._devices: dict[str, DiscoveredDevice] = {}

    async def scan(self) -> list[DiscoveredDevice]:
        """扫描局域网中的 DLNA MediaRenderer 设备"""
        self._devices.clear()
        logger.info("开始扫描 DLNA 设备 (超时 %ds)...", self.timeout)

        try:
            await async_search(
                search_target=SSDP_TARGET_MEDIA_RENDERER,
                timeout=self.timeout,
                async_callback=self._on_device_found,
            )
        except Exception as e:
            logger.error("SSDP 扫描出错: %s", e)

        devices = list(self._devices.values())
        logger.info("扫描完成，发现 %d 个 DLNA 设备", len(devices))
        for d in devices:
            logger.info("  - %s (%s) [%s]", d.name, d.ip, d.model)
        return devices

    async def _on_device_found(self, headers: dict):
        """SSDP 响应回调"""
        location = headers.get("location", headers.get("LOCATION", ""))
        if not location or location in self._devices:
            return

        try:
            device_info = await self._fetch_device_info(location)
            if device_info:
                self._devices[location] = device_info
        except Exception as e:
            logger.debug("获取设备信息失败 %s: %s", location, e)

    async def _fetch_device_info(self, location: str, timeout: float = 3.0) -> Optional[DiscoveredDevice]:
        """获取设备详细信息"""
        requester = AiohttpRequester()
        factory = UpnpFactory(requester)

        try:
            device = await asyncio.wait_for(
                factory.async_create_device(location),
                timeout=timeout,
            )
            ip = location.split("//")[1].split(":")[0].split("/")[0]

            has_av_transport = any(
                "AVTransport" in svc.service_id
                for svc in device.services.values()
            )

            return DiscoveredDevice(
                name=device.friendly_name or "Unknown",
                ip=ip,
                location=location,
                model=device.model_name or "",
                manufacturer=device.manufacturer or "",
                udn=device.udn or "",
                av_transport_supported=has_av_transport,
            )
        finally:
            await requester.async_close()

    async def probe_by_ip(self, ip: str) -> Optional[DiscoveredDevice]:
        """通过 IP 直接探测设备的 UPnP 描述（不依赖 SSDP 广播）"""
        # 小爱音箱常见的 UPnP description URL 端口和路径
        probe_urls = [
            f"http://{ip}:8443/description.xml",
            f"http://{ip}:49152/description.xml",
            f"http://{ip}:49153/description.xml",
            f"http://{ip}:1400/xml/device_description.xml",
            f"http://{ip}:8008/ssdp/device-desc.xml",
            f"http://{ip}:80/description.xml",
            f"http://{ip}:8080/description.xml",
            f"http://{ip}:52235/dmr.xml",
            f"http://{ip}:9197/dmr",
        ]

        logger.info("通过 IP 直接探测设备: %s", ip)
        for url in probe_urls:
            try:
                device_info = await self._fetch_device_info(url)
                if device_info:
                    logger.info("探测成功: %s (%s)", device_info.name, url)
                    return device_info
            except Exception:
                continue

        logger.warning("IP %s 的常见 UPnP 端口均无响应", ip)
        return None

    async def find_xiaoai(
        self,
        device_name: Optional[str] = None,
        device_ip: Optional[str] = None,
    ) -> Optional[DiscoveredDevice]:
        """查找小爱音箱"""
        # 优先通过 IP 直接探测（不依赖 SSDP 多播）
        if device_ip:
            device = await self.probe_by_ip(device_ip)
            if device:
                return device
            logger.info("直接探测未成功，回退到 SSDP 扫描...")

        devices = await self.scan()

        for d in devices:
            if device_ip and d.ip == device_ip:
                logger.info("通过 IP 匹配到设备: %s", d.name)
                return d
            if device_name and device_name.lower() in d.name.lower():
                logger.info("通过名称匹配到设备: %s", d.name)
                return d

        # 尝试通过制造商识别小爱
        xiaomi_keywords = ["xiaomi", "小米", "xiao", "miot", "yeelight"]
        for d in devices:
            combined = f"{d.name} {d.manufacturer} {d.model}".lower()
            if any(kw in combined for kw in xiaomi_keywords):
                logger.info("通过制造商匹配到小米设备: %s (%s)", d.name, d.manufacturer)
                return d

        logger.warning("未找到小爱音箱，发现的设备: %s", [d.name for d in devices])
        return None


async def main():
    """独立运行：扫描并列出所有 DLNA 设备"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    discovery = DLNADiscovery(timeout=5)
    devices = await discovery.scan()

    if not devices:
        print("\n未发现任何 DLNA 设备。请确认：")
        print("  1. 设备与本机在同一局域网")
        print("  2. 路由器未开启 AP 隔离")
        return

    print(f"\n发现 {len(devices)} 个设备：")
    print("-" * 60)
    for i, d in enumerate(devices, 1):
        print(f"  {i}. {d.name}")
        print(f"     IP: {d.ip}")
        print(f"     型号: {d.model}")
        print(f"     制造商: {d.manufacturer}")
        print(f"     AVTransport: {'✓' if d.av_transport_supported else '✗'}")
        print(f"     Location: {d.location}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
