from __future__ import annotations

import json
import re
import threading
import subprocess
from decimal import Decimal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from . import db
from .config import ROOT

MODEL_LOCK = threading.Lock()


class Explanation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument_id: str
    rationale: str = Field(max_length=1500)
    counterargument: str = Field(max_length=1000)
    invalidation: str = Field(max_length=1000)
    source_ids: list[str]


class ResearchText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=2000)
    market_context: str = Field(max_length=3000)
    candidates: list[Explanation] = Field(max_length=3)
    risk_notes: list[str] = Field(max_length=15)
    missing_data: list[str] = Field(max_length=30)
    source_ids: list[str]


def health() -> dict:
    cfg = db.settings()
    try:
        with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as c:
            response = c.get(cfg.ollama_url + "/api/tags")
            response.raise_for_status()
        models = [m["name"] for m in response.json().get("models", [])]
        available = cfg.model in models
        return {"available": available, "enabled": cfg.ai_enabled, "model": cfg.model, "models": models, "message": "模型就绪" if available else "Ollama 在线，但指定模型未安装"}
    except Exception:
        return {"available": False, "enabled": cfg.ai_enabled, "model": cfg.model, "models": [], "message": "无法连接本机 Ollama；规则计算仍可使用"}


def validate_output(text: ResearchText, facts: dict) -> ResearchText:
    expected = {c["instrument_id"] for c in facts["candidates"]}
    got = [c.instrument_id for c in text.candidates]
    if len(got) != len(set(got)) or set(got) != expected:
        raise ValueError("模型增删了程序确定的候选")
    source_ids = {s["id"] for s in facts["sources"]}
    if not set(text.source_ids) <= source_ids or any(not set(c.source_ids) <= source_ids or not c.source_ids for c in text.candidates):
        raise ValueError("模型引用了不存在的来源")
    # Model prose cannot introduce a new numeric fact. Executable values are never read from prose.
    strings = [text.summary, text.market_context, *text.risk_notes, *text.missing_data]
    for candidate in text.candidates:
        strings += [candidate.rationale, candidate.counterargument, candidate.invalidation]
    for value in strings:
        if re.search(r"\d",value):
            raise ValueError("AI定性解释中包含数字；数值与日期必须由程序单独展示")
        if any(x in value for x in ("保证盈利", "稳赚", "必涨", "保本保收益")):
            raise ValueError("模型输出含有收益保证表述")
    return text


def explain(facts: dict, mode: str) -> tuple[dict | None, str | None]:
    cfg = db.settings()
    if not cfg.ai_enabled:
        return None, "AI 已关闭，使用规则型报告"
    state = health()
    if not state["available"]:
        return None, state["message"]
    try:
        result=subprocess.run(["nvidia-smi","--query-gpu=memory.free","--format=csv,noheader,nounits"],capture_output=True,text=True,timeout=3)
        if result.returncode==0 and result.stdout.strip() and max(int(x.strip()) for x in result.stdout.splitlines())<2048:
            return None,"GPU 可用显存低于2GB，暂缓推理并使用规则型报告；请关闭占用较高的程序后重试"
    except (OSError,ValueError,subprocess.TimeoutExpired):
        pass  # CPU-only installations and unavailable GPU telemetry remain supported.
    if not MODEL_LOCK.acquire(blocking=False):
        return None, "已有本地模型任务运行，使用规则型报告"
    try:
        prompt = (ROOT / "config" / ("prompt-full.md" if mode == "full" else "prompt-standard.md")).read_text(encoding="utf-8")
        payload = {"model":cfg.model,"stream":False,"think":False,"format":ResearchText.model_json_schema(),"options":{"temperature":0.1,"num_ctx":16384,"num_predict":3000},"messages":[{"role":"system","content":prompt},{"role":"user","content":db.dumps(facts)}]}
        if len(json.dumps(payload,ensure_ascii=False).encode("utf-8")) > 60000:
            return None, "事实输入超出本地上下文预算，使用规则型报告"
        error = None
        for _ in range(2):
            try:
                with httpx.Client(timeout=cfg.ai_timeout,trust_env=False,follow_redirects=False) as c:
                    response = c.post(cfg.ollama_url + "/api/chat",json=payload)
                    response.raise_for_status()
                result = ResearchText.model_validate_json(response.json()["message"]["content"])
                return validate_output(result, facts).model_dump(), None
            except Exception as exc:
                from pydantic import ValidationError
                reason=exc.errors()[0]["msg"] if isinstance(exc,ValidationError) else str(exc)[:180]
                error = f"AI 输出未通过校验或调用失败：{type(exc).__name__}（{reason}）；已使用规则型报告"
                payload["messages"].append({"role":"user","content":"上一次输出未通过程序校验。原因："+reason+"。解释文字仅写定性内容，严禁使用阿拉伯数字、日期、代码、数量；数值由程序另行显示。不要增删候选或来源。重新返回完整JSON。"})
        return None, error
    finally:
        MODEL_LOCK.release()
