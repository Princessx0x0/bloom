#!/bin/bash
# Bloom — Production Deploy to Cloud Run
# Run from project root: chmod +x deploy.sh && ./deploy.sh

set -e

# ── Infer project from gcloud context ─────────────────────────────────────────
PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
if [ -z "$PROJECT_ID" ]; then
  echo "❌ No active GCP project. Run: gcloud config set project YOUR_PROJECT_ID"
  exit 1
fi

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
REGION="europe-west2"
SERVICE_NAME="bloom"
REPO="bloom-repo"
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/$SERVICE_NAME"

# Service accounts
BLOOM_SA_NAME="bloom-sa"
BLOOM_SA_EMAIL="$BLOOM_SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
CLOUDBUILD_SA="$PROJECT_NUMBER@cloudbuild.gserviceaccount.com"
COMPUTE_SA="$PROJECT_NUMBER-compute@developer.gserviceaccount.com"

echo "🌿 Deploying Bloom"
echo "   Project: $PROJECT_ID ($PROJECT_NUMBER)"
echo "   Region:  $REGION"
echo "   Image:   $IMAGE"

# ── Enable APIs ────────────────────────────────────────────────────────────────
echo ""
echo "🔧 Enabling APIs..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  aiplatform.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  --quiet

# ── Artifact Registry repo ────────────────────────────────────────────────────
echo ""
echo "📦 Setting up Artifact Registry..."
gcloud artifacts repositories describe $REPO \
  --location=$REGION &>/dev/null || \
  gcloud artifacts repositories create $REPO \
    --repository-format=docker \
    --location=$REGION \
    --description="Bloom container images" \
    --quiet

gcloud auth configure-docker $REGION-docker.pkg.dev --quiet

# ── Bloom SA (runs the Cloud Run service) ─────────────────────────────────────
echo ""
echo "🔐 Configuring Bloom service account..."
gcloud iam service-accounts describe $BLOOM_SA_EMAIL &>/dev/null || \
  gcloud iam service-accounts create $BLOOM_SA_NAME \
    --display-name="Bloom Service Account" \
    --description="Least-privilege SA for Bloom Cloud Run service" \
    --quiet

for ROLE in \
  "roles/aiplatform.user" \
  "roles/datastore.user" \
  "roles/secretmanager.secretAccessor" \
  "roles/logging.logWriter"; do
  gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$BLOOM_SA_EMAIL" \
    --role="$ROLE" \
    --quiet
done

echo "   ✅ bloom-sa configured (aiplatform, firestore, secretmanager, logging)"

# ── Cloud Build SA (builds and pushes the image) ──────────────────────────────
echo ""
echo "🔐 Configuring Cloud Build permissions..."

# Push images to Artifact Registry
gcloud artifacts repositories add-iam-policy-binding $REPO \
  --location=$REGION \
  --member="serviceAccount:$CLOUDBUILD_SA" \
  --role="roles/artifactregistry.writer" \
  --quiet

gcloud artifacts repositories add-iam-policy-binding $REPO \
  --location=$REGION \
  --member="serviceAccount:$COMPUTE_SA" \
  --role="roles/artifactregistry.writer" \
  --quiet

# Deploy to Cloud Run and act as bloom-sa
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$CLOUDBUILD_SA" \
  --role="roles/run.admin" \
  --quiet

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$CLOUDBUILD_SA" \
  --role="roles/iam.serviceAccountUser" \
  --quiet

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$CLOUDBUILD_SA" \
  --role="roles/logging.logWriter" \
  --quiet

echo "   ✅ Cloud Build SA configured (artifactregistry, run.admin, logging)"

# ── Build & push ───────────────────────────────────────────────────────────────
echo ""
echo "🐳 Building container image..."
gcloud builds submit --tag $IMAGE --quiet

# ── Deploy to Cloud Run using bloom-sa ────────────────────────────────────────
echo ""
echo "🚀 Deploying to Cloud Run (running as bloom-sa)..."
gcloud run deploy $SERVICE_NAME \
  --image $IMAGE \
  --platform managed \
  --region $REGION \
  --allow-unauthenticated \
  --service-account $BLOOM_SA_EMAIL \
  --set-env-vars GCP_PROJECT_ID=$PROJECT_ID,GCP_REGION=us-central1 \
  --memory 1Gi \
  --cpu 1 \
  --concurrency 10 \
  --min-instances 0 \
  --max-instances 5 \
  --port 8080 \
  --quiet

# ── Done ───────────────────────────────────────────────────────────────────────
URL=$(gcloud run services describe $SERVICE_NAME --region $REGION --format 'value(status.url)')
echo ""
echo "✅ Bloom is live!"
echo "   URL: $URL"
echo ""
echo "📋 Post-deploy:"
echo "   1. Visit $URL and test with a plant photo"
echo "   2. Update CORS in main.py: allow_origins=[\"$URL\"]"
echo "   3. git add . && git commit -m 'fix: tighten CORS' && git push && ./deploy.sh"
