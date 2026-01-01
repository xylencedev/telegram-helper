from fastapi import FastAPI, HTTPException
import logging
import asyncio
import os
import importlib.util

# Dynamically load the existing script `xylence-helper/main.py` because the folder name
# contains a hyphen and cannot be imported as a normal package name.
main_path = os.path.join(os.path.dirname(__file__), "xylence-helper", "main.py")
spec = importlib.util.spec_from_file_location("xylence_main", main_path)
tg_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tg_main)

api = FastAPI(title="Xylence-Helper Serve")
logger = logging.getLogger("uvicorn")

# The Pyrogram client object defined in the main script is named `app`
tg_client = tg_main.app

@api.on_event("startup")
async def startup_event():
    logger.info("Starting Pyrogram client...")
    try:
        await tg_client.start()
        logger.info("Pyrogram client started")
    except Exception as e:
        logger.exception("Failed to start Pyrogram client: %s", e)
        raise

@api.on_event("shutdown")
async def shutdown_event():
    logger.info("Stopping Pyrogram client...")
    try:
        await tg_client.stop()
        logger.info("Pyrogram client stopped")
    except Exception as e:
        logger.exception("Failed to stop Pyrogram client: %s", e)


@api.get("/")
async def root():
    """Simple health endpoint"""
    try:
        connected = getattr(tg_client, "is_connected", None)
    except Exception:
        connected = None
    return {"status": "ok", "pyrogram_connected": connected}


@api.get("/health")
async def health():
    """Health check"""
    try:
        connected = getattr(tg_client, "is_connected", None)
    except Exception:
        connected = None
    return {"status": "healthy", "pyrogram_connected": connected}


@api.get("/forward-data")
async def forward_data():
    """Proxy endpoint to fetch forward data from configured backend"""
    data = await tg_main.fetch_forward_data()
    if data is None:
        raise HTTPException(status_code=502, detail="Failed to fetch forward data")
    return data


@api.get("/ready")
async def ready():
    """Return ready when Pyrogram is connected"""
    connected = getattr(tg_client, "is_connected", False)
    return {"ready": bool(connected)}
