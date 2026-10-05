"""FastAPI inference service for the trained ViT-B/16 classifier."""

import io
import os
import sys
from contextlib import asynccontextmanager

import torch
import torch.nn as nn
import torchvision.models as models
from fastapi import FastAPI, File, HTTPException, UploadFile
from PIL import Image
from torchvision import transforms


MODEL_PATH = os.path.join(os.path.dirname(__file__), "best_vit_b16.pth")
CLASS_NAMES = ["autistic", "non_autistic"]
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
PORT = int(os.environ.get("ML_PORT", os.environ.get("FLASK_PORT", 5001)))
INFERENCE_TRANSFORM = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.ConvertImageDtype(torch.float),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])
MODEL: nn.Module | None = None


def load_model(path: str) -> nn.Module:
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Model file not found: {path}")
    model = models.vit_b_16(weights=None)
    model.heads.head = nn.Linear(model.heads.head.in_features, len(CLASS_NAMES))
    state_dict = torch.load(path, map_location=DEVICE, weights_only=False)
    model.load_state_dict(state_dict)
    model.to(DEVICE)
    model.eval()
    return model


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global MODEL
    try:
        MODEL = load_model(MODEL_PATH)
        print(f"[INFO] Loaded model from {MODEL_PATH} on {DEVICE}")
    except Exception as error:
        MODEL = None
        print(f"[ERROR] Model unavailable: {error}", file=sys.stderr)
    yield
    MODEL = None


app = FastAPI(title="AutiSmart ML Inference API", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "device": str(DEVICE), "modelLoaded": MODEL is not None}


@app.post("/predict")
async def predict(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "Empty filename. Please attach an image file.")
    if MODEL is None:
        raise HTTPException(503, "The trained model is unavailable. Place best_vit_b16.pth beside this service.")
    try:
        image_bytes = await file.read()
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as error:
        raise HTTPException(400, "Could not decode image. Make sure you are sending a valid JPEG/PNG file.") from error
    tensor = INFERENCE_TRANSFORM(image).unsqueeze(0).to(DEVICE)
    with torch.no_grad():
        probabilities = torch.softmax(MODEL(tensor), dim=1)
        confidence, predicted_index = torch.max(probabilities, dim=1)
    return {"label": CLASS_NAMES[predicted_index.item()], "confidence": round(confidence.item(), 4)}


if __name__ == "__main__":
    import uvicorn

    print(f"[INFO] Starting FastAPI inference service on port {PORT}...")
    uvicorn.run("api:app", host="0.0.0.0", port=PORT, reload=False)