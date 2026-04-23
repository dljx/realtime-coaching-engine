import requests
import json
import time

# --- CONFIGURATION ---
BASE_URL = "https://running-form-coach-test-570470297309.us-central1.run.app" 
FIXED_TIMESTAMP = "2025-09-01 12:00:00 UTC" # UPDATED to Match Test Suite B
API_KEY = os.environ.get("API_KEY", "your-api-key-here")

# --- HELPERS ---
G = '\033[92m' # Green
R = '\033[91m' # Red
B = '\033[94m' # Blue
Y = '\033[93m' # Yellow
X = '\033[0m'  # Reset

def run_test(name, user_id, device_id, lang="en_US", checks=None, omit_key=False):
    print(f"\n{B}========================================{X}")
    print(f"{B}TEST: {name}{X}")
    print(f"{B}========================================{X}")
    
    payload = {
        "userId": user_id, "device": device_id, "facilityId": 999, "activityId": 1, "modelId": 201,
        "timestamp": FIXED_TIMESTAMP, "language_code": lang, "retrieval_interval": 5
    }
    
    headers = {"Content-Type": "application/json"}
    if not omit_key: headers["X-Api-Key"] = API_KEY
    
    start = time.time()
    try:
        resp = requests.post(f"{BASE_URL}/analyze_run", json=payload, headers=headers, timeout=120)
        duration = time.time() - start
    except Exception as e:
        print(f"{R}FAIL: Network Error: {e}{X}")
        return None

    print(f"Latency: {duration:.2f}s")
    
    # Handle Expected Errors (like 401 Unauthorized)
    if resp.status_code != 200 and not omit_key:
        print(f"{R}FAIL: Status {resp.status_code}{X}\n{resp.text}")
        return None
    elif omit_key and resp.status_code == 401:
        print(f"{G}PASS: Correctly received 401 Unauthorized{X}")
        print(f"{Y}--- API RESPONSE ---{X}")
        print(resp.text)
        print(f"{Y}--------------------{X}")
        return resp.json()

    data = resp.json()
    
    # --- PRINT OUTPUT ---
    print(f"{Y}--- API RESPONSE ---{X}")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"{Y}--------------------{X}")
    
    # --- RUN CHECKS ---
    all_passed = True
    if checks:
        for check_name, check_func in checks.items():
            try: 
                try: result = check_func(data, duration)
                except TypeError: result = check_func(data)
                
                if result: 
                    print(f"  [x] {check_name}")
                else:
                    print(f"  [ ] {check_name} {R}(FAILED){X}")
                    all_passed = False
            except Exception as e:
                print(f"  [ ] {check_name} {R}(ERROR: {e}){X}")
                all_passed = False
    
    if all_passed: print(f"{G}>> RESULT: PASS{X}")
    else: print(f"{R}>> RESULT: FAIL{X}")
    return data

# --- ASSERTIONS ---
def check_status_idle(d): return d.get('status') == 'idle'
def check_status_error(d): return d.get('status') == 'sensor_error'
def check_score(m, v): return lambda d: d.get(m, {}).get('Score') == v
def check_score_in(m, v_list): return lambda d: d.get(m, {}).get('Score') in v_list
def check_lang_score(d, v): return d.get('Cadence', {}).get('Score') == v
def check_lang_insight(d, kw): return kw in d.get('Cadence', {}).get('Recommendation', "").lower()
def check_unauthorized(d): return d.get("error") == "Unauthorized"

# --- EXECUTION ---
if __name__ == "__main__":
    
    # TC 1: Idle Check (<5 mph)
    run_test("TC 1: Idle Check", "test_user_general", "DEV_IDLE", checks={"Status is Idle": check_status_idle})

    # TC 2: Sensor Integrity
    run_test("TC 2: Sensor Integrity", "test_user_general", "DEV_SENSOR_ERR", checks={"Status is Error": check_status_error})

    # TC 3: Elite Metrics
    run_test("TC 3: Elite Metrics", "test_user_elite", "DEV_ELITE", checks={
        "Cadence Excellent": check_score("Cadence", "Excellent"),
        "Vert Excellent": check_score("Vertical_Motion", "Excellent"),
        "Horiz Excellent": check_score("Horizontal_Motion", "Excellent")
    })

    # TC 4: Spanish Localization
    run_test("TC 4: Spanish Localization", "test_user_elite", "DEV_ELITE", lang="es", checks={
        "Score is Excelente": lambda d: check_lang_score(d, "Excelente"),
        "Insight is Spanish": lambda d: check_lang_insight(d, "tu")
    })

    # TC 5: Poor Cadence Logic
    run_test("TC 5: Poor Cadence Logic", "test_user_general", "DEV_POOR_CAD", checks={
        "Cadence Needs Imp": check_score("Cadence", "Needs Improvement")
    })

    # TC 6: Horizontal Excellent
    run_test("TC 6: Horizontal Excellent", "test_user_general", "DEV_HORIZ_EXC", checks={
        "Horizontal Excellent": check_score("Horizontal_Motion", "Excellent")
    })

    # TC 7: Horizontal Poor (>2.0% dev)
    run_test("TC 7: Horizontal Poor", "test_user_general", "DEV_HORIZ_POOR", checks={
        "Horizontal Needs Imp": check_score("Horizontal_Motion", "Needs Improvement")
    })

    # TC 8: Vertical Poor (High GCT)
    run_test("TC 8: Vertical Poor", "test_user_general", "DEV_VERT_POOR", checks={
        "Vertical Needs Imp": check_score("Vertical_Motion", "Needs Improvement")
    })

    # TC 9: Contradictory Data
    # At 12 MPH, cadence of 140 is extremely low -> Needs Improvement
    run_test("TC 9: Contradictory Data", "test_user_general", "DEV_CONTRADICT", checks={
        "Cadence Needs Imp": check_score("Cadence", "Needs Improvement")
    })

    # TC 10: Missing API Key
    run_test("TC 10: Unauthorized", "test_user_general", "DEV_GENERAL", omit_key=True, checks={
        "Error Message Correct": check_unauthorized
    })
