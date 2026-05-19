import google.generativeai as genai
from google.cloud import secretmanager, firestore
from PIL import Image
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi import FastAPI, File, UploadFile, Request, HTTPException
from contextlib import asynccontextmanager
from collections import defaultdict
from datetime import datetime, timezone
import logging
import io
import uuid
import time
import hashlib
import json
import base64
import os


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bloom")

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "bloom-496623")
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic"}
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024
MAX_REQUESTS_PER_MINUTE = 10
MAX_REQUESTS_PER_DAY = 100

rate_store: dict[str, list[float]] = defaultdict(list)


def is_rate_limited(ip: str) -> bool:
    now = time.time()
    minute_ago = now - 60
    day_ago = now - 86400
    rate_store[ip] = [t for t in rate_store[ip] if t > day_ago]
    per_minute = sum(1 for t in rate_store[ip] if t > minute_ago)
    per_day = len(rate_store[ip])
    if per_minute >= MAX_REQUESTS_PER_MINUTE or per_day >= MAX_REQUESTS_PER_DAY:
        return True
    rate_store[ip].append(now)
    return False


def get_secret(secret_id: str) -> str:
    try:
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
        resp = client.access_secret_version(request={"name": name})
        return resp.payload.data.decode("UTF-8").strip()
    except Exception as e:
        logger.warning(f"Secret Manager failed for {secret_id}: {e}")
        return os.environ.get(secret_id.upper().replace("-", "_"), "")


db: firestore.Client | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    logger.info("🌿 Bloom starting up...")
    api_key = get_secret("gemini-api-key")
    if api_key:
        genai.configure(api_key=api_key)
        logger.info("✅ Gemini API configured from Secret Manager")
    else:
        logger.error("❌ No Gemini API key found")
    try:
        db = firestore.Client(project=PROJECT_ID)
        logger.info("✅ Firestore connected")
    except Exception as e:
        logger.warning(f"⚠️  Firestore unavailable: {e}")
        db = None
    yield
    logger.info("🌿 Bloom shutting down")

app = FastAPI(title="Bloom", docs_url=None, redoc_url=None, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

templates = Jinja2Templates(directory="templates")


def validate_image(file: UploadFile, image_bytes: bytes) -> None:
    if file.content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported file type.")
    if len(image_bytes) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=413, detail="Image too large. Maximum size is 10MB.")
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.verify()
    except Exception:
        raise HTTPException(
            status_code=422, detail="File does not appear to be a valid image.")


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


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
                "id":         doc.id,
                "plant_name": data.get("plant_name"),
                "health":     data.get("health_status"),
                "diagnosis":  data.get("diagnosis"),
                "created_at": data.get("created_at").isoformat() if data.get("created_at") else None,
            })
        return JSONResponse({"scans": scans})
    except Exception as e:
        logger.error(f"Firestore read error: {e}")
        return JSONResponse({"scans": []})


@app.get("/scan/{scan_id}")
async def get_scan(scan_id: str):
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    try:
        doc = db.collection("scans").document(scan_id).get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Scan not found")
        data = doc.to_dict()
        return JSONResponse({
            "success": True,
            "scan_id": scan_id,
            "diagnosis": {
                "plant_name":      data.get("plant_name"),
                "scientific_name": data.get("scientific_name"),
                "health_status":   data.get("health_status"),
                "diagnosis":       data.get("diagnosis"),
                "symptoms":        data.get("symptoms", []),
                "treatment":       data.get("treatment", []),
                "recovery_time":   data.get("recovery_time"),
                "care_tip":        data.get("care_tip", ""),
            },
            "uploaded_image": None,
            "healthy_image":  None,
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Firestore fetch error: {e}")
        raise HTTPException(status_code=500, detail="Could not retrieve scan")


@app.post("/diagnose")
async def diagnose_plant(request: Request, file: UploadFile = File(...)):
    ip = get_client_ip(request)

    if is_rate_limited(ip):
        raise HTTPException(
            status_code=429, detail="Too many requests. Please wait a moment.")

    image_bytes = await file.read()
    validate_image(file, image_bytes)

    pil_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    buffer = io.BytesIO()
    pil_image.save(buffer, format="JPEG", quality=85)
    clean_bytes = buffer.getvalue()
    img_b64 = base64.b64encode(clean_bytes).decode()

    scan_id = str(uuid.uuid4())
    img_fingerprint = hashlib.sha256(clean_bytes).hexdigest()[:16]
    logger.info(f"[{scan_id}] Diagnosing — fingerprint={img_fingerprint}")

    try:
        model = genai.GenerativeModel(
            "gemini-2.5-flash",
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
    "healthy_prompt": "Detailed prompt: lush healthy [species], vibrant foliage, studio lighting, botanical illustration style, white background"
}"""

        image_part = {"mime_type": "image/jpeg", "data": clean_bytes}
        response = model.generate_content([prompt, image_part])

        raw = response.text.strip()
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()

        diagnosis = json.loads(raw)
        logger.info(
            f"[{scan_id}] Diagnosed: {diagnosis.get('plant_name')} — {diagnosis.get('health_status')}")

    except json.JSONDecodeError:
        logger.error(f"[{scan_id}] JSON parse failed")
        raise HTTPException(
            status_code=500, detail="Could not parse plant diagnosis. Please try again.")
    except Exception as e:
        logger.error(f"[{scan_id}] Gemini error: {e}")
        raise HTTPException(
            status_code=500, detail="Diagnosis service unavailable. Please try again.")

    healthy_b64 = None
    try:
        imagen_model = genai.GenerativeModel("gemini-2.0-flash-exp")
        healthy_prompt = diagnosis.get(
            "healthy_prompt",
            f"A perfectly healthy {diagnosis['plant_name']}, lush vibrant foliage, botanical illustration, studio lighting, white background"
        )
        imagen_response = imagen_model.generate_content(
            [f"Generate a photorealistic image of: {healthy_prompt}"],
            generation_config={"response_mime_type": "image/jpeg"}
        )
        if imagen_response.candidates:
            for part in imagen_response.candidates[0].content.parts:
                if hasattr(part, 'inline_data') and part.inline_data:
                    healthy_b64 = base64.b64encode(
                        part.inline_data.data).decode()
                    break
        logger.info(f"[{scan_id}] Imagen generated successfully")
    except Exception as e:
        logger.warning(f"[{scan_id}] Imagen error (non-fatal): {e}")

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
                "care_tip":        diagnosis.get("care_tip", ""),
                "created_at":      datetime.now(timezone.utc),
            })
            logger.info(f"[{scan_id}] Saved to Firestore")

        except Exception as e:
            logger.warning(
                f"[{scan_id}] Firestore write failed (non-fatal): {e}"
            )

    return JSONResponse({
        "success":        True,
        "scan_id":        scan_id,
        "uploaded_image": img_b64,
        "diagnosis":      diagnosis,
        "healthy_image":  healthy_b64,
    })
