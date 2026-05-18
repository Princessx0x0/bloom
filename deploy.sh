#!/bin/bash
# Bloom — Production Deploy to Cloud Run
# Run from project root: chmod +x deploy.sh && ./deploy.sh

set -e

PROJECT_ID="bloom-496623"
REGION="europe-west2"        # London
SERVICE_NAME="bloom"
REPO="bloom-repo"
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/$SERVICE_NAME"
SA_NAME="bloom-sa"
SA_EMAIL="$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"

echo "🌿 Deploying Bloom to Cloud Run..."
echo "   Project: $PROJECT_ID"
echo "   Region:  $REGION"

# ── Set project ────────────────────────────────────────────────────────────────
gcloud config set project $PROJECT_ID

# ── Enable APIs ────────────────────────────────────────────────────────────────
echo "Enabling APIs..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  aiplatform.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com

# ── Service Account (least privilege) ─────────────────────────────────────────
echo "Setting up service account..."

# Create SA if it doesn't exist
gcloud iam service-accounts describe $SA_EMAIL &>/dev/null || \
  gcloud iam service-accounts create $SA_NAME \
    --display-name="Bloom Service Account" \
    --description="Least-privilege SA for Bloom Cloud Run service"

# Grant only what Bloom needs
for ROLE in \
  "roles/aiplatform.user" \
  "roles/datastore.user" \
  "roles/secretmanager.secretAccessor"; do
  gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$SA_EMAIL" \
    --role="$ROLE" \
    --quiet
done

echo "Service account configured: $SA_EMAIL"

# ── Build & push image ─────────────────────────────────────────────────────────
echo "Building container image..."
gcloud builds submit --tag $IMAGE --quiet

# ── Deploy to Cloud Run ────────────────────────────────────────────────────────
echo "Deploying to Cloud Run..."
gcloud run deploy $SERVICE_NAME \
  --image $IMAGE \
  --platform managed \
  --region $REGION \
  --allow-unauthenticated \
  --service-account $SA_EMAIL \
  --set-env-vars GCP_PROJECT_ID=$PROJECT_ID,GCP_REGION=us-central1 \
  --memory 1Gi \
  --cpu 1 \
  --concurrency 10 \
  --min-instances 0 \
  --max-instances 5 \
  --port 8080 \
  --quiet

# ── Print URL ──────────────────────────────────────────────────────────────────
URL=$(gcloud run services describe $SERVICE_NAME --region $REGION --format 'value(status.url)')
echo ""
echo "Bloom is live!"
echo "   URL: $URL"
echo ""
echo "Next steps:"
echo "   1. Visit $URL to test"
echo "   2. Update CORS origin in main.py to: $URL"
echo "   3. Redeploy after CORS update"
