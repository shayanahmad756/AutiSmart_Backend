from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from starlette.staticfiles import StaticFiles
import socketio

from . import core
from .admin import router as admin_router
from .assessments import router as assessments_router
from .auth import router as auth_router
from .chat import CHAT_UPLOAD_DIR, router as chat_router, socket_server
from .children import router as children_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    if core.settings.mongo_uri:
        core.mongo_client = AsyncIOMotorClient(core.settings.mongo_uri, serverSelectionTimeoutMS=5000)
        try:
            await core.mongo_client.admin.command("ping")
            print("MongoDB connected")
        except Exception as error:
            print(f"MongoDB connection failed: {error}")
    else:
        print("MONGO_URI is not configured")
    yield
    if core.mongo_client is not None:
        core.mongo_client.close()
        core.mongo_client = None


app = FastAPI(title="AutiSmart API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(children_router)
app.include_router(assessments_router)
app.include_router(chat_router)
app.mount("/uploads/chat", StaticFiles(directory=CHAT_UPLOAD_DIR), name="chat-uploads")


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, error: HTTPException):
    message = error.detail if isinstance(error.detail, str) else "Request failed"
    return JSONResponse(status_code=error.status_code, content={"success": False, "message": message}, headers=error.headers)


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, error: Exception):
    print(f"Unhandled API error: {error}")
    message = str(error) if core.settings.node_env == "development" else "Internal server error"
    return JSONResponse(status_code=500, content={"success": False, "message": message})


@app.get("/health")
async def health():
    return {"success": True, "message": "Server is running"}


socket_app = socketio.ASGIApp(socket_server, other_asgi_app=app)