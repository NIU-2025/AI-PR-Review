"""
分析结果本地持久化模块

负责将每次 PR Review 的分析结果保存为本地 JSON 文件。
写入操作在调用线程中同步完成（JSON 文件体积小，写入 <1ms，不影响响应延迟）。

典型使用:
    storage = ResultStorage(StorageConfig())
    storage.save_result(pr_url, analysis, pr_metadata, llm_model, token_used, duration_ms)

存储目录结构:
    results/
        owner_repo_12345_2026-05-30T14-30-22Z.json
        owner_repo_12346_2026-05-30T15-00-00Z.json
        ...

每份文件包含完整的 PR 元信息 + 分析结果, 便于离线复盘和对比。
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from config import StorageConfig

logger = logging.getLogger(__name__)

# 文件名不允许的字符，统一替换为下划线
_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')
# 最大文件名长度（不含扩展名）
_MAX_FILENAME_LENGTH = 200


class ResultStorage:
    """本地结果存储管理器"""

    def __init__(self, config: StorageConfig):
        """
        Args:
            config: 存储相关配置
        """
        self.config = config
        self.enabled = config.auto_save_enabled

        # 将相对路径转为绝对路径，确保无论从哪个目录启动都能写对位置
        raw_dir = config.results_dir
        if not os.path.isabs(raw_dir):
            # 相对路径：相对于项目根目录（backend 的父目录）
            # Path(__file__) = backend/utils/file_storage.py
            # .parent.parent = backend/
            # .parent = project_root/
            project_root = Path(__file__).resolve().parent.parent.parent
            raw_dir = str(project_root / raw_dir)

        self.results_dir = os.path.normpath(raw_dir)

        if self.enabled:
            self._ensure_dir()

    # ──────────────────────────────────────────────
    # 公开方法
    # ──────────────────────────────────────────────

    def save_result(
        self,
        pr_url: str,
        analysis,
        pr_metadata,
        llm_model: str = "",
        token_used: int = 0,
        duration_ms: int = 0,
    ) -> str | None:
        """
        保存分析结果到本地 JSON 文件（同步写入）

        Args:
            pr_url: PR 的完整 URL
            analysis: AnalysisResult 对象或 None
            pr_metadata: PRMetadata 对象
            llm_model: 使用的 LLM 模型名称
            token_used: 消耗的 token 数
            duration_ms: 分析耗时（毫秒）

        Returns:
            保存的文件路径，未启用时返回 None
        """
        if not self.enabled:
            print("[ResultStorage] auto_save_enabled=False, 跳过保存", flush=True)
            return None

        try:
            # ── 组装要保存的数据 ──
            saved_at = datetime.now(timezone.utc).isoformat()
            print(f"[ResultStorage] 开始组装记录, saved_at={saved_at}", flush=True)

            record = {
                "saved_at": saved_at,
                "pr_url": pr_url,
                "pr_metadata": pr_metadata.model_dump() if pr_metadata else {},
                "analysis": analysis.model_dump() if analysis else None,
                "llm_model": llm_model,
                "token_used": token_used,
                "analysis_duration_ms": duration_ms,
            }
            print(f"[ResultStorage] 记录组装完成, analysis is None = {analysis is None}", flush=True)

            # ── 生成文件名 ──
            filename = self._generate_filename(pr_url, saved_at)
            filepath = os.path.join(self.results_dir, filename)
            print(f"[ResultStorage] 目标文件: {filepath}", flush=True)

            # ── 同步写入 ──
            self._write_file(filepath, record)

            print(f"[ResultStorage] 写入成功: {filepath}", flush=True)
            return filepath

        except Exception as e:
            import traceback
            print(f"[ResultStorage] 保存失败!!! 异常: {e}", flush=True)
            traceback.print_exc()
            logger.error(f"save_result 异常: {e}", exc_info=True)
            return None

    def list_results(self) -> list[dict]:
        """
        列出所有已保存的分析结果（摘要信息）

        遍历 results/ 目录，只读取顶层字段（saved_at / pr_url / pr_metadata），
        不加载完整的 analysis 内容，避免占用过多内存。

        Returns:
            摘要信息列表，按保存时间倒序排列
        """
        if not os.path.isdir(self.results_dir):
            return []

        entries = []
        for filename in os.listdir(self.results_dir):
            if not filename.endswith(".json"):
                continue

            filepath = os.path.join(self.results_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    record = json.load(f)
                entries.append({
                    "filename": filename,
                    "saved_at": record.get("saved_at", ""),
                    "pr_url": record.get("pr_url", ""),
                    "pr_title": record.get("pr_metadata", {}).get("title", ""),
                    "risk_level": (
                        record.get("analysis", {})
                        .get("summary", {})
                        .get("risk_level", "")
                        if record.get("analysis")
                        else ""
                    ),
                    "token_used": record.get("token_used", 0),
                    "duration_ms": record.get("analysis_duration_ms", 0),
                })
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"读取结果文件失败: {filename}, 原因: {e}")

        # 按保存时间倒序
        entries.sort(key=lambda e: e["saved_at"], reverse=True)
        return entries

    def load_result(self, filename: str) -> dict | None:
        """
        加载完整分析结果

        Args:
            filename: 文件名（不含目录前缀）

        Returns:
            完整记录 dict，文件不存在或损坏返回 None
        """
        filepath = os.path.join(self.results_dir, filename)

        # 安全检查：防止路径穿越
        real_path = os.path.realpath(filepath)
        real_dir = os.path.realpath(self.results_dir)
        if not real_path.startswith(real_dir + os.sep) and real_path != real_dir:
            logger.warning(f"路径穿越检测: {filename}")
            return None

        if not os.path.isfile(filepath):
            logger.warning(f"结果文件不存在: {filename}")
            return None

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"加载结果文件失败: {filename}, 原因: {e}")
            return None

    # ──────────────────────────────────────────────
    # 私有方法
    # ──────────────────────────────────────────────

    def _ensure_dir(self):
        """确保结果目录存在，不存在则自动创建"""
        os.makedirs(self.results_dir, exist_ok=True)
        logger.info(f"结果存储目录已就绪: {self.results_dir}")

    def _generate_filename(self, pr_url: str, saved_at: str) -> str:
        """
        根据 PR URL 和保存时间生成唯一文件名

        文件命名规则:
            {owner}_{repo}_{pr_number}_{timestamp}.json

        例如:
            facebook_react_30000_2026-05-30T14-30-22Z.json

        Args:
            pr_url: PR 完整 URL
            saved_at: ISO 格式的保存时间戳（含微秒，如 2026-05-30T14:30:22.123456+00:00）

        Returns:
            清理过的文件名（Windows 兼容）
        """
        # 从 PR URL 中提取 owner / repo / pr_number
        owner, repo, pr_number = "unknown", "unknown", "0"
        match = re.search(
            r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)",
            pr_url,
        )
        if match:
            owner = _INVALID_FILENAME_CHARS.sub("_", match.group("owner"))
            repo = _INVALID_FILENAME_CHARS.sub("_", match.group("repo"))
            pr_number = match.group("number")

        # 将 ISO 时间戳转为文件名安全格式
        # 原始: 2026-05-30T14:30:22.123456+00:00
        # 步骤: 取前半段 → 替换冒号 → 得到 2026-05-30T14-30-22
        safe_time = saved_at.split(".")[0].replace(":", "-")

        filename = f"{owner}_{repo}_{pr_number}_{safe_time}.json"

        # 文件名过长时截断
        if len(filename) > _MAX_FILENAME_LENGTH:
            filename = f"{owner}_{repo}_{pr_number}_{safe_time[-19:]}.json"

        return filename

    def _write_file(self, filepath: str, record: dict):
        """
        将分析记录写入文件（同步）

        Args:
            filepath: 目标文件绝对路径
            record: 要写入的 dict 记录
        """
        print(f"[ResultStorage._write_file] 开始写入, path={filepath}", flush=True)
        try:
            json_text = json.dumps(record, ensure_ascii=False, indent=2, default=str)
            print(f"[ResultStorage._write_file] json序列化完成, 长度={len(json_text)}", flush=True)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(json_text)
            print(f"[ResultStorage._write_file] 文件写入完成: {filepath}", flush=True)
            logger.info(f"分析结果已保存: {filepath} ({len(json_text)} 字符)")
        except OSError as e:
            print(f"[ResultStorage._write_file] OS写入错误: {e}", flush=True)
            logger.error(f"保存分析结果失败: {filepath}, 原因: {e}")
        except Exception as e:
            import traceback
            print(f"[ResultStorage._write_file] 未知错误: {e}", flush=True)
            traceback.print_exc()
            logger.error(f"保存分析结果时未知错误: {filepath}, 原因: {e}")
