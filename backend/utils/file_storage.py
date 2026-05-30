"""
文件存储服务

封装 ReviewStore, 为路由层提供统一的持久化 API。
职责:
- 缓存查找: 给定 PR URL, 查找是否已有缓存结果
- 结果保存: 将分析结果写入本地文件并更新索引
- 结果加载: 按 review_id 读取完整结果
- 历史列表: 获取所有已缓存 Review 的摘要
"""

import logging
from typing import Optional

from services.review_store import ReviewStore

logger = logging.getLogger(__name__)


class ResultStorage:
    """文件存储服务 (对 ReviewStore 的门面封装)"""

    def __init__(self, config=None):
        self._store = ReviewStore()
        self.enabled = getattr(config, "auto_save_enabled", True) if config else True
        self.results_dir = str(self._store._get_reviews_dir())

    def find_cached(self, pr_url: str) -> Optional[dict]:
        return self._store.find_cached(pr_url)

    def load_result(self, review_id: str) -> Optional[dict]:
        return self._store.load_result(review_id)

    def save_result(
        self,
        pr_url: str,
        analysis,
        pr_metadata,
        llm_model: str = "",
        token_used: int = 0,
        duration_ms: int = 0,
        etag: str = "",
    ) -> str:
        return self._store.save_result(
            pr_url=pr_url,
            pr_metadata=pr_metadata.model_dump() if hasattr(pr_metadata, 'model_dump') else pr_metadata,
            analysis=analysis.model_dump() if hasattr(analysis, 'model_dump') else analysis,
            etag=etag,
            llm_model=llm_model,
            tokens_used=token_used,
            duration_ms=duration_ms,
        )

    def list_all(self) -> list[dict]:
        return self._store.list_all()
