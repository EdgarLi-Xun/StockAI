# 从环境变量构建公网服务配置，集中校验站点地址、目录和 Cookie 策略。
from __future__ import annotations

import os
import ipaddress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from backend.config import ROOT


# 声明公网部署路径、会话、模型、邮件、备份及后台执行配置。
@dataclass(frozen=True)
class CloudConfig:
    data: Path
    erasures: Path
    key_file: Path
    public_url: str = "https://stockai.example.com"
    dev: bool = False
    age_bin: str = "age"
    age_recipient: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = False
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_enabled: bool = False
    background: bool = True
    model_timeout: int = 180
    backup_hour: int = 2
    desktop_mode: bool = False
    desktop_instance: str = ""
    desktop_origins: tuple[str,...] = ()
    sync_private_networks: tuple[str,...] = ()
    sync_ca_file: str = ""

    # 将环境变量转换为配置对象，并应用部署所需的默认值。
    @classmethod
    def from_env(cls):
        return cls(
            data=Path(os.environ.get("STOCKAI_CLOUD_DATA_DIR", ROOT/"cloud-data")).resolve(),
            erasures=Path(os.environ.get("STOCKAI_ERASURE_DIR", ROOT/"cloud-erasures")).resolve(),
            key_file=Path(os.environ.get("STOCKAI_MASTER_KEY_FILE", ROOT/"cloud-secrets"/"keys.json")).resolve(),
            public_url=os.environ.get("STOCKAI_PUBLIC_URL", "https://stockai.example.com").rstrip("/"),
            dev=os.environ.get("STOCKAI_CLOUD_DEV")=="1",
            age_bin=os.environ.get("STOCKAI_AGE_BIN", "age"), age_recipient=os.environ.get("STOCKAI_BACKUP_RECIPIENT", ""),
            smtp_host=os.environ.get("STOCKAI_CLOUD_SMTP_HOST", ""), smtp_port=int(os.environ.get("STOCKAI_CLOUD_SMTP_PORT", "465")),
            smtp_user=os.environ.get("STOCKAI_CLOUD_SMTP_USER", ""), smtp_password=os.environ.get("STOCKAI_CLOUD_SMTP_PASSWORD", ""),
            smtp_from=os.environ.get("STOCKAI_CLOUD_SMTP_FROM", ""), smtp_starttls=os.environ.get("STOCKAI_CLOUD_SMTP_STARTTLS")=="1",
            ollama_url=os.environ.get("STOCKAI_CLOUD_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
            ollama_enabled=os.environ.get("STOCKAI_CLOUD_OLLAMA_ENABLED", "0")=="1",
            background=os.environ.get("STOCKAI_CLOUD_NO_BACKGROUND")!="1",
            model_timeout=int(os.environ.get("STOCKAI_CLOUD_MODEL_TIMEOUT", "180")),
            sync_private_networks=tuple(v.strip() for v in os.environ.get('STOCKAI_SYNC_PRIVATE_NETWORKS','').split(',') if v.strip()),
            sync_ca_file=os.environ.get('STOCKAI_SYNC_CA_FILE',''),
        )

    # 检查生产站点地址、路径和关键配置，拒绝不满足条件的部署。
    def validate(self):
        p=urlsplit(self.public_url)
        if p.username or p.password or p.query or p.fragment or p.path not in {"", "/"} or not p.hostname:
            raise ValueError("公开站点 URL 必须为固定站点根地址")
        if p.scheme!="https" and not (self.dev and p.scheme=="http" and p.hostname in {"127.0.0.1","localhost","testserver"}):
            raise ValueError("公网入口必须使用 HTTPS；仅显式开发模式允许本机 HTTP")
        if self.desktop_mode and (not self.dev or p.scheme!="http" or p.hostname!="127.0.0.1" or not self.desktop_instance or not self.desktop_instance.isalnum()):
            raise ValueError("桌面服务必须使用明确的本机主入口和独立实例")
        if self.desktop_origins and not self.desktop_mode:
            raise ValueError("附加本机访问地址仅适用于桌面服务")
        for origin in self.desktop_origins:
            parsed=urlsplit(origin)
            if parsed.hostname!="localhost":
                address=ipaddress.IPv4Address(parsed.hostname)
                if address.is_unspecified or address.is_multicast or str(address)=="255.255.255.255":
                    raise ValueError("桌面访问地址必须为明确的本机 IPv4 地址")
            if parsed.port!=p.port or origin!=f"http://{parsed.hostname}:{p.port}":
                raise ValueError("桌面访问地址必须为同一端口的 HTTP 根地址")
        if self.erasures==self.data or self.erasures.is_relative_to(self.data) or self.key_file.is_relative_to(self.data):
            raise ValueError("注销记录与凭据主密钥必须保存在研究数据目录之外")
        if not 10<=self.model_timeout<=600:
            raise ValueError("模型超时须为10至600秒")
        o=urlsplit(self.ollama_url)
        if o.scheme not in {"http","https"} or not o.hostname or o.username or o.password or o.query or o.fragment or o.path not in {"", "/"}:
            raise ValueError("站点 Ollama 地址无效")

    # 根据开发或生产模式选择会话 Cookie 名称。
    @property
    def session_cookie(self):
        if self.desktop_mode:return "stockai_desktop_"+self.desktop_instance[:12]+"_session"
        return "stockai_dev_session" if self.dev else "__Host-stockai_session"

    # 根据开发或生产模式选择 CSRF Cookie 名称。
    @property
    def csrf_cookie(self):
        if self.desktop_mode:return "stockai_desktop_"+self.desktop_instance[:12]+"_csrf"
        return "stockai_dev_csrf" if self.dev else "__Host-stockai_csrf"
