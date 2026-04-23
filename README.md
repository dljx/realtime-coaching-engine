# Realtime Coaching Engine

A production-grade AI coaching system that analyzes live biomechanical sensor data from workout sessions, scores running form using configurable models, and delivers personalized recommendations via **Gemini 2.5 Flash Lite** with context-cached research papers. Deployed as three Cloud Run microservices.

## Architecture

```
Treadmill Sensor Data
        │
        ▼
┌──────────────────────────┐
│ Data Streaming Pipeline  │──► BigQuery (raw sessions)
└──────────────────────────┘
                                       │
                     ┌─────────────────┘
                     │  (workout complete)
                     ▼
          ┌─────────────────────┐
          │   Coaching Agent    │◄── GCS (knowledge base PDFs)
          │  (Flask / Cloud Run)│◄── Gemini Context Cache (24h TTL)
          │                     │◄── BigQuery (scoring config, segments)
          └────────┬────────────┘
                   │
                   ▼
          Coaching Response (JSON)
          Cadence / Vertical / Horizontal scores
          + literature-backed recommendations

                          GCS event (PDF upload)
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │   Cache Auto-Updater    │  (Cloud Function)
                    │   Refreshes Gemini      │
                    │   context cache (24h)   │
                    └─────────────────────────┘
```

## Features

- **Biomechanical scoring** across three dimensions:
  - **Cadence** — step rate vs. speed (linear threshold model)
  - **Vertical Motion** — ground contact time vs. speed (quadratic model)
  - **Horizontal Motion** — left/right balance symmetry
- **Gemini context caching** — research papers (Heiderscheit 2011, Moore 2019, Snyder 2011, Schulze 2017) are cached in Vertex AI for 24 hours, significantly reducing per-request latency and cost
- **Configurable thresholds** — scoring parameters stored in GCS, hot-reloaded without redeployment
- **User segmentation** — cluster-based advice fetched from BigQuery
- **Multi-language support** — localization config for coaching text
- **API key auth** — key stored in Secret Manager, cached on startup
- **Lazy client initialization** — BQ, GCS, GenAI, SecretManager clients initialized on first use
- **Parallel data fetching** — thread pool executor for concurrent BigQuery queries

## Tech Stack

- Python 3.9, Flask, Gunicorn
- Google Vertex AI — Gemini 2.5 Flash Lite + Context Caching
- Google BigQuery, Cloud Storage, Secret Manager
- Google Cloud Run, Cloud Functions

## Project Structure

```
├── coaching-agent/
│   ├── main.py                # Flask API — scoring, Gemini inference, response formatting
│   ├── config/
│   │   ├── coach_config.json  # Biomechanical scoring thresholds (also loaded from GCS)
│   │   └── localization.json  # Language strings for coaching responses
│   ├── comprehensive_test.py  # Integration test suite
│   ├── run_test_suite.py      # Test runner
│   ├── sync_test_state.py     # Syncs cache state between environments
│   ├── Dockerfile
│   └── requirements.txt
├── data-streaming-pipeline/
│   ├── main.py                # Telemetry ingestion endpoint → BigQuery
│   ├── Dockerfile
│   └── requirements.txt
├── cache-auto-updater/
│   ├── main.py                # GCS-triggered Cloud Function to refresh Gemini context cache
│   └── requirements.txt
├── .env.example
└── LICENSE
```

## Environment Variables

### Coaching Agent

| Variable | Description | Default |
|----------|-------------|---------|
| `GOOGLE_CLOUD_PROJECT` | GCP project ID | `your-gcp-project` |
| `GOOGLE_CLOUD_LOCATION` | GCP region | `us-central1` |
| `ENV_TYPE` | `PROD` or `TEST` | `PROD` |
| `PORT` | Server port | `8080` |

### Data Streaming Pipeline

| Variable | Description |
|----------|-------------|
| `GCP_PROJECT` | GCP project ID |
| `BQ_DATASET` | BigQuery dataset ID |
| `BQ_TABLE` | BigQuery table for raw sessions |

## API Usage

### `POST /analyze_run`

**Headers:**
- `X-Api-Key` — API key stored in Secret Manager (`coaching-api-key`)

**Request:**
```json
{
  "userId": "user123",
  "timestamp": "2025-01-15T10:30:00Z",
  "device": "device_id",
  "facilityId": 1,
  "activityId": 1,
  "modelId": 201,
  "retrievalInterval": 5,
  "languageCode": "en"
}
```

**Response:**
```json
{
  "Cadence": {
    "Score": "Excellent",
    "Recommendation": "Maintain your current cadence.",
    "Source": "Heiderscheit (2011)"
  },
  "Vertical_Motion": {
    "Score": "Fair",
    "Recommendation": "Focus on quicker ground contact.",
    "Source": "Moore (2019)"
  },
  "Horizontal_Motion": {
    "Score": "Excellent",
    "Recommendation": "Good balance symmetry.",
    "Source": "Snyder (2011)"
  }
}
```

## Scoring Model

Thresholds are loaded from `coach_config.json` (GCS or local fallback):

```json
{
  "operational": { "min_speed_mph": 5.0 },
  "cadence": {
    "low":    { "slope": 4.25, "intercept": 123 },
    "median": { "slope": 5.25, "intercept": 132 },
    "high":   { "slope": 6.00, "intercept": 151 }
  },
  "vertical": { ... },
  "horizontal": { ... }
}
```

The cadence model: `threshold = slope × speed + intercept`  
The vertical model uses a quadratic fit against speed.

## Deployment

```bash
# Coaching agent
cd coaching-agent
gcloud run deploy coaching-agent --source . --region us-central1

# Data streaming pipeline
cd ../data-streaming-pipeline
gcloud run deploy data-streaming --source . --region us-central1

# Cache auto-updater (Cloud Function, triggered by GCS)
cd ../cache-auto-updater
gcloud functions deploy cache-auto-updater \
  --runtime python39 \
  --trigger-resource coaching-knowledge-base \
  --trigger-event google.storage.object.finalize
```

## License

MIT
