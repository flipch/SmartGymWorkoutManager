from flask import Flask, abort, render_template, request, redirect, url_for, flash, jsonify, send_from_directory, Response
from api_client import SpeedianceClient, UpstreamError, _require_ok, unit_is_confirmed
from auth import load_token, install_flask_auth, install_flask_write_gate
from features import init_features
import media
from insights_core import json_object, valid_iso_date, valid_record_id
import json
import re
import os
import sys
import webbrowser
import secrets
import threading
from threading import Timer, Thread
import requests
try:
    import tkinter as tk
    from tkinter import scrolledtext
except Exception:
    tk = None
    scrolledtext = None
from urllib.parse import urlparse

# Determine if running as a script or frozen exe (PyInstaller)
if getattr(sys, 'frozen', False):
    # If frozen, use the temporary folder created by PyInstaller
    base_dir = sys._MEIPASS
    template_folder = os.path.join(base_dir, 'templates')
    static_folder = os.path.join(base_dir, 'static')
    app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)
else:
    # If running as script, use default paths
    app = Flask(__name__)

# Signs the flash-message cookie only. Not a constant (it was a published string): env, else random per process.
app.secret_key = os.environ.get("GYM_SECRET_KEY") or secrets.token_hex(32)
client = SpeedianceClient()


@app.errorhandler(RecursionError)
def _too_deep(_e):  # a body of 200000 '[' overflows the JSON parser: that is a bad request, not a server fault
    return jsonify({"error": "bad_request", "message": "The request body is nested too deeply."}), 400


install_flask_auth(app, load_token())
install_flask_write_gate(app)
app.register_blueprint(init_features(client))

# --- Media Caching Logic ---
# Define local cache path
# Use base_dir to ensure it works in exe mode (though usually we want cache outside the temp exe folder)
# For the cache, we actually want it next to the executable, not inside the temp folder
if getattr(sys, 'frozen', False):
    # If exe, store cache next to the exe file
    current_dir = os.path.dirname(sys.executable)
else:
    current_dir = app.root_path
    
CACHE_ROOT = os.path.join(current_dir, 'static', 'media_cache')
_PRELOAD_LOCK = threading.Lock()

def get_cache_path(url):
    """(local path, subfolder) for a vendor media URL, keyed by a hash of the whole URL; (None, None) if not allowed."""
    return media.cache_path(CACHE_ROOT, url)

@app.template_filter('local_cache')
def local_cache_filter(url, force=False):
    """Jinja filter to rewrite remote URLs to local proxy URLs."""
    if not url: return ""
    if not media.is_allowed_url(url): return url  # not ours to cache: the browser handles it directly

    # Check if file exists locally
    local_path, _ = get_cache_path(url)
    if local_path and os.path.exists(local_path):
        return url_for('media_proxy', url=url)
    
    # If forced (e.g. on detail page), use proxy to trigger download
    if force:
        return url_for('media_proxy', url=url)
    
    # If not cached and not forced, return original URL to let browser fetch directly
    return url

@app.route('/media_proxy')
def media_proxy():
    """Downloads and serves vendor media locally. Only allowlisted https media hosts; never redirects the caller."""
    remote_url = request.args.get('url')
    if not remote_url:
        return "No URL provided", 400
    local_path, subfolder = get_cache_path(remote_url)
    if not local_path:
        return "URL is not an allowed media URL", 400

    if not os.path.exists(local_path):
        try:
            media.download(remote_url, local_path, subfolder)
        except media.MediaError as err:
            print(f"[ERROR] media download refused/failed: {err}")
            return f"Could not fetch media: {err}", 502
    resp = send_from_directory(os.path.dirname(local_path), os.path.basename(local_path))
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return resp

@app.route('/')
def index():
    if not client.credentials.get("token"):
        return redirect(url_for('settings'))

    try:
        workouts = client.get_user_workouts()
        # Sort workouts from oldest to newest (so newest is closest to Calendar section)
        workouts.sort(key=lambda w: (0, int(str(w.get('id'))), '') if str(w.get('id')).isascii() and str(w.get('id')).isdigit() else (1, 0, str(w.get('id', ''))))
    except Exception as e:
        if str(e) == "Unauthorized":
            client.logout()
            flash("Session expired. Please login again.", "error")
            return redirect(url_for('settings'))
        flash("Error loading workouts. Invalid token?", "error")
        workouts = []

    unit = client.credentials.get('unit', 0)
    return render_template('index.html', workouts=workouts, unit=unit)

_UNIT_BANNER = (b'<div style="background:#92400e;color:#fff;padding:8px 14px;font:14px system-ui;text-align:center">'
                b'Your weight unit (kg or lb) has not been set, so weights on this page may be mislabelled. '
                b'<a href="/settings" style="color:#fde68a;text-decoration:underline">Choose it in Settings</a>.</div>')


@app.after_request
def no_framing(response):
    response.headers.setdefault("X-Frame-Options", "DENY")  # the UI deletes workouts and changes the unit: no other site may frame it
    response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    return response


@app.after_request
def warn_when_the_unit_is_unset(response):
    """Pages label weights from a default of kg: say so, once, on every page, until a person has chosen the unit."""
    try:
        creds = client.credentials
        if (response.mimetype == 'text/html' and response.status_code == 200 and not response.direct_passthrough
                and creds.get("token") and not unit_is_confirmed(creds) and not request.path.startswith('/settings')):
            data = response.get_data()
            match = re.search(rb'<body[^>]*>', data)
            if match:
                response.set_data(data[:match.end()] + _UNIT_BANNER + data[match.end():])
    except Exception:  # a banner must never break a page
        pass
    return response


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        # Manual config save
        try:
            device_type = int(request.form.get('device_type', client.credentials.get('device_type', 1)))
            unit = int(request.form.get('unit', client.credentials.get('unit', 0)))
        except (TypeError, ValueError):
            flash("Invalid settings value.", "error")
            return redirect(url_for('settings'))
        if unit not in (0, 1) or device_type not in (1, 2):
            flash("Invalid settings value.", "error")
            return redirect(url_for('settings'))
        allow_monster_moves = bool(request.form.get('allow_monster_moves'))
        client.save_config(
            request.form.get('user_id') or client.credentials.get('user_id'),
            request.form.get('token') or client.credentials.get('token'),
            request.form.get('region', 'Global'),
            unit,  # an omitted field keeps the unit
            request.form.get('custom_instruction', ''),
            device_type,
            allow_monster_moves,
            client.credentials.get('owned_accessories', []),
            client.credentials.get('owned_devices', []),
            unit_confirmed=True if 'unit' in request.form else None,
        )
        flash("Settings saved!", "success")
        return redirect(url_for('index'))
    
    creds = client.load_config()
    # Ensure new fields exist for older configs
    if 'owned_accessories' not in creds:
        creds['owned_accessories'] = []
    if 'owned_devices' not in creds:
        creds['owned_devices'] = []
    accessories = []
    if creds.get('token'):
        try:
            accessories = client.get_accessories()
        except Exception:
            pass
    shown = {**creds, "token_set": bool(creds.get("token")), "token": ""}  # the token never leaves the server
    return render_template('settings.html', creds=shown, accessories=accessories)

@app.route('/settings/custom_instruction', methods=['POST'])
def update_custom_instruction():
    data = json_object(request.get_json(silent=True))
    instruction = data.get('instruction', '')
    if not isinstance(instruction, str) or len(instruction) > 4000:
        return jsonify({"status": "error", "message": "instruction must be text of at most 4000 characters"}), 400
    
    # Update only the instruction, keep other settings
    creds = client.credentials
    client.save_config(
        creds.get('user_id'),
        creds.get('token'),
        creds.get('region'),
        creds.get('unit', 0),
        instruction,
        creds.get('device_type', 1),
        creds.get('allow_monster_moves', False),
        creds.get('owned_accessories', []),
        creds.get('owned_devices', []),
    )
    return jsonify({"status": "success"})

@app.route('/settings/unit', methods=['POST'])
def update_unit():
    unit = request.form.get('unit')
    if unit not in ('0', '1'):
        flash("Choose metric or imperial.", "error")
        return redirect(url_for('settings'))
    success, msg = client.update_unit(unit)
    if success:
        flash("Unit preference updated!", "success")
    else:
        flash(f"Error updating unit: {msg}", "error")
    return redirect(url_for('settings'))

@app.route('/settings/accessories', methods=['POST'])
def update_accessories():
    selected = request.form.getlist('accessories')
    try:
        owned = [int(x) for x in selected]
    except ValueError:
        flash("Accessory settings were not saved: unknown value.", "error")
        return redirect(url_for('settings'))
    creds = client.credentials
    client.save_config(
        creds.get('user_id'),
        creds.get('token'),
        creds.get('region'),
        creds.get('unit', 0),
        creds.get('custom_instruction', ''),
        creds.get('device_type', 1),
        creds.get('allow_monster_moves', False),
        owned,
        creds.get('owned_devices', []),
    )
    flash("Accessory settings updated!", "success")
    return redirect(url_for('settings'))

@app.route('/settings/owned_devices', methods=['POST'])
def update_owned_devices():
    selected = request.form.getlist('owned_devices')
    try:
        owned = [int(x) for x in selected]
    except ValueError:
        flash("Device settings were not saved: unknown value.", "error")
        return redirect(url_for('settings'))
    creds = client.credentials
    client.save_config(
        creds.get('user_id'),
        creds.get('token'),
        creds.get('region'),
        creds.get('unit', 0),
        creds.get('custom_instruction', ''),
        creds.get('device_type', 1),
        creds.get('allow_monster_moves', False),
        creds.get('owned_accessories', []),
        owned,
    )
    flash("Owned devices updated!", "success")
    return redirect(url_for('settings'))

@app.route('/login', methods=['POST'])
def login():
    email = request.form.get('email')
    password = request.form.get('password')
    region = request.form.get('region', 'Global')
    
    if not email or not password:
        flash("Email and password required", "error")
        return redirect(url_for('settings'))
    
    # Update client region before login attempt; a failed login must leave the old region (and so the stored token's host)
    previous = (client.region, client.host, client.base_url)
    client.region = region
    client.host = "euapi.speediance.com" if region == "EU" else "api2.speediance.com"
    client.base_url = "https://" + client.host

    success, message, debug_info = client.login(email, password)
    if not success:
        client.region, client.host, client.base_url = previous
    if success:
        flash("Login successful!", "success")
        return redirect(url_for('index'))
    else:
        flash(message, "error")
        return redirect(url_for('settings'))

@app.route('/logout', methods=['POST'])
def logout():
    client.logout()
    flash("Logged out successfully", "success")
    return redirect(url_for('settings'))

@app.route('/settings/preload', methods=['POST'])
def preload_assets():
    """Streamed response that downloads all assets."""
    if not client.credentials.get("token"): return "Unauthorized", 401

    def download_url(url):
        local_path, subfolder = get_cache_path(url)
        if not local_path: return "Skipped (not an allowed media URL)"
        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            return "Skipped (Already exists)"
        try:
            media.download(url, local_path, subfolder, timeout=20)
            return "Downloaded"
        except media.MediaError as err:
            return f"Failed ({err})"

    def extract_urls_from_exercise(ex):
        urls = set()
        if ex.get('img'): urls.add(ex['img'])
        
        # Variants
        for variant in ex.get('actionLibraryList', []):
            if variant.get('videoPath'): urls.add(variant['videoPath'])
            if variant.get('leftVideo'): urls.add(variant['leftVideo'])
            if variant.get('rightVideo'): urls.add(variant['rightVideo'])
            if variant.get('endVideo'): urls.add(variant['endVideo'])
            
            if variant.get('startVideo'):
                for v in variant['startVideo'].split(','):
                    if v.strip(): urls.add(v.strip())
            
            if variant.get('coach', {}).get('avatar'):
                urls.add(variant['coach']['avatar'])
                
            for key in ['actionNameVoice', 'completionTimeVoice', 'completionNumberVoice', 'goVoice', 'restConfigVoice']:
                if variant.get(key): urls.add(variant[key])
            
            for i in range(1, 7):
                key = f'guideVoice{i}'
                if variant.get(key): urls.add(variant[key])

        # Steps
        try:
            if ex.get('showDetails'):
                steps = json.loads(ex['showDetails'])
                for step in steps:
                    if step.get('img'): urls.add(step['img'])
        except:
            pass
        return urls

    def generate():
        yield "Starting deep discovery and download of assets...\n"
        yield "This process fetches full details for every exercise to ensure no video is missed.\n"
        yield "It may take several minutes. Please do not close this tab.\n\n"
        
        # 1. Accessories
        yield "--- Processing Accessories ---\n"
        try:
            accessories = client.get_accessories()
            for acc in accessories:
                if acc.get('img'): 
                    res = download_url(acc['img'])
                    yield f"Accessory {acc.get('name', 'Unknown')}: {res}\n"
        except Exception as e:
            yield f"Error scanning accessories: {e}\n"

        # 2. Library
        yield "\n--- Processing Exercise Library ---\n"
        try:
            # Get the list of groups first
            library_groups = client.get_library()
            total_groups = len(library_groups)
            
            for i, group in enumerate(library_groups):
                group_id = group.get('id')
                group_title = group.get('title', f'ID {group_id}')
                yield f"[{i+1}/{total_groups}] Processing: {group_title} ... "
                
                # Fetch FULL details for this exercise group
                # This ensures we get all variants and videos even if the list endpoint was incomplete
                try:
                    detail = client.get_exercise_detail(group_id)
                    if not detail:
                        yield "Failed to fetch details.\n"
                        continue
                        
                    urls = extract_urls_from_exercise(detail)
                    yield f"Found {len(urls)} assets.\n"
                    
                    for url in urls:
                        filename = os.path.basename(urlparse(url).path)
                        res = download_url(url)
                        if "Downloaded" in res:
                            yield f"    -> {filename}: {res}\n"
                            
                except Exception as e:
                    yield f"Error fetching details: {e}\n"
                    
        except Exception as e:
            yield f"Error scanning library: {e}\n"
        
        yield "\nDone! All assets have been processed."

    if not _PRELOAD_LOCK.acquire(blocking=False):
        return "A preload is already running", 409

    def guarded():
        try:
            yield from generate()
        finally:
            _PRELOAD_LOCK.release()

    return Response(guarded(), mimetype='text/plain')

@app.route('/api/stats/<int:group_id>')
def api_stats(group_id):
    """Returns user statistics for a specific exercise."""
    if not client.credentials.get("token"): 
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        # Fetch stats using the client
        result = client.get_user_action_stats(group_id)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/library')
def library():
    if not client.credentials.get("token"): return redirect(url_for('settings'))
    try:
        exercises = client.get_library(strict=True)
        accessories = client.get_accessories()
        categories = client.get_categories(strict=True)
    except Exception as e:
        if str(e) == "Unauthorized":
            client.logout()
            flash("Session expired. Please login again.", "error")
            return redirect(url_for('settings'))
        flash(f"Error loading library: {e}", "error")
        exercises = []
        accessories = []
        categories = []

    accessory_map = {str(acc['id']): acc['name'] for acc in accessories if isinstance(acc, dict) and 'id' in acc and 'name' in acc}
    
    # Enrich exercises with equipment names
    for ex in exercises:
        acc_ids = str(ex.get('accessories', '')).split(',')
        names = [accessory_map.get(aid, 'Standard') for aid in acc_ids if aid]
        ex['equipment_name'] = ', '.join(names) if names else 'Standard'
        
    owned_accessories = client.credentials.get('owned_accessories', [])
    owned_devices = client.credentials.get('owned_devices', [])
    return render_template(
        'library.html',
        exercises=exercises,
        categories=categories,
        device_type=client.device_type,
        allow_monster_moves=client.allow_monster_moves,
        owned_accessories=owned_accessories,
        owned_devices=owned_devices,
    )

@app.route('/library/refresh', methods=['POST'])
def refresh_library():
    if not client.credentials.get("token"): return redirect(url_for('settings'))
    
    # Clear memory and disk cache
    client.library_cache = None
    if os.path.exists(client.library_cache_file):
        try:
            os.remove(client.library_cache_file)
        except Exception as e:
            print(f"Error removing cache file: {e}")
            
    flash("Library cache cleared. Reloading from server...", "info")
    return redirect(url_for('library'))

@app.route('/exercise/<int:ex_id>')
def exercise_detail(ex_id):
    if not client.credentials.get("token"): return redirect(url_for('settings'))
    
    # 1. Load details
    try:
        detail = client.get_exercise_detail(ex_id, strict=True)
    except Exception as e:
        if str(e) == "Unauthorized":
            client.logout()
            flash("Session expired. Please login again.", "error")
            return redirect(url_for('settings'))
        flash(f"Could not load this exercise ({type(e).__name__}).", "error")
        return redirect(url_for('library'))
    
    if not detail.get('actionLibraryList'):
        flash("This exercise has no variants to show.", "error")
        return redirect(url_for('library'))

    # 2. Resolve accessories (IDs -> Objects with Image/Name)
    all_accessories = client.get_accessories()
    required_ids = str(detail.get('accessories') or '').split(',')
    
    mapped_accessories = []
    for acc in all_accessories:
        # Check if the ID is in the required list
        if isinstance(acc, dict) and str(acc.get('id')) in required_ids:
            mapped_accessories.append(acc)
            
    # 3. "showDetails" is a JSON string in the API response, we need to parse it
    # Format: [{"context": "Text...", "img": "url..."}, ...]
    try:
        if detail.get('showDetails'):
            detail['steps'] = json.loads(detail['showDetails'])
        else:
            detail['steps'] = []
    except Exception as e:
        print(f"JSON Parse Error: {e}")
        detail['steps'] = []

    unit = client.credentials.get('unit', 0)
    return render_template('exercise_detail.html', ex=detail, accessories=mapped_accessories, ex_id=ex_id, unit=unit)

@app.route('/api/exercise/<int:ex_id>')
def api_exercise_detail(ex_id):
    """Returns details as JSON for the frontend (dropdowns)"""
    if not client.credentials.get("token"): 
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        # Uses the existing cache/request
        detail = client.get_exercise_detail(ex_id, strict=True)  # a vendor blip is an error, never "no variants"
        return jsonify(detail)
    except Exception as e:
        if str(e) == "Unauthorized":
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify({"error": "upstream_error"}), 502

@app.route('/api/workout/<code>')
def api_workout_detail(code):
    """Returns workout detail as JSON (used for bulk export)."""
    if not client.credentials.get("token"):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        detail = client.get_workout_detail(code)
        return jsonify(detail)
    except Exception as e:
        if str(e) == "Unauthorized":
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify({"error": str(e)}), 500

@app.route('/api/calendar')
def api_calendar():
    """Returns calendar data for a specific month."""
    if not client.credentials.get("token"):
        return jsonify({"error": "Unauthorized"}), 401
    
    date_str = request.args.get('date')
    if not date_str:
        return jsonify({"error": "Missing date parameter"}), 400
        
    try:
        data = client.get_calendar_month(date_str)
        return jsonify(data)
    except ValueError as e:
        return jsonify({"error": "bad_request", "message": str(e)}), 400
    except Exception as e:
        if str(e) == "Unauthorized":
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify({"error": str(e)}), 500

def _valid_schedule_status(status):
    """1 schedules, 0 removes; a bool or anything else is refused rather than forwarded to the vendor."""
    return type(status) is int and status in (0, 1)


def _record_arg(value):
    """A vendor record id as a client may send it (digits as a number, or a code as a string); never a bool, 0 or a negative."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 < value < 10 ** 12
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value) and int(value) <= 0:
        return False  # "0" and "-7" are no more ids than 0 and -7
    return valid_record_id(value)


@app.route('/api/schedule', methods=['POST'])
def api_schedule():
    """Schedules or unschedules a workout."""
    if not client.credentials.get("token"):
        return jsonify({"error": "Unauthorized"}), 401

    data = json_object(request.get_json(silent=True))
    date_str = data.get('date')
    template_code = data.get('templateCode')
    status = data.get('status')

    if not date_str or not template_code or status is None:
        return jsonify({"error": "Missing parameters"}), 400
    if not (valid_iso_date(date_str) and _valid_schedule_status(status) and _record_arg(template_code)):
        return jsonify({"error": "Invalid parameters"}), 400  # nothing the caller sent is echoed back

    try:
        success = client.schedule_workout(date_str, template_code, status)
        return jsonify({"success": success})
    except Exception as e:
        if str(e) == "Unauthorized":
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify({"error": str(e)}), 500

@app.route('/api/schedule_course', methods=['POST'])
def api_schedule_course():
    """Schedules or unschedules an official course."""
    if not client.credentials.get("token"):
        return jsonify({"error": "Unauthorized"}), 401

    data = json_object(request.get_json(silent=True))
    date_str = data.get('date')
    course_id = data.get('courseId')
    status = data.get('status', 1)

    if not date_str or not course_id:
        return jsonify({"error": "Missing parameters"}), 400
    if not (valid_iso_date(date_str) and _valid_schedule_status(status) and _record_arg(course_id)):
        return jsonify({"error": "Invalid parameters"}), 400

    try:
        success = client.schedule_course(date_str, course_id, status)
        return jsonify({"success": success})
    except Exception as e:
        if str(e) == "Unauthorized":
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify({"error": str(e)}), 500

@app.route('/history')
def history_page():
    if not client.credentials.get("token"):
        return redirect(url_for('settings'))
    unit = client.credentials.get('unit', 0)
    return render_template('history.html', unit=unit)

@app.route('/api/history')
def api_history():
    if not client.credentials.get("token"):
        return jsonify({"error": "Unauthorized"}), 401
    start = request.args.get('start')
    end = request.args.get('end')
    if not start or not end:
        return jsonify({"error": "Missing start/end parameters"}), 400
    if not (valid_iso_date(start) and valid_iso_date(end)):
        return jsonify({"error": "start and end must be YYYY-MM-DD dates"}), 400
    try:
        records = client.get_training_records(start, end)
        stats = client.get_training_stats(start, end)
        return jsonify({"records": records, "stats": stats})
    except Exception as e:
        if str(e) == "Unauthorized":
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify({"error": "history_unavailable", "detail": type(e).__name__}), 502

@app.route('/api/history/detail/<int:training_id>')
def api_history_detail(training_id):
    """Returns detailed info for a completed training session."""
    if not client.credentials.get("token"):
        return jsonify({"error": "Unauthorized"}), 401
    training_type = request.args.get('type', 'custom')  # 'course' or 'custom'
    try:
        detail = client.get_training_detail(training_id, training_type)
        session_info = client.get_training_session_info(training_id)
        return jsonify({"detail": detail, "session": session_info})
    except Exception as e:
        if str(e) == "Unauthorized":
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify({"error": "history_unavailable", "detail": type(e).__name__}), 502

@app.route('/browse')
def browse_page():
    if not client.credentials.get("token"):
        return redirect(url_for('settings'))
    unit = client.credentials.get('unit', 0)
    owned_accessories = client.credentials.get('owned_accessories', [])
    owned_devices = client.credentials.get('owned_devices', [])
    return render_template('browse.html', unit=unit, owned_accessories=owned_accessories, owned_devices=owned_devices)

@app.route('/api/browse/courses')
def api_browse_courses():
    if not client.credentials.get("token"):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        all_courses = []
        seen_ids = set()
        for page in range(1, 20):
            batch = client.get_courses_page(page, 200)
            if not batch:
                break
            for c in batch:
                if c.get('id') not in seen_ids:
                    seen_ids.add(c['id'])
                    all_courses.append(c)
            if len(batch) < 200:
                break
        return jsonify({"courses": all_courses})
    except Exception as e:
        if "Unauthorized" in str(e):
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify({"error": str(e)}), 500

@app.route('/api/browse/course/<int:course_id>')
def api_browse_course_detail(course_id):
    if not client.credentials.get("token"):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        detail = client.get_course_detail(course_id)
        return jsonify(detail)
    except Exception as e:
        if "Unauthorized" in str(e):
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify({"error": str(e)}), 500

@app.route('/api/browse/programs')
def api_browse_programs():
    if not client.credentials.get("token"):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        page = request.args.get('page', 1, type=int)
        programs = client.get_programs_page(page, 200)
        return jsonify({"programs": programs, "page": page, "hasMore": len(programs) == 200})
    except Exception as e:
        if "Unauthorized" in str(e):
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify({"error": str(e)}), 500

@app.route('/api/browse/program/<int:plan_id>')
def api_browse_program_detail(plan_id):
    if not client.credentials.get("token"):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        detail = client.get_program_detail(plan_id)
        return jsonify(detail)
    except Exception as e:
        if "Unauthorized" in str(e):
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify({"error": str(e)}), 500

@app.route('/edit/<string:code>')  # HERE: string instead of int
def edit(code):
    if not client.credentials.get("token"): return redirect(url_for('settings'))
    
    try:
        # Load workout details via code
        workout = client.get_workout_detail(code)
        library = client.get_library(strict=True)  # an outage must not open an editor that cannot add anything
        categories = client.get_categories(strict=True)
    except Exception as e:
        if str(e) == "Unauthorized":
            client.logout()
            flash("Session expired. Please login again.", "error")
            return redirect(url_for('settings'))
        flash(f"Error loading data: {e}", "error")
        return redirect(url_for('index'))
    
    if not workout:
        flash("Could not load workout details.", "error")
        return redirect(url_for('index'))

    unit = client.credentials.get("unit", 0)
    custom_instruction = client.credentials.get("custom_instruction", "")
    return render_template(
        'create.html',
        library=library,
        existing_workout=workout,
        unit=unit,
        custom_instruction=custom_instruction,
        categories=categories,
        device_type=client.device_type,
        allow_monster_moves=client.allow_monster_moves,
    )


@app.route('/create', methods=['GET', 'POST'])
def create():
    if not client.credentials.get("token"): return redirect(url_for('settings'))
    
    if request.method == 'POST':
        data = json_object(request.get_json(silent=True))
        name = data.get('name')
        exercises = data.get('exercises')
        template_id = data.get('id') 
        
        shown_unit = data.get('unit')
        if shown_unit is not None and (isinstance(shown_unit, bool) or shown_unit != int(bool(client.credentials.get('unit')))):
            return jsonify({"status": "error", "message": "Your unit setting changed since this page was opened "
                                                          "(kg and lb differ): reload the page and check the weights."})
        try:
            result = client.save_workout(name, exercises, template_id)
            if result.get('code') == 0:
                return jsonify({"status": "success"})
            else:
                return jsonify({"status": "error", "message": result.get('message')})
        except Exception as e:
            if str(e) == "Unauthorized":
                return jsonify({"status": "error", "message": "Session expired. Please login again."}), 401
            return jsonify({"status": "error", "message": str(e)})

    try:
        library = client.get_library(strict=True)
        categories = client.get_categories(strict=True)
    except Exception as e:
        if str(e) == "Unauthorized":
            client.logout()
            flash("Session expired. Please login again.", "error")
            return redirect(url_for('settings'))
        flash(f"Could not load the exercise library ({type(e).__name__}): reload before building a workout.", "error")
        library = []
        categories = []

    unit = client.credentials.get("unit", 0)
    custom_instruction = client.credentials.get("custom_instruction", "")
    
    # HERE: We pass 'None' so the template knows: "No data to preload"
    # This has NO influence on the edit route, which sends its own data.
    return render_template(
        'create.html',
        library=library,
        existing_workout=None,
        unit=unit,
        custom_instruction=custom_instruction,
        categories=categories,
        device_type=client.device_type,
        allow_monster_moves=client.allow_monster_moves,
    )

@app.route('/delete/<id>', methods=['POST'])
def delete(id):
    if not valid_record_id(id):  # template ids can be opaque, but never a path or a list
        abort(404)
    try:
        client.delete_workout(id)
    except Exception as e:
        # the vendor refused or was unreachable: the workout still exists, so do not say it was deleted
        flash("Could not delete the workout." if str(e) != "Unauthorized" else "Session expired. Please login again.",
              "error")
        return redirect(url_for('index'))
    flash("Workout deleted.", "info")
    return redirect(url_for('index'))

@app.route('/workout_history')
def workout_history():
    if not client.credentials.get("token"): return redirect(url_for('settings'))
    return render_template('workout_history.html')

@app.route('/api/workout_history', methods=['GET'])
def api_workout_history():
    start = request.args.get('start')
    end = request.args.get('end')
    if not start or not end:
        return jsonify({"error": "Missing dates"}), 400
    if not (valid_iso_date(start) and valid_iso_date(end)):
        return jsonify({"error": "start and end must be YYYY-MM-DD"}), 400

    try:
        data = client.get_training_history(start, end)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": "history_unavailable", "detail": type(e).__name__}), 502

@app.route('/api/workout_history/detail/<path:record_id>')
def api_workout_history_detail(record_id):
    if not valid_record_id(record_id):
        return jsonify({"error": "invalid record id"}), 400
    training_type = request.args.get('type', default=2, type=int)

    if training_type == 5:
        target_url = f"{client.base_url}/api/app/trainingInfo/cttTrainingInfoDetail/{record_id}"
    else:
        target_url = f"{client.base_url}/api/app/trainingInfo/courseTrainingInfoDetail/{record_id}"

    try:
        # through _request: timeout, no redirects (the session Token must not be replayed to another host), code-91 handling
        response = client._request('GET', target_url, headers=client._get_headers())

        if response.status_code == 200:
            _require_ok(response, "training detail")  # HTTP 200 with a vendor error code is not a session
            body = response.json()
            if not isinstance(body, dict) or not body.get("data"):  # code 0 and no data: an unknown id, not a workout with no sets
                return jsonify({"error": "training_detail_not_found", "type_requested": training_type}), 502
            return jsonify(body)
        status = 401 if response.status_code == 401 else 502
        return jsonify({"error": f"Speediance API Error: {response.status_code}", "type_requested": training_type}), status
    except Exception as e:
        if str(e) == "Unauthorized":
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify({"error": "upstream_error", "detail": type(e).__name__}), 502

class TextRedirector(object):
    def __init__(self, widget, tag="stdout"):
        self.widget = widget
        self.tag = tag

    def write(self, str):
        try:
            self.widget.configure(state="normal")
            self.widget.insert("end", str, (self.tag,))
            self.widget.see("end")
            self.widget.configure(state="disabled")
        except:
            pass
    
    def flush(self):
        pass

def dev_server_options(env=None) -> dict:
    """`python app.py` settings. The Werkzeug debugger is remote code execution: loopback only, and only when asked."""
    env = os.environ if env is None else env
    return {"debug": env.get("GYM_DEBUG") == "1", "host": env.get("GYM_BIND", "127.0.0.1"), "port": 5001}


def open_browser():
    webbrowser.open_new("http://127.0.0.1:5001")

def run_flask_server():
    try:
        app.run(debug=False, port=5001, host='0.0.0.0', use_reloader=False)
    except Exception as e:
        print(f"Error starting server: {e}")

def start_gui():
    if tk is None:
        print("Tkinter is not available; starting Flask server without GUI.")
        run_flask_server()
        return
    root = tk.Tk()
    root.title("Unofficial Speediance Workout Manager Server")
    root.geometry("700x500")
    
    lbl = tk.Label(root, text="Unofficial Speediance Workout Manager is running.\nDo not close this window while using the app.", font=("Arial", 10), pady=10)
    lbl.pack()

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=5)

    btn_open = tk.Button(btn_frame, text="Open in Browser", command=open_browser, bg="#4CAF50", fg="white", font=("Arial", 10, "bold"), padx=10, pady=5)
    btn_open.pack(side=tk.LEFT, padx=10)

    def on_close():
        root.destroy()
        sys.exit(0)

    btn_close = tk.Button(btn_frame, text="Stop Server & Exit", command=on_close, bg="#f44336", fg="white", font=("Arial", 10, "bold"), padx=10, pady=5)
    btn_close.pack(side=tk.LEFT, padx=10)

    log_frame = tk.Frame(root)
    log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
    
    tk.Label(log_frame, text="Server Logs:", anchor="w").pack(fill=tk.X)
    
    text_area = scrolledtext.ScrolledText(log_frame, state='disabled', font=("Consolas", 9))
    text_area.pack(fill=tk.BOTH, expand=True)
    
    sys.stdout = TextRedirector(text_area, "stdout")
    sys.stderr = TextRedirector(text_area, "stderr")

    root.protocol("WM_DELETE_WINDOW", on_close)

    t = Thread(target=run_flask_server, daemon=True)
    t.start()

    Timer(2.0, open_browser).start()

    root.mainloop()

if __name__ == '__main__':
    if getattr(sys, 'frozen', False):
        start_gui()
    else:
        app.run(**dev_server_options())
