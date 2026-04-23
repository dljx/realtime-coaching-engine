from google.cloud import storage
import json

# Config
BUCKET_NAME = "coaching-knowledge-base"
PROD_STATE_FILE = "system_state/active_cache.json"
TEST_STATE_FILE = "system_state/active_cache_TEST.json"

def sync_state():
    client = storage.Client()
    bucket = client.bucket(BUCKET_NAME)
    
    # 1. Read Prod State
    prod_blob = bucket.blob(PROD_STATE_FILE)
    if not prod_blob.exists():
        print("Error: Production cache state not found. Run the Cloud Function trigger first.")
        return

    data = json.loads(prod_blob.download_as_string())
    print(f"Found Prod Cache: {data.get('name')}")

    # 2. Write to Test State
    test_blob = bucket.blob(TEST_STATE_FILE)
    test_blob.upload_from_string(
        json.dumps(data), 
        content_type="application/json"
    )
    print(f"Success! Copied Prod Cache ID to {TEST_STATE_FILE}")

if __name__ == "__main__":
    sync_state()
