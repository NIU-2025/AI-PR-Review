"""
Review 结果本地持久化存储

职责:
- 将每次 PR 分析结果保存为 JSON 文件 (reviews/{review_id}/result.json)
- 维护索引文件 (reviews/index.json), 记录 PR URL → review_id + ETag 映射
- 提供缓存查找: 给定 PR URL, 返回是否有已缓存的结果及其 ETag
- 提供结果加载: 按 review_id 读取完整分析结果
- 支持历史列表: 读取所有已缓存 Review 的摘要信息

设计原则:
- 索引文件 (index.json) 保持轻量 (< 50KB), 仅含摘要 + ETag, 用于快速查找
- 完整结果文件 (result.json) 按需加载, 避免大索引
- 文件写入使用原子操作 (先写临时文件再 rename), 防止写入中断导致数据损坏
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_REVIEWS_DIR = Path(__file__).resolve().parent.parent.parent / "reviews"
_INDEX_FILE = _REVIEWS_DIR / "index.json"


class ReviewStore:
    """Review 结果本地持久化存储"""

    def __init__(self):
        self._ensure_dir()

    def save_result(
        self,
        pr_url: str,
        pr_metadata: dict,
        analysis: dict,
        etag: str,
        llm_model: str = "",
        tokens_used: int = 0,
        duration_ms: int = 0,
    ) -> str:
        now = datetime.now(timezone.utc)
        review_id = self._generate_review_id(pr_url, now)
        result_dir = _REVIEWS_DIR / review_id

        result_dir.mkdir(parents=True, exist_ok=True)

        result = {
            "version": "1.0",
            "review_id": review_id,
            "pr_url": pr_url,
            "saved_at": now.isoformat(),
            "pr_metadata": pr_metadata,
            "analysis": analysis,
            "llm_model": llm_model,
            "tokens_used": tokens_used,
            "duration_ms": duration_ms,
        }

        tmp_path = result_dir / ".result.tmp"
        final_path = result_dir / "result.json"

        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp_path, final_path)

        risk_count = len(analysis.get("risks", []))
        risk_level = analysis.get("summary", {}).get("risk_level", "unknown")

        self._update_index(
            pr_url=pr_url,
            review_id=review_id,
            etag=etag,
            pr_title=pr_metadata.get("title", ""),
            pr_author=pr_metadata.get("author", ""),
            pr_updated_at=str(pr_metadata.get("updated_at", "")),
            risk_level=risk_level,
            risk_count=risk_count,
            saved_at=now,
            repo=self._extract_repo(pr_url),
            pr_number=self._extract_pr_number(pr_url),
        )

        logger.info(
            f"分析结果已保存: review_id={review_id}, "
            f"risks={risk_count}, tokens={tokens_used}"
        )
        return review_id

    def find_cached(self, pr_url: str) -> Optional[dict]:
        index = self._read_index()
        for item in index.get("items", []):
            if item.get("pr_url") == pr_url:
                return {
                    "review_id": item["review_id"],
                    "etag": item.get("etag", ""),
                    "saved_at": item.get("saved_at", ""),
                    "pr_updated_at": item.get("pr_updated_at", ""),
                }
        return None

    def load_result(self, review_id: str) -> Optional[dict]:
        result_path = _REVIEWS_DIR / review_id / "result.json"
        if not result_path.exists():
            logger.warning(f"结果文件不存在: {result_path}")
            return None

        with open(result_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def list_all(self) -> list[dict]:
        index = self._read_index()
        items = index.get("items", [])
        items.sort(key=lambda x: x.get("saved_at", ""), reverse=True)
        return items

    def _ensure_dir(self):
        _REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
        if not _INDEX_FILE.exists():
            self._write_index({"version": "1.0", "updated_at": "", "items": []})

    def _get_reviews_dir(self) -> Path:
        return _REVIEWS_DIR

    def _generate_review_id(self, pr_url: str, now: datetime) -> str:
        timestamp = now.strftime("%Y%m%d_%H%M%S")
        repo = self._extract_repo(pr_url) or "unknown"
        pr_number = self._extract_pr_number(pr_url) or "0"
        short_id = uuid.uuid4().hex[:4]
        return f"{timestamp}_{repo}_{pr_number}_{short_id}"

    def _extract_repo(self, pr_url: str) -> str:
        match = re.search(r"github\.com[:/]([^/]+/[^/]+)/pull/\d+", pr_url)
        if match:
            return match.group(1).replace("/", "_")
        return "unknown"

    def _extract_pr_number(self, pr_url: str) -> str:
        match = re.search(r"/pull/(\d+)", pr_url)
        return match.group(1) if match else "0"

    def _read_index(self) -> dict:
        try:
            with open(_INDEX_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError) as e:
            logger.warning(f"索引文件损坏或不存在, 重建空索引: {e}")
            return {"version": "1.0", "updated_at": "", "items": []}

    def _write_index(self, data: dict):
        tmp_path = _INDEX_FILE.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp_path, _INDEX_FILE)

    def _update_index(
        self,
        pr_url: str,
        review_id: str,
        etag: str,
        pr_title: str,
        pr_author: str,
        pr_updated_at: str,
        risk_level: str,
        risk_count: int,
        saved_at: datetime,
        repo: str,
        pr_number: str,
    ):
        index = self._read_index()
        items = index.get("items", [])

        items = [item for item in items if item.get("pr_url") != pr_url]

        new_item = {
            "review_id": review_id,
            "pr_url": pr_url,
            "etag": etag,
            "pr_title": pr_title,
            "pr_author": pr_author,
            "pr_updated_at": pr_updated_at,
            "repo": repo,
            "pr_number": pr_number,
            "risk_level": risk_level,
            "risk_count": risk_count,
            "saved_at": saved_at.isoformat(),
        }
        items.append(new_item)

        index["items"] = items
        index["updated_at"] = saved_at.isoformat()
        self._write_index(index)

        logger.debug(f"索引已更新: {pr_url} → {review_id}")
