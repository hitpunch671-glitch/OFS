"""Servidor FastAPI: recibe los webhooks de TradingView."""
from __future__ import annotations

import hmac
import json
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from bingx import BingX
from config import Settings
from engine import Engine
from messages import startup_msg
from packet import PacketError, parse_packet
from state import State
from tg_client import Telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


def build_engine(cfg: Settings) -> Engine:
    bx = BingX(cfg.bingx_base_url, cfg.bingx_api_key, cfg.bingx_api_secret, cfg.position_mode) if cfg.executes_orders else None
    return Engine(cfg, State(cfg.data_dir, cfg.tz), Telegram(cfg.telegram_token, cfg.telegram_chat_id), bx)


def create_app(engine: Engine | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = engine or build_engine(Settings.from_env())
        await app.state.engine.tg.send(startup_msg(app.state.engine.cfg))
        yield
        await app.state.engine.tg.aclose()
        if app.state.engine.bx:
            await app.state.engine.bx.aclose()

    app = FastAPI(title="OFS → BingX + Telegram", lifespan=lifespan)

    def _auth(request: Request, secret: str | None) -> None:
        expected = request.app.state.engine.cfg.webhook_secret
        if not secret or not hmac.compare_digest(secret, expected):
            raise HTTPException(status_code=401, detail="secret inválido")

    @app.get("/")
    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/status")
    async def status(request: Request, secret: str | None = None):
        _auth(request, secret)
        e = request.app.state.engine
        d = e.state.d
        return {"mode": e.cfg.trading_mode, "day": d["day"], "trades_today": d["trades_today"],
                "loss_streak": d["loss_streak"], "day_r": d["day_r"], "open": d["open"],
                "last_closed": d["history"][-5:]}

    @app.post("/webhook")
    async def webhook(request: Request, background: BackgroundTasks, secret: str | None = None):
        _auth(request, secret)
        raw = (await request.body()).decode("utf-8", errors="replace").strip()
        try:
            pkt = parse_packet(json.loads(raw))
        except (ValueError, PacketError) as e:      # JSON inválido o schema distinto
            log.warning("Paquete ignorado: %s", e)
            return {"ok": False, "error": str(e)}
        # TradingView espera respuesta rápida: procesamos en segundo plano.
        background.add_task(_process, request.app.state.engine, pkt)
        return {"ok": True, "event": pkt.event}

    return app


async def _process(engine: Engine, pkt) -> None:
    try:
        res = await engine.handle(pkt)
        log.info("%s -> %s", pkt.event, res)
    except Exception:  # noqa: BLE001
        log.exception("Error procesando %s", pkt.event)
        await engine.tg.send(f"🚨 <b>Error del bot</b> procesando {pkt.event}. Revisa los logs de Railway.")


app = create_app()
