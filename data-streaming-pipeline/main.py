import os
import logging
import json
from flask import Flask, request, jsonify
from google.cloud import bigquery
from google.api_core.exceptions import GoogleAPICallError

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

try:
    client = bigquery.Client()
except Exception as e:
    app.logger.critical(f"FATAL: Could not initialize BigQuery client: {e}")
    client = None

@app.route('/', methods=['POST'])
def stream_to_bq():
    if not client:
        app.logger.error("BigQuery client is not available.")
        return jsonify({"status": "error", "message": "Internal server error"}), 500

    # --- 1. DATA EXTRACTION ---
    # Merge URL parameters and JSON body into one 'payload'
    payload = request.args.to_dict()

    if request.is_json:
        body_data = request.get_json(silent=True)
        if body_data and isinstance(body_data, dict):
            payload.update(body_data)

    if not payload:
        app.logger.warning("Received request with no data.")
        return jsonify({"status": "error", "message": "No data found"}), 400

    # Handle 'workoutData' string parsing if it came from URL
    if 'workoutData' in payload and isinstance(payload['workoutData'], str):
        try:
            payload['workoutData'] = json.loads(payload['workoutData'])
        except Exception as e:
            app.logger.warning(f"Failed to parse workoutData string: {e}")

    # --- 2. FILTER LOGIC (Keep 1 & 201 Rule) ---
    try:
        activity_id = int(payload.get("activityId", -1))
    except (ValueError, TypeError):
        activity_id = -1

    try:
        model_id = int(payload.get("modelId", -1))
    except (ValueError, TypeError):
        model_id = -1

    if activity_id != 1 or model_id != 201:
        app.logger.info(f"Skipping: activityId={activity_id}, modelId={model_id} (requires 1 & 201)")
        return jsonify({"status": "ignored", "message": "Criteria not met"}), 200

    # --- 3. PREPARE ROW WITH ALL COLUMNS ---
    workout_data = payload.get("workoutData")

    if not workout_data or not isinstance(workout_data, dict):
        app.logger.error("Payload missing valid 'workoutData' object.")
        return jsonify({"status": "error", "message": "Invalid payload structure"}), 400

    # Start with the workout stats
    row_to_insert = workout_data.copy()

    # ADD THE METADATA FIELDS
    # We safeguard int conversions for IDs
    try:
        row_to_insert['subType'] = payload.get('subType') # STRING
        row_to_insert['timestamp'] = payload.get('timestamp') # STRING (ISO Format)
        row_to_insert['device'] = payload.get('device') # STRING
        
        # IDs: Convert to int, or use None if missing/invalid
        fac_id = payload.get('facilityId')
        row_to_insert['facilityId'] = int(fac_id) if fac_id is not None else None
        
        row_to_insert['activityId'] = activity_id # We already cast this to int above
        row_to_insert['modelId'] = model_id       # We already cast this to int above

    except Exception as e:
        app.logger.error(f"Error processing metadata fields: {e}")
        return jsonify({"status": "error", "message": "Metadata error"}), 400

    rows_to_insert = [row_to_insert]

    # --- 4. BIGQUERY INSERTION ---
    project_id = os.environ.get("GCP_PROJECT")
    dataset_id = os.environ.get("BQ_DATASET")
    table_id = os.environ.get("BQ_TABLE")

    if not all([project_id, dataset_id, table_id]):
        app.logger.error("Server is missing BQ environment variables.")
        return jsonify({"status": "error", "message": "Internal server error"}), 500

    table_ref = f"{project_id}.{dataset_id}.{table_id}"

    try:
        errors = client.insert_rows_json(table_ref, rows_to_insert)
        if not errors:
            app.logger.info(f"Success: Inserted data (act={activity_id}, mod={model_id})")
            return jsonify({"status": "success"}), 200
        else:
            app.logger.error(f"BQ Errors: {errors}")
            return jsonify({"status": "error", "message": "Insert failed"}), 400

    except GoogleAPICallError as e:
        app.logger.exception(f"API Error: {e}")
        return jsonify({"status": "error", "message": "Server error"}), 500
    except Exception as e:
        app.logger.exception(f"Unexpected Error: {e}")
        return jsonify({"status": "error", "message": "Server error"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
