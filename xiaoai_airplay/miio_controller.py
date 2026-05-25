"""MiService 控制器 - 通过小米云服务 API 控制小爱音箱播放

参考 xiaomusic 项目的实现思路：
1. 通过小米账号登录获取设备列表
2. 调用 MIoT 指令控制音箱播放指定 URL
3. 支持 TTS、播放控制等

认证方式（二选一）：
  方式 1 - 账号密码：
    MI_USER: 小米账号（手机号/邮箱）
    MI_PASS: 密码
  方式 2 - Token（更安全）：
    MI_USER: userId
    MI_PASS: passToken
  通用：
    MI_DID:  设备 DID（可选，不填则自动搜索）
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TOKEN_STORE_PATH = os.path.join(
    os.path.expanduser("~"), ".mi_token.json"
)


@dataclass
class XiaoaiDevice:
    """小爱音箱设备信息"""
    did: str
    name: str
    model: str
    ip: str = ""
    hardware: str = ""


class MiServiceController:
    """通过 MiService 云 API 控制小爱音箱

    支持两种认证方式：
    1. account + password (常规登录)
    2. userId + passToken (从浏览器 cookie 获取，更安全)
    """

    def __init__(
        self,
        account: str = "",
        password: str = "",
        did: str = "",
    ):
        self.account = account or os.getenv("MI_USER", "")
        self.password = password or os.getenv("MI_PASS", "")
        self.target_did = did or os.getenv("MI_DID", "")

        self._service = None
        self._device: Optional[XiaoaiDevice] = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def device(self) -> Optional[XiaoaiDevice]:
        return self._device

    def _is_token_auth(self) -> bool:
        """判断是否使用 userId+passToken 认证（userId 是纯数字）"""
        return self.account.isdigit()

    def _prepare_token_store(self):
        """预写入 token 文件，让 MiAccount 跳过登录直接使用 token"""
        from miservice.miaccount import MiTokenStore, get_random

        token_data = {
            "userId": self.account,
            "passToken": self.password,
            "deviceId": get_random(16).upper(),
        }
        store = MiTokenStore(TOKEN_STORE_PATH)
        store.save_token(token_data)
        logger.info("Token 文件已写入: %s", TOKEN_STORE_PATH)
        return store

    async def connect(self) -> bool:
        """连接小米云服务并获取设备信息

        支持两种方式：
        1. account(手机号/邮箱) + password → 常规登录
        2. account(userId 纯数字) + password(passToken) → Token 认证
        """
        if not self.account or not self.password:
            logger.error(
                "未设置小米账号。请设置环境变量 MI_USER 和 MI_PASS，"
                "或在配置文件中填写 mi_account 和 mi_password"
            )
            return False

        try:
            import ssl
            import aiohttp
            from miservice import MiAccount, MiNAService, MiIOService

            # macOS 的 Python 可能缺少根证书，禁用 SSL 验证
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            session = aiohttp.ClientSession(connector=connector)

            if self._is_token_auth():
                logger.info("使用 userId+passToken 方式认证 (userId=%s)", self.account)
                token_store = self._prepare_token_store()
                self._mi_account = MiAccount(session, self.account, self.password, token_store)
            else:
                logger.info("使用账号密码方式认证 (account=%s)", self.account)
                self._mi_account = MiAccount(session, self.account, self.password, TOKEN_STORE_PATH)

            self._session = session
            self._miio_service = MiIOService(self._mi_account)
            self._mina_service = MiNAService(self._mi_account)

            # 获取设备列表
            devices = await self._mina_service.device_list()
            if not devices:
                logger.error("未获取到任何小爱音箱设备")
                return False

            logger.info("发现 %d 个小爱音箱设备:", len(devices))
            for d in devices:
                did = d.get("deviceID", "")
                name = d.get("name", "Unknown")
                model = d.get("hardware", "")
                miot_did = d.get("miotDID", "")
                logger.info("  - %s (DID: %s, 型号: %s, miotDID: %s)", name, did, model, miot_did)

            # 选择目标设备
            target = None
            if self.target_did:
                for d in devices:
                    if d.get("deviceID") == self.target_did or d.get("miotDID") == self.target_did:
                        target = d
                        break
                if not target:
                    logger.error("未找到 DID=%s 的设备", self.target_did)
                    return False
            else:
                target = devices[0]
                logger.info("未指定 DID，使用第一个设备")

            self._device = XiaoaiDevice(
                did=target.get("deviceID", ""),
                name=target.get("name", ""),
                model=target.get("hardware", ""),
            )
            self._miot_did = target.get("miotDID", "")
            self._connected = True
            logger.info("已连接到: %s (DID: %s)", self._device.name, self._device.did)
            return True

        except Exception as e:
            logger.error("连接小米云服务失败: %s", e)
            return False

    async def play_url(self, url: str) -> bool:
        """让小爱音箱播放指定 URL"""
        if not self._connected:
            logger.error("未连接到设备")
            return False

        try:
            # 优先使用 play_by_url（轻量，直接播放）
            result = await self._mina_service.play_by_url(self._device.did, url)
            logger.info("play_by_url 结果: %s", result)
            return True
        except Exception as e:
            logger.warning("play_by_url 失败: %s，尝试 play_by_music_url...", e)

        try:
            # 备选：play_by_music_url（带音乐元数据）
            result = await self._mina_service.play_by_music_url(self._device.did, url)
            logger.info("play_by_music_url 结果: %s", result)
            return True
        except Exception as e:
            logger.error("播放失败: %s", e)
            return False

    async def play_by_miot(self, url: str) -> bool:
        """通过 MIoT 协议播放 URL（备选方案）

        小爱音箱的 MIoT SIID/AIID:
        - SIID 3 (Speaker): 音箱控制
        - SIID 4 (Intelligent Speaker): 智能音箱
          - AIID 1: play (播放)
          - AIID 3: play_url (播放URL)
        """
        if not self._connected or not self._miot_did:
            return False

        try:
            # action: play-url
            # 不同型号的 SIID/AIID 可能不同
            params_list = [
                # 常见的 play URL action
                {"did": self._miot_did, "siid": 3, "aiid": 1, "in": [url]},
                {"did": self._miot_did, "siid": 4, "aiid": 3, "in": [url]},
                {"did": self._miot_did, "siid": 7, "aiid": 4, "in": [url]},
            ]

            for params in params_list:
                try:
                    result = await self._miio_service.miot_action(params)
                    logger.info("MIoT action %s 结果: %s", params, result)
                    if result and result.get("code") == 0:
                        return True
                except Exception as e:
                    logger.debug("MIoT action %s 失败: %s", params, e)
                    continue

            logger.error("所有 MIoT action 尝试均失败")
            return False

        except Exception as e:
            logger.error("play_by_miot 失败: %s", e)
            return False

    async def tts(self, text: str) -> bool:
        """让小爱音箱播报文字"""
        if not self._connected:
            return False

        try:
            await self._mina_service.text_to_speech(self._device.did, text)
            logger.info("TTS: %s", text)
            return True
        except Exception as e:
            logger.error("TTS 失败: %s", e)
            return False

    async def stop(self) -> bool:
        """停止播放"""
        if not self._connected:
            return False

        try:
            await self._mina_service.player_stop(self._device.did)
            logger.info("停止播放")
            return True
        except Exception as e:
            logger.error("停止失败: %s", e)
            return False

    async def pause(self) -> bool:
        """暂停播放"""
        if not self._connected:
            return False

        try:
            result = await self._mina_service.player_pause(self._device.did)
            logger.info("暂停播放, 响应: %s", result)
            # 检查 ubus 响应码
            code = (result or {}).get("data", {}).get("code", 0)
            if code != 0:
                logger.warning("player_pause 返回错误码 %d，尝试 player_stop 代替", code)
                result2 = await self._mina_service.player_stop(self._device.did)
                code2 = (result2 or {}).get("data", {}).get("code", 0)
                return code2 == 0
            return True
        except Exception as e:
            logger.error("暂停失败: %s", e)
            return False

    async def resume(self) -> bool:
        """继续播放"""
        if not self._connected:
            return False

        try:
            result = await self._mina_service.player_play(self._device.did)
            logger.info("继续播放, 响应: %s", result)
            code = (result or {}).get("data", {}).get("code", 0)
            return code == 0
        except Exception as e:
            logger.error("继续播放失败: %s", e)
            return False

    async def set_volume(self, volume: int) -> bool:
        """设置音量 (0-100)"""
        if not self._connected:
            return False

        try:
            volume = max(0, min(100, volume))
            await self._mina_service.player_set_volume(self._device.did, volume)
            logger.info("音量设置为 %d", volume)
            return True
        except Exception as e:
            logger.error("设置音量失败: %s", e)
            return False

    async def get_status(self) -> dict:
        """获取播放状态"""
        if not self._connected:
            return {}

        try:
            info = await self._mina_service.player_get_status(self._device.did)
            return info or {}
        except Exception as e:
            logger.error("获取状态失败: %s", e)
            return {}
