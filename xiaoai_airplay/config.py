"""配置管理"""

import os
from dataclasses import dataclass, field
from typing import Optional

import yaml


@dataclass
class Config:
    # HTTP 音频服务
    http_host: str = "0.0.0.0"
    http_port: int = 8080

    # AirPlay 接收
    airplay_name: str = "小爱音箱-AirPlay"
    airplay_port: int = 7100

    # DLNA
    dlna_scan_timeout: int = 5
    target_device_name: Optional[str] = None
    target_device_ip: Optional[str] = None

    # 音频
    audio_format: str = "mp3"  # mp3 或 aac
    audio_bitrate: str = "320k"
    music_dir: str = "./music"

    # 小米账号（MiService 云 API）
    mi_account: str = ""
    mi_password: str = ""
    mi_did: str = ""

    # 日志
    log_level: str = "INFO"

    @classmethod
    def from_yaml(cls, path: str) -> "Config":
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            config = cls(**{k: v for k, v in data.items() if hasattr(cls, k)})
        else:
            config = cls()
        # 环境变量优先级高于配置文件（避免密码写入文件）
        config.mi_account = os.getenv("MI_USER", "") or config.mi_account
        config.mi_password = os.getenv("MI_PASS", "") or config.mi_password
        config.mi_did = os.getenv("MI_DID", "") or config.mi_did
        return config

    def to_yaml(self, path: str):
        from dataclasses import asdict

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(asdict(self), f, allow_unicode=True, default_flow_style=False)

    def get_local_ip(self) -> str:
        """获取本机局域网 IP"""
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            s.close()
