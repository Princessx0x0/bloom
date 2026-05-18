import os
import base64
import json
import hashlib
import time
import uuid
import io
import logging
from datetime import datetime, timezone
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, UploadFile, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image

# Google Cloud
from google.cloud import firestore
import vertexai
from vertexai.generative_models import GenerativeModel, Part
from vertexai.preview.vision_models import ImageGenerationModel

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bloom")

# ── Config ─────────────────────────────────────────────────────────────────────
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "bloom-496623")
REGION     = os.environ.get("GCP_REGION", "us-central1")

ALLOWED_MIME_TYPES      = {"image/jpeg", "image/png", "image/webp", "image/heic"}
MAX_FILE_SIZE_BYTES     = 10 * 1024 * 1024  # 10MB
MAX_REQUESTS_PER_MINUTE = 10
MAX_REQUESTS_PER_DAY    = 100

# ── In-memory rate limiter (per IP) ───────────────────────────────────────────
rate_store: dict[str, list[float]] = defaultdict(list)

def is_rate_limited(ip: str) -> bool:
    now        = time.time()
    minute_ago = now - 60
    day_ago    = now - 86400

    rate_store[ip] = [t for t in rate_store[ip] if t > day_ago]

    per_minute = sum(1 for t in rate_store[ip] if t > minute_ago)
    per_day    = len(rate_store[ip])

    if per_minute >= MAX_REQUESTS_PER_MINUTE or per_day >= MAX_REQUESTS_PER_DAY:
        return True

    rate_store[ip].append(now)
    return False

# ── Firestore client ───────────────────────────────────────────────────────────
db: firestore.Client | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    logger.info("🌿 Bloom starting up...")

    vertexai.init(project=PROJECT_ID, location=REGION)
    logger.info(f"✅ Vertex AI initialised — project={PROJECT_ID} region={REGION}")

    try:
        db = firestore.Client(project=PROJECT_ID)
        logger.info("✅ Firestore connected")
    except Exception as e:
        logger.warning(f"⚠️  Firestore unavailable: {e}")
        db = None

    yield
    logger.info("🌿 Bloom shutting down")

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Bloom", docs_url=None, redoc_url=None, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to your Cloud Run URL after deploy
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

templates = Jinja2Templates(directory="templates")

# ── Helpers ────────────────────────────────────────────────────────────────────
def validate_image(file: UploadFile, image_bytes: bytes) -> None:
    if file.content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type. Please upload a JPEG, PNG, or WebP image."
        )
    if len(image_bytes) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="Image too large. Maximum size is 10MB.")
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.verify()
    except Exception:
        raise HTTPException(status_code=422, detail="File does not appear to be a valid image.")

def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/health")
async def health():
    return {"status": "ok", "service": "bloom"}

@app.get("/history")
async def get_history():
    if db is None:
        return JSONResponse({"scans": []})
    try:
        docs = (
            db.collection("scans")
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(10)
            .stream()
        )
        scans = []
        for doc in docs:
            data = doc.to_dict()
            scans.append({
                "id":          doc.id,
                "plant_name":  data.get("plant_name"),
                "health":      data.get("health_status"),
                "diagnosis":   data.get("diagnosis"),
                "created_at":  data.get("created_at").isoformat() if data.get("created_at") else None,
            })
        return JSONResponse({"scans": scans})
    except Exception as e:
        logger.error(f"Firestore read error: {e}")
        return JSONResponse({"scans": []})

@app.post("/diagnose")
async def diagnose_plant(request: Request, file: UploadFile = File(...)):
    ip = get_client_ip(request)

    # Rate limit
    if is_rate_limited(ip):
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait a moment before trying again."
        )

    # Read & validate
    image_bytes = await file.read()
    validate_image(file, image_bytes)

    # Normalise to JPEG
    pil_image   = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    buffer      = io.BytesIO()
    pil_image.save(buffer, format="JPEG", quality=85)
    clean_bytes = buffer.getvalue()
    img_b64     = base64.b64encode(clean_bytes).decode()

    scan_id         = str(uuid.uuid4())
    img_fingerprint = hashlib.sha256(clean_bytes).hexdigest()[:16]
    logger.info(f"[{scan_id}] Diagnosing — fingerprint={img_fingerprint} ip_hash={hashlib.sha256(ip.encode()).hexdigest()[:8]}")

    # Gemini Vision via Vertex AI
    try:
        model = GenerativeModel(
            "gemini-1.5-pro",
            system_instruction=(
                "You are Bloom, a warm and knowledgeable plant doctor. "
                "You diagnose plants with care and always encourage the owner. "
                "You respond ONLY with valid JSON — no preamble, no markdown fences."
            )
        )

        prompt = """Analyse this plant image and respond with ONLY a valid JSON object:
{
    "plant_name": "Common name of the plant",
    "scientific_name": "Scientific name",
    "health_status": "healthy | struggling | critical",
    "diagnosis": "2-3 warm clear sentences describing what is wrong",
    "symptoms": ["symptom 1", "symptom 2", "symptom 3"],
    "treatment": ["Actionable step 1", "Actionable step 2", "Actionable step 3"],
    "recovery_time": "Realistic timeframe with consistent care",
    "care_tip": "One encouraging sentence about this plant species personality",
    "healthy_prompt": "Detailed Imagen prompt: lush healthy [species], vibrant foliage, studio lighting, botanical illustration style, white background"
}"""

        image_part = Part.from_data(data=clean_bytes, mime_type="image/jpeg")
        response   = model.generate_content([prompt, image_part])

        raw = response.text.strip()
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()

        diagnosis = json.loads(raw)

    except json.JSONDecodeError:
        logger.error(f"[{scan_id}] JSON parse failed")
        raise HTTPException(status_code=500, detail="Could not parse plant diagnosis. Please try again.")
    except Exception as e:
        logger.error(f"[{scan_id}] Vertex AI error: {e}")
        raise HTTPException(status_code=500, detail="Diagnosis service unavailable. Please try again.")

    # Imagen — healthy plant
    healthy_b64 = None
    try:
        imagen = ImageGenerationModel.from_pretrained("imagegeneration@006")
        healthy_prompt = diagnosis.get(
            "healthy_prompt",
            f"A perfectly healthy {diagnosis['plant_name']}, lush vibrant foliage, botanical illustration, studio lighting, white background"
        )
        img_response = imagen.generate_images(
            prompt=healthy_prompt,
            number_of_images=1,
            aspect_ratio="1:1",
            safety_filter_level="block_few",
        )
        if img_response.images:
            healthy_b64 = base64.b64encode(img_response.images[0]._image_bytes).decode()
            logger.info(f"[{scan_id}] Imagen generated successfully")
    except Exception as e:
        logger.warning(f"[{scan_id}] Imagen error (non-fatal): {e}")

    # Firestore — persist scan
    if db is not None:
        try:
            db.collection("scans").document(scan_id).set({
                "scan_id":         scan_id,
                "ip_hash":         hashlib.sha256(ip.encode()).hexdigest()[:12],
                "img_fingerprint": img_fingerprint,
                "plant_name":      diagnosis.get("plant_name"),
                "scientific_name": diagnosis.get("scientific_name"),
                "health_status":   diagnosis.get("health_status"),
                "diagnosis":       diagnosis.get("diagnosis"),
                "symptoms":        diagnosis.get("symptoms", []),
                "treatment":       diagnosis.get("treatment", []),
                "recovery_time":   diagnosis.get("recovery_time"),
                "created_at":      datetime.now(timezone.utc),
            })
            logger.info(f"[{scan_id}] Saved to Firestore")
        except Exception as e:
            logger.warning(f"[{scan_id}] Firestore write failed (non-fatal): {e}")

    return JSONResponse({
        "success":        True,
        "scan_id":        scan_id,
        "uploaded_image": img_b64,
        "diagnosis":      diagnosis,
        "healthy_image":  healthy_b64,
    })
