# ============================================================
# 文件: backend/main.py
# 狀態: 已完成 (已檢查 1 次)
# 問題: 無
# 關聯文件:
#   → backend/api/routes.py      (REST API 路由)
#   → backend/api/websocket.py   (WebSocket 端點)
#   → backend/db/database.py     (資料庫初始化)
# 函數結構:
#   - create_app() -> FastAPI
#   - lifespan(app) -> async context manager
#   - main entry: uvicorn.run
# ============================================================
"""
ancserTPX Backend — FastAPI 入口

啟動:
    cd backend
    python main.py
    # 或
    uvicorn main:app --reload --port 8000
"""

from __future__ import annotations
import os
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# 載入 .env（從專案根目錄）
_project_root = Path(__file__).parent.parent
load_dotenv(_project_root / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Suppress noisy httpx request logs (we have our own broker logs)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """應用生命週期"""
    logger.info("ancserTPX backend starting...")
    username = os.getenv("TOPSTEPX_USERNAME", "")
    logger.info(f"  .env loaded: username={username}, api_key={'***set***' if os.getenv('TOPSTEPX_API_KEY') else 'NOT SET'}")
    # 1.0.9 P0: 每日 20:10 UTC 影子重放(實盤 vs 同參數回測逐筆對賬)
    import asyncio as _asyncio
    from backend.api.routes import shadow_replay_daily_task
    _shadow_task = _asyncio.create_task(shadow_replay_daily_task())
    yield
    _shadow_task.cancel()   # 1.0.9
    logger.info("ancserTPX backend stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="ancserTPX",
        description="TopstepX NQ futures automated trading system",
        version="1.0.6",
        lifespan=lifespan,
    )

    # CORS — 允許前端 localhost 連接
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 掛載路由
    from backend.api.routes import router
    app.include_router(router, prefix="/api")

    # 掛載前端靜態文件
    frontend_dir = Path(__file__).parent.parent / "frontend" / "static"
    if frontend_dir.exists():
        app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

        @app.get("/")
        async def serve_frontend():
            return FileResponse(
                str(frontend_dir / "ancserTPX.html"),
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )

        @app.get("/favicon.ico", include_in_schema=False)
        async def serve_favicon():
            return FileResponse(str(frontend_dir / "favicon.ico"))

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    # Suppress verbose uvicorn access log (IP, port etc.)
    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["access"]["fmt"] = '%(asctime)s %(message)s'
    log_config["formatters"]["access"]["datefmt"] = '%H:%M:%S'
    log_config["formatters"]["default"]["fmt"] = '%(asctime)s %(message)s'
    log_config["formatters"]["default"]["datefmt"] = '%H:%M:%S'
    uvicorn.run(
        "backend.main:app", host="0.0.0.0", port=8001,
        log_config=log_config,
    )
