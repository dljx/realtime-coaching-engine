import requests
import json
import time

# --- CONFIGURATION ---
BASE_URL = "https://running-form-coach-test-570470297309.us-central1.run.app" 
FIXED_TIMESTAMP = "2025-10-01 12:00:00 UTC"
USER_ID = "test_user_suite"
API_KEY = os.environ.get("API_KEY", "your-api-key-here")

# --- HELPERS ---
G = '\033[92m' # Green
R = '\033[91m' # Red
B = '\033[94m' # Blue
Y = '\033[93m' # Yellow
X = '\033[0m'  # Reset

def run_test(name, device_id, lang="en_US", checks=None):
    print(f"\n{B}========================================{X}")
    print(f"{B}TEST: {name}{X}")
    print(f"{B}========================================{X}")
    
    payload = {
        "userId": USER_ID, "device": device_id, "facilityId": 999, "activityId": 1, "modelId": 201,
        "timestamp": FIXED_TIMESTAMP, "language_code": lang, "retrieval_interval": 5
    }
    
    start = time.time()
    try:
        resp = requests.post(f"{BASE_URL}/analyze_run", json=payload, headers={"X-Api-Key": API_KEY}, timeout=120)
        duration = time.time() - start
    except Exception as e:
        print(f"{R}FAIL: Network Error: {e}{X}")
        return None

    print(f"Latency: {duration:.2f}s")
    if resp.status_code != 200:
        print(f"{R}FAIL: Status {resp.status_code}{X}\n{resp.text}")
        return None

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
                # Handle checks that need duration vs just data
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
def check_score(m, v): return lambda d: d.get(m, {}).get('Score') == v
def check_format(d): 
    return all(k in d.get('Cadence', {}) for k in ['Score', 'Recommendation', 'Source'])
def check_word_count(d):
    for m in ["Cadence", "Vertical_Motion", "Horizontal_Motion"]:
        if len(d.get(m, {}).get("Recommendation", "").split()) > 25: return False
    return True
def check_latency(d, t): return t <= 12.0
def check_tone(d):
    txt = json.dumps(d).lower()
    return not any(w in txt for w in ["bad", "terrible", "awful", "stupid"])
def check_score_lang(d): return d.get('Cadence', {}).get('Score') == "Excelente"
def check_insight_lang(d): 
    return "tu" in d.get('Cadence', {}).get('Recommendation', "").lower()
def check_idle_lang(d): return "Aumenta" in d.get('message', "")

# --- EXECUTION ---
if __name__ == "__main__":
    
    # 1. Invalid Case
    run_test("Invalid Case (<5mph)", "TC_INVALID", checks={"Status is Idle": check_status_idle})

    # 2. Scoring Logic (Cadence)
    run_test("Cadence Excellent", "TC_CAD_EXC", checks={"Score is Excellent": check_score("Cadence", "Excellent")})
    run_test("Cadence Good (Fair)", "TC_CAD_GOOD", checks={"Score is Fair": check_score("Cadence", "Fair")})
    run_test("Cadence Needs Imp", "TC_CAD_NI", checks={"Score is Needs Improvement": check_score("Cadence", "Needs Improvement")})

    # 3. Scoring Logic (Vertical)
    run_test("Vertical Excellent", "TC_VERT_EXC", checks={"Score is Excellent": check_score("Vertical_Motion", "Excellent")})
    run_test("Vertical Good (Fair)", "TC_VERT_GOOD", checks={"Score is Fair": check_score("Vertical_Motion", "Fair")})
    run_test("Vertical Needs Imp", "TC_VERT_NI", checks={"Score is Needs Improvement": check_score("Vertical_Motion", "Needs Improvement")})

    # 4. Scoring Logic (Horizontal)
    run_test("Horizontal Excellent", "TC_HORZ_EXC", checks={"Score is Excellent": check_score("Horizontal_Motion", "Excellent")})
    run_test("Horizontal Good (Fair)", "TC_HORZ_GOOD", checks={"Score is Fair": check_score("Horizontal_Motion", "Fair")})
    run_test("Horizontal Needs Imp", "TC_HORZ_NI", checks={"Score is Needs Improvement": check_score("Horizontal_Motion", "Needs Improvement")})

    # 5. Determinism
    print(f"\n{B}========================================{X}")
    print(f"{B}TEST: Determinism Check{X}")
    print(f"{B}========================================{X}")
    # Note: Using run_test helper without printing to keep log clean, or standard run
    print("...Running First Request...")
    r1 = run_test("Determinism Run 1", "TC_CAD_EXC")
    print("...Running Second Request...")
    r2 = run_test("Determinism Run 2", "TC_CAD_EXC")
    
    if r1 and r2 and json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True):
        print(f"{G}>> RESULT: PASS (Outputs match exactly){X}")
    else:
        print(f"{R}>> RESULT: FAIL (Outputs differ){X}")

    # 6. Language/Tone
    run_test("Tone Check", "TC_CAD_NI", checks={"No negative words": check_tone})

    # 7. BQ & API Format
    run_test("API Format", "TC_CAD_EXC", checks={"Schema Valid": check_format})

    # 8. Insights Word Length
    run_test("Word Length <= 25", "TC_CAD_EXC", checks={"Length OK": check_word_count})

    # 9. Latency
    run_test("Latency <= 12s", "TC_CAD_EXC", checks={"Time <= 12s": check_latency})

    # 10. Language Checks
    run_test("Scoring Language (ES)", "TC_CAD_EXC", lang="es", checks={"Score is Excelente": check_score_lang})
    run_test("Insights Language (ES)", "TC_CAD_EXC", lang="es", checks={"Insight is Spanish": check_insight_lang})
    run_test("Exception Language (ES)", "TC_INVALID", lang="es", checks={"Error is Spanish": check_idle_lang})
