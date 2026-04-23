import os
import json
import time
import logging
import datetime
import threading
import concurrent.futures
from functools import lru_cache
from flask import Flask, request, jsonify
from google.cloud import bigquery
from google.cloud import storage
from google.cloud import secretmanager
from google import genai
from google.genai import types
from google.api_core.exceptions import InvalidArgument, NotFound

app = Flask(__name__)
app.json.ensure_ascii = False 

# ==============================================================================
# CONFIGURATION & ENVIRONMENT SWITCHING
# ==============================================================================
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "your-gcp-project")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
BUCKET_NAME = "coaching-knowledge-base"
PREFIX = "coaching_docs/" 
MODEL_ID = "gemini-2.5-flash-lite"

ENV_TYPE = os.environ.get("ENV_TYPE", "PROD").upper()

if ENV_TYPE == "TEST":
    logger_prefix = "[TEST ENV]"
    DATASET_ID = "your-gcp-project.test_fitness_platform"
    SEGMENT_DATASET_ID = "your-gcp-project.test_segmentation"
    CACHE_STATE_FILE = "system_state/active_cache_TEST.json"
    CACHE_DISPLAY_NAME = "coaching_cache_TEST"
else:
    logger_prefix = "[PROD ENV]"
    DATASET_ID = "your-gcp-project.fitness_platform_data_1"
    SEGMENT_DATASET_ID = "your-gcp-project.segmentation"
    CACHE_STATE_FILE = "system_state/active_cache.json"
    CACHE_DISPLAY_NAME = "coaching_cache_PROD"

SCORING_CONFIG_KEY = "config/coach_config.json"
LOCALIZATION_CONFIG_KEY = "config/localization.json"
API_KEY_SECRET_ID = "coaching-api-key"

FETCH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=16)
LOG_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4)

GLOBAL_CACHE_NAME = None
CACHE_EXPIRY = None
SCORING_CONFIG = None
SCORING_CONFIG_EXPIRY = 0
STATIC_API_KEY = None
STATIC_LOCALIZATION = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.info(f"{logger_prefix} Starting up. Using Dataset: {DATASET_ID}")

# ==============================================================================
# 0. CLIENT LAZY LOADING
# ==============================================================================
_BQ_CLIENT = None
_STORAGE_CLIENT = None
_GENAI_CLIENT = None
_SECRET_CLIENT = None

def get_bq_client():
    global _BQ_CLIENT
    if _BQ_CLIENT is None: _BQ_CLIENT = bigquery.Client(project=PROJECT_ID)
    return _BQ_CLIENT

def get_storage_client():
    global _STORAGE_CLIENT
    if _STORAGE_CLIENT is None: _STORAGE_CLIENT = storage.Client(project=PROJECT_ID)
    return _STORAGE_CLIENT

def get_genai_client():
    global _GENAI_CLIENT
    if _GENAI_CLIENT is None:
        _GENAI_CLIENT = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION, http_options={'api_version': 'v1beta1'})
    return _GENAI_CLIENT

def get_secret_client():
    global _SECRET_CLIENT
    if _SECRET_CLIENT is None: _SECRET_CLIENT = secretmanager.SecretManagerServiceClient()
    return _SECRET_CLIENT

# ==============================================================================
# 1. BOOT-TIME LOADING & SECURITY
# ==============================================================================
def get_api_key():
    if STATIC_API_KEY: return STATIC_API_KEY
    try:
        client = get_secret_client()
        name = f"projects/{PROJECT_ID}/secrets/{API_KEY_SECRET_ID}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except: return None

def verify_auth(request):
    valid_key = get_api_key()
    if not valid_key: return False
    return request.headers.get("X-Api-Key") == valid_key

def load_static_assets():
    global STATIC_API_KEY, STATIC_LOCALIZATION
    try:
        STATIC_API_KEY = get_api_key()
        client = get_storage_client()
        bucket = client.bucket(BUCKET_NAME)
        blob = bucket.blob(LOCALIZATION_CONFIG_KEY)
        if blob.exists():
            STATIC_LOCALIZATION = json.loads(blob.download_as_string())
    except Exception as e:
        logger.warning(f"Boot load warning: {e}")

try: load_static_assets()
except: pass

# ==============================================================================
# 2. CONFIG LOADERS
# ==============================================================================
def get_gcs_json(file_key):
    try:
        client = get_storage_client()
        bucket = client.bucket(BUCKET_NAME)
        blob = bucket.blob(file_key)
        if blob.exists(): return json.loads(blob.download_as_string())
    except: pass
    return None

def get_scoring_config():
    global SCORING_CONFIG, SCORING_CONFIG_EXPIRY
    if SCORING_CONFIG and time.time() < SCORING_CONFIG_EXPIRY: return SCORING_CONFIG
    
    data = get_gcs_json(SCORING_CONFIG_KEY)
    
    # --- DEFAULTS UPDATED FOR NEW RULES ---
    defaults = {
        "operational": {"min_speed_mph": 5.0}, 
        "gen_ai": {"max_recommendation_words": 25},
        "cadence": {
            "low": {"slope": 4.25, "intercept": 123}, 
            "median": {"slope": 5.25, "intercept": 132}, 
            "high": {"slope": 6.00, "intercept": 151}
        },
        "vertical": {
            "low_limit": {"a": 0.92, "b": -30.8, "c": 395}, 
            "median_limit": {"a": 1.07, "b": -36.0, "c": 468},
            "high_limit": {"a": 1.23, "b": -41.2, "c": 540}
        },
        "horizontal": {
            "excellent_threshold": 1.0, 
            "fair_threshold": 2.0
        }
    }

    if data:
        if "vertical" not in data: data["vertical"] = defaults["vertical"]
        if "median_limit" not in data["vertical"]: 
            data["vertical"]["median_limit"] = defaults["vertical"]["median_limit"]
        if "high" not in data["cadence"]:
            data["cadence"]["high"] = defaults["cadence"]["high"]
        if "fair_threshold" not in data["horizontal"]:
             data["horizontal"]["fair_threshold"] = defaults["horizontal"]["fair_threshold"]
        
        SCORING_CONFIG = data
        SCORING_CONFIG_EXPIRY = time.time() + 600
        return data
        
    return defaults

def get_localization_labels(lang_code):
    default = { 
        "Excellent": "Excellent", 
        "Fair": "Fair", 
        "Needs Improvement": "Needs Improvement", 
        "IdleMessage": "Increase your speed to unlock your scores and engage real-time form coaching." 
    }
    if not STATIC_LOCALIZATION: return default
    return STATIC_LOCALIZATION.get(lang_code, default)

# ==============================================================================
# 3. KNOWLEDGE BASE
# ==============================================================================
def get_shared_cache_state():
    try:
        bucket = get_storage_client().bucket(BUCKET_NAME)
        blob = bucket.blob(CACHE_STATE_FILE)
        if not blob.exists(): return None, None
        data = json.loads(blob.download_as_string())
        return data.get("name"), data.get("expiry")
    except: return None, None

def load_documents_from_gcs():
    client = get_storage_client()
    blobs = client.list_blobs(BUCKET_NAME, prefix=PREFIX)
    documents = []
    for blob in blobs:
        if blob.name.endswith(".pdf"):
            file_bytes = blob.download_as_bytes()
            doc_id = os.path.basename(blob.name).replace(".pdf", "").replace(" ", "_")
            documents.append({"id": doc_id, "bytes": file_bytes, "mime_type": "application/pdf"})
    return documents

def get_or_create_cache(ignore_shared=False):
    global GLOBAL_CACHE_NAME, CACHE_EXPIRY
    current_time = time.time()
    
    if not ignore_shared and GLOBAL_CACHE_NAME and CACHE_EXPIRY and (CACHE_EXPIRY - current_time > 300):
        return GLOBAL_CACHE_NAME

    if not ignore_shared:
        shared_name, shared_expiry = get_shared_cache_state()
        if shared_name and shared_expiry and (shared_expiry - current_time > 300):
            if shared_name != GLOBAL_CACHE_NAME: logger.info(f"Switching to shared cache: {shared_name}")
            GLOBAL_CACHE_NAME = shared_name
            CACHE_EXPIRY = shared_expiry
            return GLOBAL_CACHE_NAME

    logger.info("Creating fresh cache (Fallback)...")
    try:
        pdf_docs = load_documents_from_gcs()
        content_parts = []
        for doc in pdf_docs:
            content_parts.append(types.Part.from_text(text=f"--- START DOCUMENT ID: {doc['id']} ---"))
            content_parts.append(types.Part.from_bytes(data=doc['bytes'], mime_type=doc['mime_type']))
            content_parts.append(types.Part.from_text(text=f"--- END DOCUMENT ID: {doc['id']} ---"))

        system_instruction = """
        You are an expert Running Biomechanics Coach.
        HIERARCHY: Heiderscheit (2011), Moore (2019), Snyder (2011), Schulze (2017).
        INSTRUCTIONS: Use scores provided. Cite sources. Keep advice concise.
        """
        
        cache_config = {
            "contents": [types.Content(role="user", parts=content_parts)],
            "system_instruction": types.Content(parts=[types.Part.from_text(text=system_instruction)]),
            "ttl": "3600s",
            "display_name": CACHE_DISPLAY_NAME
        }

        client = get_genai_client()
        cached_content = client.caches.create(model=MODEL_ID, config=cache_config)
        GLOBAL_CACHE_NAME = cached_content.name
        CACHE_EXPIRY = time.time() + 3600
        logger.info(f"New cache created: {GLOBAL_CACHE_NAME}")
        return GLOBAL_CACHE_NAME
    except Exception as e:
        logger.error(f"Failed to create cache: {e}")
        GLOBAL_CACHE_NAME = None
        raise e

# ==============================================================================
# 4. BUSINESS LOGIC (With Context for LLM)
# ==============================================================================
def validate_sensor_data(metrics, labels):
    if not metrics: return False, {"status": "error", "message": "No data found."}
    
    config = get_scoring_config()
    min_speed = config.get("operational", {}).get("min_speed_mph", 5.0)

    if metrics['speed'] is None or metrics['speed'] < min_speed:
        return False, {"status": "idle", "exceptionCode": "00", "message": labels.get("IdleMessage", "Increase speed.")}
    
    missing = []
    if not metrics['cadence']: missing.append("Cadence")
    if not metrics['gct']: missing.append("Ground Contact")
    if missing: return False, {"status": "sensor_error", "message": f"Sensors missing: {', '.join(missing)}."}
    return True, None

def calculate_scores(speed_input, cadence, gct, balance, labels, config):
    if not speed_input or not cadence: return None, 0
    
    speed_mph = float(speed_input) 
    
    # We now store both the Label (for JSON) and Context (for LLM)
    scores = {
        "cadence": {"label": "N/A", "context": "N/A"},
        "vertical": {"label": "N/A", "context": "N/A"},
        "horizontal": {"label": "N/A", "context": "N/A"}
    }
    
    # --- CADENCE ---
    c = config['cadence']
    t_low = (c['low']['slope'] * speed_mph) + c['low']['intercept']
    t_med = (c['median']['slope'] * speed_mph) + c['median']['intercept']
    t_high = (c['high']['slope'] * speed_mph) + c['high']['intercept']
    
    if cadence < t_low:
        scores["cadence"]["label"] = labels["Needs Improvement"]
        scores["cadence"]["context"] = "Needs Improvement (Cadence is too low)"
    elif t_med <= cadence <= t_high:
        scores["cadence"]["label"] = labels["Excellent"]
        scores["cadence"]["context"] = "Excellent (Optimal range)"
    elif cadence > t_high:
        scores["cadence"]["label"] = labels["Fair"]
        scores["cadence"]["context"] = "Fair (Cadence is too high - advise lengthening stride or relaxing)"
    else:
        scores["cadence"]["label"] = labels["Fair"]
        scores["cadence"]["context"] = "Fair (Cadence is slightly low)"

    # --- VERTICAL (GCT) ---
    if gct:
        v = config['vertical']
        v_low_cfg = v.get('low_limit', v.get('excellent_limit'))
        v_high_cfg = v.get('high_limit', v.get('poor_limit'))
        v_med_cfg = v.get('median_limit', {"a": 1.07, "b": -36.0, "c": 468})

        l_low = (v_low_cfg['a']*speed_mph**2) + (v_low_cfg['b']*speed_mph) + v_low_cfg['c']
        l_med = (v_med_cfg['a']*speed_mph**2) + (v_med_cfg['b']*speed_mph) + v_med_cfg['c']
        l_high = (v_high_cfg['a']*speed_mph**2) + (v_high_cfg['b']*speed_mph) + v_high_cfg['c']
        
        if l_low <= gct <= l_med:
            scores["vertical"]["label"] = labels["Excellent"]
            scores["vertical"]["context"] = "Excellent (Sweet spot)"
        elif gct < l_low:
            scores["vertical"]["label"] = labels["Fair"]
            scores["vertical"]["context"] = "Fair (Ground Contact Time is too short/fast - potential stiffness)"
        elif l_med < gct <= l_high:
            scores["vertical"]["label"] = labels["Fair"]
            scores["vertical"]["context"] = "Fair (Ground Contact Time is slightly slow)"
        else:
            scores["vertical"]["label"] = labels["Needs Improvement"]
            scores["vertical"]["context"] = "Needs Improvement (Ground Contact Time is too slow)"

    # --- HORIZONTAL (BALANCE) ---
    if balance:
        h = config['horizontal']
        dev = abs(50.0 - balance)
        fair_thresh = h.get('fair_threshold', 2.0)
        exc_thresh = h.get('excellent_threshold', 1.0)
        
        if dev < exc_thresh:
            scores["horizontal"]["label"] = labels["Excellent"]
            scores["horizontal"]["context"] = "Excellent (Symmetry)"
        elif dev <= fair_thresh:
            scores["horizontal"]["label"] = labels["Fair"]
            scores["horizontal"]["context"] = "Fair (Slight Asymmetry)"
        else:
            scores["horizontal"]["label"] = labels["Needs Improvement"]
            scores["horizontal"]["context"] = "Needs Improvement (Significant Asymmetry)"
        
    return scores, speed_mph

# ==============================================================================
# 5. DATA LAYER
# ==============================================================================
def fetch_metrics(userid, timestamp, device_id, facility_id, activity_id, interval):
    try: safe_interval = int(interval)
    except: safe_interval = 5
    query = f"""
    DECLARE target_time TIMESTAMP DEFAULT TIMESTAMP('{timestamp}');
    SELECT AVG(NULLIF(workout_currentSpeed, 0)) as s, AVG(NULLIF(workout_cadence, 0)) as c,
           AVG(NULLIF(workout_groundContact, 0)) as g, AVG(NULLIF(workout_airTime, 0)) as a,
           AVG(NULLIF(workout_stride, 0)) as str, AVG(workout_currentIncline) as inc,
           AVG(NULLIF(workout_leftRightBalance, 0)) as bal
    FROM `{DATASET_ID}.data_stream_raw_full`
    WHERE userId = '{userid}' AND device = '{device_id}' AND facilityId = {facility_id} 
    AND activityId = {activity_id} AND timestamp BETWEEN TIMESTAMP_SUB(target_time, INTERVAL {safe_interval} MINUTE) AND target_time;
    """
    try:
        rows = list(get_bq_client().query(query).result())
        if not rows: return None
        r = rows[0]
        return {"speed": r.s, "cadence": r.c, "gct": r.g, "air_time": r.a, "balance": r.bal, "incline": r.inc}
    except Exception as e:
        logger.error(f"BQ Error: {e}")
        return None

def save_full_session_log_async(userid, timestamp, device_id, facility_id, activity_id, model_id, metrics, llm_output, interval, lang_code):
    try:
        table = f"`{DATASET_ID}.aggregated_running_sessions`"
        json_str = json.dumps(llm_output, ensure_ascii=False).replace("'", "\\'")
        fmt = lambda v: str(v) if v is not None else 'NULL'
        lang = f"'{lang_code}'" if lang_code else "NULL"
        query = f"""
        INSERT INTO {table} (userid, device_id, facility_id, activity_id, model_id, session_timestamp, 
        avg_speed, avg_cadence, avg_gct, avg_air_time, avg_incline, avg_balance, language_code, llm_advice_json)
        VALUES ('{userid}', '{device_id}', {facility_id}, {activity_id}, {model_id}, TIMESTAMP('{timestamp}'),
        {fmt(metrics['speed'])}, {fmt(metrics['cadence'])}, {fmt(metrics['gct'])}, {fmt(metrics['air_time'])}, 
        {fmt(metrics['incline'])}, {fmt(metrics['balance'])}, {lang}, PARSE_JSON('{json_str}'))
        """
        get_bq_client().query(query).result()
    except Exception as e:
        logger.error(f"Async Logging Failed: {e}")

@lru_cache(maxsize=1024)
def get_cluster_advice(userid):
    if userid == "Anonymous": return "General recreational runner."
    query = f"""
    WITH UserCluster AS (SELECT clusterNo FROM `{SEGMENT_DATASET_ID}.user_clusters` WHERE userId = '{userid}')
    SELECT advice FROM `{SEGMENT_DATASET_ID}.cluster_advice` a JOIN UserCluster uc ON a.clusterNo = uc.clusterNo
    """
    try:
        rows = list(get_bq_client().query(query).result())
        return rows[0].advice if rows else "General recreational runner."
    except: return "General recreational runner."

# ==============================================================================
# 6. API ENDPOINT
# ==============================================================================
@app.route("/analyze_run", methods=["POST"])
def analyze_run():
    if not verify_auth(request): return jsonify({"error": "Unauthorized"}), 401

    try:
        d = request.json
        uid, ts, dev = d.get('userId'), d.get('timestamp'), d.get('device')
        fac, act, mod = d.get('facilityId'), d.get('activityId'), d.get('modelId')
        
        try: interval = int(d.get('retrievalInterval', 5))
        except: interval = 5
        
        requested_lang = d.get('languageCode', 'en')
        
        if STATIC_LOCALIZATION and requested_lang in STATIC_LOCALIZATION:
            lang = requested_lang
        else:
            lang = 'en'
        
        f_metrics = FETCH_EXECUTOR.submit(fetch_metrics, uid, ts, dev, fac, act, interval)
        f_advice = FETCH_EXECUTOR.submit(get_cluster_advice, uid)
        f_config = FETCH_EXECUTOR.submit(get_scoring_config)
        
        metrics = f_metrics.result()
        if not metrics: return jsonify({"error": "No data"}), 200
        
        labels = get_localization_labels(lang)
        is_valid, err = validate_sensor_data(metrics, labels)
        if not is_valid: return jsonify(err), 200

        config = f_config.result()
        max_words = config.get("gen_ai", {}).get("max_recommendation_words", 25)

        # Updated: Returns 'scores' dict (Labels + Context) and speed
        scores, spd_mph = calculate_scores(
            metrics['speed'], metrics['cadence'], metrics['gct'], metrics['balance'], labels, config
        )

        incline_str = f"{metrics['incline']:.1f}%" if metrics['incline'] else "0%"
        balance_str = f"{metrics['balance']:.1f}" if metrics['balance'] else "N/A"

        cache_name = get_or_create_cache()
        
        # UPDATED PROMPT: Passes Context (Reason) to LLM
        user_prompt = f"""
        Analyze Runner ({interval} mins):
        METRICS: Speed:{spd_mph:.1f}MPH, Incline:{incline_str}, Cadence:{metrics['cadence']}, GCT:{metrics['gct']}, Bal:{balance_str}
        CONTEXT: {f_advice.result()}
        SCORES (ANALYSIS): Cadence:{scores['cadence']['context']}, Horiz:{scores['horizontal']['context']}, Vert:{scores['vertical']['context']}
        LANG: {lang}
        """

        # UPDATED INSTRUCTIONS: Enforces Labels for JSON
        runtime_instruction = f"""
        INSTRUCTIONS:
        1. Generate advice in the language associated with code: '{lang}'.
        2. Keep recommendations strictly under {max_words} words.
        3. OUTPUT LABELS MUST MATCH EXACTLY: 
           Cadence: "{scores['cadence']['label']}"
           Vertical_Motion: "{scores['vertical']['label']}"
           Horizontal_Motion: "{scores['horizontal']['label']}"
        4. Cite sources in the 'Source' field.
        """
        final_prompt = user_prompt + "\n" + runtime_instruction

        schema = {"type": "object", "properties": {k: {"type": "object", "properties": {"Recommendation": {"type": "string"}, "Source": {"type": "string"}, "Score": {"type": "string"}}} for k in ["Cadence", "Horizontal_Motion", "Vertical_Motion"]}}
        
        client = get_genai_client()
        config_gen = types.GenerateContentConfig(cached_content=cache_name, temperature=0.0, max_output_tokens=350, response_mime_type="application/json", response_schema=schema)

        try:
            resp = client.models.generate_content(model=MODEL_ID, contents=final_prompt, config=config_gen)
        except Exception as e:
            if "400" in str(e) or "404" in str(e):
                logger.warning("Cache failed. Retrying...")
                cache_name = get_or_create_cache(ignore_shared=True)
                config_gen = types.GenerateContentConfig(cached_content=cache_name, temperature=0.0, max_output_tokens=350, response_mime_type="application/json", response_schema=schema)
                resp = client.models.generate_content(model=MODEL_ID, contents=final_prompt, config=config_gen)
            else: raise e

        out = json.loads(resp.text)
        LOG_EXECUTOR.submit(save_full_session_log_async, uid, ts, dev, fac, act, mod, metrics, out, interval, lang)
        
        return jsonify(out)

    except Exception as e:
        logger.error(f"Err: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
