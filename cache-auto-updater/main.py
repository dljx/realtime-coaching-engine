import os
import json
import time
import datetime
import logging
import functions_framework
from google.cloud import storage
from google import genai
from google.genai import types

# --- Configuration ---
PROJECT_ID = os.environ.get("GCP_PROJECT", "your-gcp-project")
LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
BUCKET_NAME = os.environ.get("BUCKET_NAME", "coaching-knowledge-base")
MODEL_ID = "gemini-2.5-flash-lite"

# Paths
DOCS_PREFIX = "coaching_docs/"
HEARTBEAT_FILE = "system_state/refresh_trigger.txt"

# State Files to Update (PROD and TEST)
STATE_FILES = [
    "system_state/active_cache.json",      # PROD
    "system_state/active_cache_TEST.json"  # TEST
]

storage_client = storage.Client()
genai_client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)

@functions_framework.cloud_event
def update_knowledge_base(cloud_event):
    data = cloud_event.data
    file_name = data.get("name")

    is_pdf_update = file_name.startswith(DOCS_PREFIX) and file_name.endswith(".pdf")
    is_heartbeat = file_name == HEARTBEAT_FILE

    if not (is_pdf_update or is_heartbeat):
        print(f"Skipping update. File '{file_name}' irrelevant.")
        return

    print(f"Cache Refresh Triggered by: {file_name}")

    try:
        bucket = storage_client.bucket(BUCKET_NAME)
        blobs = bucket.list_blobs(prefix=DOCS_PREFIX)
        
        content_parts = []
        doc_count = 0
        for blob in blobs:
            if blob.name.endswith(".pdf"):
                print(f"Loading: {blob.name}")
                file_bytes = blob.download_as_bytes()
                doc_id = os.path.basename(blob.name).replace(".pdf", "").replace(" ", "_")
                content_parts.append(types.Part.from_text(text=f"--- START DOCUMENT ID: {doc_id} ---"))
                content_parts.append(types.Part.from_bytes(data=file_bytes, mime_type="application/pdf"))
                content_parts.append(types.Part.from_text(text=f"--- END DOCUMENT ID: {doc_id} ---"))
                doc_count += 1

        if doc_count == 0:
            print("Error: No PDFs found.")
            return

        system_instruction = """
        You are an expert Running Biomechanics Coach.
        KNOWLEDGE HIERARCHY: Heiderscheit (2011), Moore (2019), Snyder (2011), Schulze (2017).
        INSTRUCTIONS: Use scores provided. Cite sources. Keep advice concise.
        OUTPUT FORMAT: Return valid JSON ONLY.
        """

        cache_config = {
            "contents": [types.Content(role="user", parts=content_parts)],
            "system_instruction": types.Content(parts=[types.Part.from_text(text=system_instruction)]),
            "ttl": "86400s", 
            "display_name": f"coaching_cache_AUTO_{int(time.time())}"
        }

        print("Uploading to Vertex AI...")
        new_cache = genai_client.caches.create(model=MODEL_ID, config=cache_config)
        print(f"SUCCESS: New Cache Created: {new_cache.name}")

        # --- UPDATE ALL STATE FILES (PROD & TEST) ---
        old_cache_name = None
        new_state = {
            "name": new_cache.name,
            "expiry": new_cache.expire_time.timestamp(),
            "updated_at": str(datetime.datetime.utcnow())
        }
        
        # We try to find the old cache ID from the PROD file to delete it later
        prod_blob = bucket.blob(STATE_FILES[0]) 
        if prod_blob.exists():
            try:
                old_data = json.loads(prod_blob.download_as_string())
                old_cache_name = old_data.get("name")
            except: pass

        # Write to both PROD and TEST paths
        for path in STATE_FILES:
            blob = bucket.blob(path)
            blob.upload_from_string(json.dumps(new_state), content_type="application/json")
            print(f"Updated state file: {path}")

        # Delete Old Cache
        if old_cache_name and old_cache_name != new_cache.name:
            print(f"Deleting old cache: {old_cache_name}...")
            try:
                genai_client.caches.delete(name=old_cache_name)
            except Exception as e:
                print(f"Warning: Failed to delete old cache: {e}")

    except Exception as e:
        print(f"CRITICAL ERROR: {e}")
        raise e
