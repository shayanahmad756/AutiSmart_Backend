# AutiSmart FastAPI Backend

The API and Socket.IO server are Python ASGI services. The API preserves the existing `/api` routes and MongoDB collections so the React frontend and existing records can continue to work.

## Run the API

From this directory, create and activate a virtual environment, then install dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
```

Create `.env` from `.env.example` and set the MongoDB URI, a new JWT secret, and any optional Groq/email credentials. For URIs without a database path, the API uses MongoDB's `test` database, matching the previous Mongoose default. Set `ML_URL` if inference runs somewhere other than `http://localhost:5001`.

Start the HTTP API and Socket.IO server on port 5000:

```powershell
py -m uvicorn app.main:socket_app --reload --host 0.0.0.0 --port 5000
```

Health check: `http://localhost:5000/health`

## Run ML Inference

The inference service is also FastAPI. Install `ML/requirements.txt` in a Python environment with compatible PyTorch/torchvision builds, place `best_vit_b16.pth` beside `ML/api.py`, and start it from the `ML` directory:

```powershell
py api.py
```

It listens on port 5001 by default. If model weights are missing, the service still starts, but prediction requests return 503 until the weights are installed.

## Scripts

Run `py -m scripts.create_admin` to create an admin account. Run `py -m scripts.seed_assessments` to seed sample assessments; it skips seeding when assessments already exist unless invoked with `--force`.

The React/Vite frontend continues to use Node.js for development/build tooling. Node.js is no longer used to serve the backend API.