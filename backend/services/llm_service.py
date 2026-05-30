"""
LLM 分析服务模块

负责调用大语言模型进行 PR 代码评审。
采用多阶段分析流程, 每个阶段有独立的 Prompt 和输出约束。

分析流程:
  Stage 1 - 变更总结: 理解 PR 做了什么
  Stage 2 - 风险识别: 逐文件/逐段识别风险代码 (带自我反驳)
  Stage 3 - 结果整合: 合并多阶段结果, 去重, 格式化

支持的模型: 任何兼容 OpenAI API 的模型 (GPT-4o, DeepSeek-V3, Claude 等)
"""

import json
import logging
import time
from typing import Optional

from openai import OpenAI

from config import LLMConfig
from models.schemas import (
    AnalysisResult,
    ChangeSummary,
    PRData,
    RiskItem,
)
from utils.context_builder import AnalysisMode, ContextBuilder

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Prompt 模板
# ──────────────────────────────────────────────

# === Stage 1: 变更总结 ===

SYSTEM_PROMPT_SUMMARY = """你是一位资深代码评审专家, 拥有 10 年以上软件开发经验。
你的任务是对 GitHub Pull Request 的变更进行客观、准确的总结。

## 输出要求
请以 JSON 格式输出, 包含以下字段:
{
  "overview": "用 3-5 句话概括这次 PR 的整体变更内容、目的和影响范围",
  "key_changes": ["变更点1", "变更点2", ...],    // 3-7 个关键变更点
  "affected_modules": ["模块1", "模块2", ...],    // 受影响的模块/领域
  "risk_level": "trivial|low|medium|high|critical"  // 整体风险等级
}

## 注意事项
- 客观描述, 不添加主观评价
- 基于 diff 内容总结, 不要猜测
- 如果 PR 描述中有变更目的, 优先参考
- 仅输出 JSON, 不要包含其他文字"""


# === Stage 2: 风险代码识别 ===

SYSTEM_PROMPT_RISK = """你是一位资深代码安全与质量审计专家。
你的任务是审查代码变更, 识别潜在的风险和问题。

## 风险等级定义
- P0 (关键): 确定有问题的代码, 可能导致安全漏洞、数据丢失、服务崩溃
- P1 (高): 很可能有问题, 如逻辑矛盾、明显性能问题、空指针/未定义行为
- P2 (中): 可能有隐患, 如不符合最佳实践、边界条件未处理、资源泄露风险
- P3 (低): 建议改进, 如命名不规范、注释缺失、代码风格

## 自我反驳机制 (重要!)
对每一个你标记的风险, 你必须先在内心完成自我反驳:
"这段代码在什么情况下实际上是安全的? 文件中是否有其他地方做了一样的模式?"
只有当反驳不成立时, 才将该风险输出。

## 白名单 (以下模式不应报告为风险)
- 非空检查的惯用写法 (如 if (x == null) return; 或 if not x: return)
- 日志级别/内容的调整 (除非导致敏感信息泄露)
- 注释增删、格式化调整、import 排序
- 测试文件中的 "硬编码" 测试数据
- 已经存在于旧代码中的模式 (仅标记新增代码中的问题)

## 输出要求
请以 JSON 格式输出风险列表, 每个风险包含:
{
  "risks": [
    {
      "severity": "P0|P1|P2|P3",
      "file": "文件路径",
      "line_range": "涉及行范围, 如 L42-L58",
      "title": "风险标题 (简洁)",
      "description": "风险详细描述, 说明为什么这是问题",
      "suggestion": "具体的改进建议或修改方案",
      "code_snippet": "相关代码片段",
      "confidence": 0.85  // 置信度 0-1, 低于 0.6 的风险不要输出
    }
  ]
}

如果 PR 变更中没有发现任何风险, 请输出 {"risks": []}。
仅输出 JSON, 不要包含其他文字。"""


# === Stage 1 (trivial模式) 简化版 ===

SYSTEM_PROMPT_TRIVIAL = """你是一位资深代码评审专家。
请对以下 PR 变更进行简要总结。

## 输出要求
请以 JSON 格式输出:
{
  "overview": "用 2-3 句话概括这次变更",
  "key_changes": ["变更点1"],
  "affected_modules": [],
  "risk_level": "trivial"
}
仅输出 JSON, 不要包含其他文字。"""


# ──────────────────────────────────────────────
# LLM 服务类
# ──────────────────────────────────────────────

class LLMService:
    """LLM 分析服务"""

    def __init__(self, config: LLMConfig):
        """
        Args:
            config: LLM 相关配置
        """
        self.config = config
        self.model = config.model_name

        self._client = OpenAI(
            api_key=config.api_key,
            base_url=config.api_base_url,
            timeout=config.request_timeout,
        )

    # ──────────────────────────────────────────────
    # 公开方法: 主分析入口
    # ──────────────────────────────────────────────

    def analyze(
        self,
        pr_data: PRData,
        context: str,
        mode: AnalysisMode,
    ) -> AnalysisResult:
        """
        主分析入口: 根据分析模式执行不同深度的分析

        Args:
            pr_data: PR 完整数据
            context: 组装好的上下文字符串
            mode: 分析模式

        Returns:
            AnalysisResult: 分析结果
        """
        start_time = time.time()
        total_tokens = 0

        # ── Stage 1: 变更总结 (所有模式都执行) ──
        logger.info(f"[Stage 1] 开始变更总结 (模式={mode.value})")
        summary = self._run_summary_stage(context, mode)
        logger.info(f"[Stage 1] 变更总结完成, 风险等级={summary.risk_level}")

        # ── Stage 2: 风险识别 (trivial 模式跳过) ──
        risks: list[RiskItem] = []
        if mode != AnalysisMode.TRIVIAL:
            logger.info(f"[Stage 2] 开始风险识别 (模式={mode.value})")
            risks = self._run_risk_stage(context, mode, pr_data)
            logger.info(f"[Stage 2] 风险识别完成, 发现 {len(risks)} 个风险")

        elapsed_ms = int((time.time() - start_time) * 1000)

        return AnalysisResult(
            summary=summary,
            risks=risks,
            llm_model=self.model,
            token_used=total_tokens,
            analysis_duration_ms=elapsed_ms,
        )

    # ──────────────────────────────────────────────
    # Stage 1: 变更总结
    # ──────────────────────────────────────────────

    def _run_summary_stage(
        self, context: str, mode: AnalysisMode
    ) -> ChangeSummary:
        """
        执行变更总结分析

        Args:
            context: 组装好的上下文
            mode: 分析模式

        Returns:
            ChangeSummary: 变更总结
        """
        if mode == AnalysisMode.TRIVIAL:
            system_prompt = SYSTEM_PROMPT_TRIVIAL
        else:
            system_prompt = SYSTEM_PROMPT_SUMMARY

        user_prompt = f"请分析以下 PR 变更:\n\n{context}"

        raw_output = self._call_llm(system_prompt, user_prompt)
        parsed = self._safe_json_parse(raw_output)

        return ChangeSummary(
            overview=parsed.get("overview", "无法生成总结"),
            key_changes=parsed.get("key_changes", []),
            affected_modules=parsed.get("affected_modules", []),
            risk_level=parsed.get("risk_level", "low"),
        )

    # ──────────────────────────────────────────────
    # Stage 2: 风险识别
    # ──────────────────────────────────────────────

    def _run_risk_stage(
        self,
        context: str,
        mode: AnalysisMode,
        pr_data: PRData,
    ) -> list[RiskItem]:
        """
        执行风险识别分析

        Args:
            context: 组装好的上下文
            mode: 分析模式
            pr_data: PR 数据 (用于上下文裁剪)

        Returns:
            风险项列表
        """
        user_prompt = f"请审查以下 PR 变更中的风险代码:\n\n{context}"

        raw_output = self._call_llm(SYSTEM_PROMPT_RISK, user_prompt)
        parsed = self._safe_json_parse(raw_output)
        raw_risks = parsed.get("risks", [])

        if not isinstance(raw_risks, list):
            logger.warning("风险识别返回格式异常, 已忽略")
            return []

        risks = []
        for item in raw_risks:
            try:
                risk = RiskItem(
                    severity=item.get("severity", "P3"),
                    file=item.get("file", ""),
                    line_range=item.get("line_range", ""),
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    suggestion=item.get("suggestion", ""),
                    code_snippet=item.get("code_snippet", ""),
                    confidence=float(item.get("confidence", 0.5)),
                )
                risks.append(risk)
            except Exception as e:
                logger.warning(f"解析风险项失败: {e}, 原始数据: {item}")

        # ── 模式过滤 ──
        risks = self._filter_by_mode(risks, mode)

        # ── 去重与合并 ──
        risks = self._deduplicate_risks(risks)

        # ── 数量上限 ──
        risks = risks[:10]

        return risks

    # ──────────────────────────────────────────────
    # 结果过滤与后处理
    # ──────────────────────────────────────────────

    def _filter_by_mode(
        self, risks: list[RiskItem], mode: AnalysisMode
    ) -> list[RiskItem]:
        """
        根据分析模式过滤风险

        - simple: 只保留 P0
        - normal: 保留 P0 + P1 (且 confidence ≥ 0.6)
        - large:  只保留 Top 5 (P0 优先)
        """
        if mode == AnalysisMode.SIMPLE:
            risks = [r for r in risks if r.severity == "P0"]
        elif mode in (AnalysisMode.NORMAL, AnalysisMode.LARGE):
            risks = [
                r for r in risks
                if r.severity in ("P0", "P1") and r.confidence >= 0.6
            ]

        if mode == AnalysisMode.LARGE:
            risks.sort(
                key=lambda r: (
                    0 if r.severity == "P0" else 1,
                    -r.confidence,
                )
            )
            risks = risks[:5]

        return risks

    def _deduplicate_risks(self, risks: list[RiskItem]) -> list[RiskItem]:
        """
        风险去重: 相同标题 + 相同文件的合并

        简单策略: 完全相同的 title 视为重复, 保留第一个。
        """
        seen = set()
        unique = []
        for risk in risks:
            key = (risk.title.lower(), risk.file)
            if key not in seen:
                seen.add(key)
                unique.append(risk)
        return unique

    # ──────────────────────────────────────────────
    # LLM 调用封装
    # ──────────────────────────────────────────────

    def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """
        调用 LLM 并返回原始文本

        Args:
            system_prompt: 系统提示
            user_prompt: 用户提示

        Returns:
            LLM 原始输出文本

        Raises:
            RuntimeError: LLM 调用失败
        """
        try:
            logger.info(
                f"调用 LLM (model={self.model}), "
                f"system_prompt={len(system_prompt)} chars, "
                f"user_prompt={len(user_prompt)} chars"
            )

            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )

            content = response.choices[0].message.content or ""

            usage = response.usage
            if usage:
                logger.info(
                    f"LLM 调用完成: "
                    f"prompt_tokens={usage.prompt_tokens}, "
                    f"completion_tokens={usage.completion_tokens}, "
                    f"total_tokens={usage.total_tokens}"
                )

            return content

        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            raise RuntimeError(f"LLM 调用失败: {e}") from e

    # ──────────────────────────────────────────────
    # 工具方法
    # ──────────────────────────────────────────────

    @staticmethod
    def _safe_json_parse(raw: str) -> dict:
        """
        安全 JSON 解析

        处理 LLM 输出中常见的格式问题:
        - ```json ... ``` 包裹
        - 首尾多余空白/换行
        - 尾随逗号
        """
        if not raw:
            return {}

        text = raw.strip()

        # 去除 markdown 代码块标记
        if text.startswith("```"):
            lines = text.split("\n")
            if len(lines) > 1:
                # 去掉第一行 (```json 或 ```) 和最后一行 (```)
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
            text = "\n".join(lines)

        text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试提取第一个 {...} 或 [...]
        for start_char, end_char in [("{", "}"), ("[", "]")]:
            start = text.find(start_char)
            end = text.rfind(end_char)
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    continue

        logger.warning(f"无法解析 LLM 输出为 JSON: {raw[:200]}...")
        return {}
