"""
main.py  ―  FastAPI エントリポイント
=========================================
エンドポイント:
  POST /webhook  … LINE Messaging API のウェブフック受信
  GET  /health   … ヘルスチェック（Railway / Render のポーリング用）
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException

from app.line_bot import handler
from app.scheduler import start_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    # サーバー起動時にスケジューラを開始
    start_scheduler()
    yield
    # シャットダウン時の処理（必要なら追記）


app = FastAPI(
    title="KU-LMS 課題締切通知サービス",
    lifespan=lifespan,
)


@app.post("/webhook")
async def webhook(request: Request):
    """LINE Messaging API からのウェブフックを受け取る"""
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()

    try:
        handler.handle(body.decode("utf-8"), signature)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"status": "ok"}


@app.get("/health")
async def health():
    """Railway / Render のヘルスチェック用"""
    return {"status": "ok"}
