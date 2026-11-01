from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("STOCKAI_DATA_DIR", ROOT / "data")).resolve()
TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
VERSION = "2.0.0"
STRATEGY_VERSION = "ma-cross-next-open/2.0.0"


def now() -> datetime:
    return datetime.now(TZ)


def timestamp() -> str:
    return now().isoformat(timespec="seconds")


class Fees(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stock_commission: float = Field(0.0003, ge=0, le=0.05)
    stock_minimum: float = Field(5, ge=0, le=100)
    etf_commission: float = Field(0.0003, ge=0, le=0.05)
    etf_minimum: float = Field(5, ge=0, le=100)
    stock_transfer: float = Field(0.00001, ge=0, le=0.01)
    stock_stamp_sell: float = Field(0.0005, ge=0, le=0.05)
    slippage_bps: float = Field(5, ge=0, le=500)
    confirmed: bool = False
    note: str = Field("默认费率仅为模拟假设，尚未核对券商交割单。ETF不套用股票印花税和过户费。", max_length=500)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = Field("qwen3-14b-16k:latest", min_length=1, max_length=120)
    ai_enabled: bool = True
    ai_timeout: int = Field(180, ge=10, le=600)
    budget: float = Field(1000, gt=0, le=1000)
    fees: Fees = Field(default_factory=Fees)
    timezone: str = "Asia/Shanghai"
    scheduler_enabled: bool = True
    morning_time: str = "08:00"
    closing_time: str = "16:00"
    weekly_time: str = "20:00"
    min_daily_amount: float = Field(1000000, ge=0, le=1e12)
    max_risk_age_hours: int = Field(24, ge=1, le=72)
    data_provider: str = "akshare"
    email_enabled: bool = False
    smtp_host: str = Field("", max_length=200)
    smtp_port: int = Field(465, ge=1, le=65535)
    smtp_user: str = Field("", max_length=200)
    email_to: str = Field("", max_length=200)
    lan_enabled: bool = False
    proxy_hostname: str = Field("",max_length=200)
    allowed_networks: list[str] = Field(default_factory=lambda: ["192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"])

    @field_validator("ollama_url")
    @classmethod
    def local_model_only(cls, value: str) -> str:
        p = urlsplit(value)
        if p.scheme != "http" or p.hostname not in {"127.0.0.1", "localhost", "::1"} or p.username or p.password or p.path not in {"", "/"} or p.query or p.fragment:
            raise ValueError("仅允许本机、不含凭据的 Ollama HTTP 地址")
        return value.rstrip("/")

    @field_validator("morning_time", "closing_time", "weekly_time")
    @classmethod
    def check_time(cls, value: str) -> str:
        datetime.strptime(value, "%H:%M")
        if len(value) != 5:
            raise ValueError("时间格式须为 HH:MM")
        return value

    @field_validator("timezone")
    @classmethod
    def shanghai_only(cls, value: str) -> str:
        if value != "Asia/Shanghai":
            raise ValueError("本版调度统一使用 Asia/Shanghai")
        return value

    @field_validator("data_provider")
    @classmethod
    def provider_only(cls, value: str) -> str:
        if value != "akshare":
            raise ValueError("不支持的数据源")
        return value

    @field_validator("allowed_networks")
    @classmethod
    def private_networks(cls, value: list[str]) -> list[str]:
        import ipaddress
        if not value or len(value) > 20:
            raise ValueError("至少提供一个内网 CIDR")
        for raw in value:
            net = ipaddress.ip_network(raw)
            if not net.is_private or net.prefixlen == 0:
                raise ValueError("仅允许私有网段")
        return value

    @field_validator("proxy_hostname")
    @classmethod
    def proxy_host(cls,value):
        import re
        if value and not re.fullmatch(r"[a-zA-Z0-9.-]+",value):
            raise ValueError("反向代理主机名仅包含域名，不含协议或路径")
        return value.lower()
