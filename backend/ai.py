# 调用本机模型解释已确定的研究事实，并校验输出结构与引用。
from __future__ import annotations

import json
import re
import threading
import subprocess
import unicodedata
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, Field

from . import db
from .config import ROOT

MODEL_LOCK = threading.Lock()


# 约束单个候选的定性解释，禁止模型增加未声明字段。
class Explanation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument_id: str
    rationale: str = Field(max_length=1500)
    counterargument: str = Field(max_length=1000)
    invalidation: str = Field(max_length=1000)
    source_ids: list[str]


# 约束报告解释、风险提示、缺失数据和来源引用的输出结构。
class ResearchText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=2000)
    market_context: str = Field(max_length=3000)
    candidates: list[Explanation] = Field(max_length=3)
    risk_notes: list[str] = Field(max_length=15)
    missing_data: list[str] = Field(max_length=30)
    source_ids: list[str]


class ReportCommentary(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    market_context: str = Field(min_length=1, max_length=1800, pattern=r"^[^0-9]*$")
    risk_notes: list[Annotated[str, Field(max_length=380, pattern=r"^[^0-9]*$")]] = Field(default_factory=list, max_length=5)


def validate_commentary(strings):
    for value in strings:
        normalized = unicodedata.normalize("NFKC", value)
        compact = re.sub(r"[\s\W_]+", "", normalized)
        if re.search(r"\d|[零〇一二两三四五六七八九十百千万亿点]+(?:元|块钱|股|份|成)|百分之", normalized):
            raise ValueError("数值、比例、数量与日期由程序展示")
        if any(word in compact for word in ("保证盈利", "稳赚", "必涨", "保本保收益")):
            raise ValueError("模型输出含有收益保证表述")
        if re.search(r"停复牌|停牌|复牌|退市|摘牌|终止上市|暂停上市|特别处理|恢复交易|正常交易|正在交易|禁止交易|停止交易|中止交易|可正常买卖|交易已恢复|风险警示板|(?<![A-Za-z])ST(?![A-Za-z])", compact, re.IGNORECASE):
            raise ValueError("证券交易状态只由程序展示")


def read_commentary(content):
    text = ReportCommentary.model_validate_json(content)
    validate_commentary([text.market_context, *text.risk_notes])
    return {**text.model_dump(), "candidates": [], "missing_data": []}


def report_prompt(mode, *, commentary=False, retry=False):
    prompt = (ROOT / "config" / ("prompt-full.md" if mode == "full" else "prompt-standard.md")).read_text(encoding="utf-8").strip()
    if commentary:
        task = (
            '只输出 {"market_context":"中文定性解读","risk_notes":["中文风险提示"]}。'
            '程序已生成摘要、候选、研究理由和来源表。只写背景和风险，不输出候选表、证券代码、来源编号、数字或个股交易状态。'
            '用“短期均线”“分钟线”“核验证据不足”等文字，资料不足时说明限制。'
        )
        if retry:
            task += '上次解读没有通过校验。这次用简短的中文定性句子，风险提示可为空数组，禁止日期、金额、比例及交易状态。'
        return f"{prompt}\n{task}"
    return (
        f"{prompt}\n返回既定 JSON 字段。候选标识与程序输入完全相同，按输入给每只候选列出定性理由、反方观点、失效条件。"
        "引用该候选自身的来源标识，模型不决定候选、交易状态或数值。"
    )


# 探测配置中的本地模型是否可用，并返回可展示的状态说明。
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


# 核对候选标识与来源引用，阻止模型篡改事实或编造来源。
def validate_output(text: ResearchText, facts: dict) -> ResearchText:
    expected = {c["instrument_id"]: set(c.get("source_ids", ())) for c in facts["candidates"]}
    got = [c.instrument_id for c in text.candidates]
    if len(got) != len(set(got)) or set(got) != expected.keys():
        raise ValueError("模型增删了程序确定的候选")
    source_ids = {s["id"] for s in facts["sources"]}
    if not set(text.source_ids) <= source_ids or any(
        not c.source_ids or not set(c.source_ids) <= (source_ids & expected[c.instrument_id])
        for c in text.candidates
    ):
        raise ValueError("模型引用了不存在的来源")
    # 模型解释只承载定性内容；价格、数量和日期等数值始终由程序事实单独展示。
    strings = [text.summary, text.market_context, *text.risk_notes, *text.missing_data]
    for candidate in text.candidates:
        strings += [candidate.rationale, candidate.counterargument, candidate.invalidation]
    validate_commentary(strings)
    return text


# 提交结构化事实并校验模型结果；模型不可用时保留规则报告能力。
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
        pass  # 没有 GPU 或无法获取显存信息时，仍允许尝试 CPU 推理。
    if not MODEL_LOCK.acquire(blocking=False):
        return None, "已有本地模型任务运行，使用规则型报告"
    try:
        payload = {"model":cfg.model,"stream":False,"think":False,"format":ReportCommentary.model_json_schema(),"options":{"temperature":0.1,"num_ctx":16384,"num_predict":1200},"messages":[{"role":"system","content":report_prompt(mode,commentary=True)},{"role":"user","content":db.dumps(facts)}]}
        if len(json.dumps(payload,ensure_ascii=False).encode("utf-8")) > 60000:
            return None, "事实输入超出本地上下文预算，使用规则型报告"
        error = None
        for _ in range(2):
            try:
                with httpx.Client(timeout=cfg.ai_timeout,trust_env=False,follow_redirects=False) as c:
                    response = c.post(cfg.ollama_url + "/api/chat",json=payload)
                    response.raise_for_status()
                return read_commentary(response.json()["message"]["content"]), None
            except Exception as exc:
                from pydantic import ValidationError
                reason=exc.errors()[0]["msg"] if isinstance(exc,ValidationError) else str(exc)[:180]
                error = f"AI 输出未通过校验或调用失败：{type(exc).__name__}（{reason}）；已使用规则型报告"
                payload["messages"][0]["content"] = report_prompt(mode,commentary=True,retry=True)
        return None, error
    finally:
        MODEL_LOCK.release()
