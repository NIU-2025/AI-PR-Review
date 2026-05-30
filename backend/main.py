"""
AI PR Review 助手 - 服务入口

FastAPI 应用主文件, 负责:
- 创建 FastAPI 实例
- 配置 CORS 和日志
- 挂载路由
- 提供启动入口
"""

import logging
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import load_config
from routers.review import router as review_router

# ── 日志配置 ──

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)

# ── 应用实例 ──

config = load_config()

app = FastAPI(
    title="AI PR Review",
    description="基于大语言模型的 GitHub Pull Request 代码评审助手",
    version="0.1.0",
)

# ── CORS 配置 ──

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 路由注册 ──

app.include_router(review_router)


# ── 静态文件托管 ──

import os
from fastapi.responses import FileResponse

frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")

if os.path.isdir(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

    @app.get("/")
    async def serve_index():
        """托管前端首页"""
        return FileResponse(os.path.join(frontend_dir, "index.html"))


# ── 启动事件 ──

@app.on_event("startup")
async def startup_event():
    logger.info("=" * 50)
    logger.info("AI PR Review 助手启动")
    logger.info(f"LLM Model: {config.llm.model_name}")
    logger.info(f"GitHub API: {config.github.api_base_url}")
    logger.info("=" * 50)


# ── 直接运行入口 ──

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=config.host,
        port=config.port,
        reload=True,
        log_level="info",
    )
