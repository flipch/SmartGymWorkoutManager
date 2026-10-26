import math
import time
import json
import requests
import os
from urllib.parse import quote

from harness.localsecrets import write_json_private
from insights_core import valid_iso_date

def _q(value, safe: str = "") -> str:
    """Percent-encode an id/code before it goes into an upstream URL (no path or query injection)."""
    return quote(str(value), safe=safe)


def _d(value) -> str:
    """A YYYY-MM-DD date for an upstream query string; anything else is a caller bug, not a request to forward."""
    text = str(value)
    if not valid_iso_date(text):
        raise ValueError("date must be YYYY-MM-DD")
    return text


class UpstreamError(Exception):
    """Speediance could not be reached, or answered with an error: never to be read as "no data"."""


def _json_data(resp, what, kind, empty=None):
    """The `data` of a vendor answer, or an error: a failed read must never look like an empty history ("well rested")."""
    if resp.status_code == 401:
        raise Exception("Unauthorized")
    if resp.status_code != 200:
        raise UpstreamError(f"{what} failed (HTTP {resp.status_code})")
    try:
        body = resp.json()
    except ValueError:
        raise UpstreamError(f"{what} response was not JSON") from None
    if not isinstance(body, dict) or body.get('code') not in (None, 0):
        raise UpstreamError(f"{what} was rejected")
    make = empty if empty is not None else (kind if isinstance(kind, type) else dict)
    if 'data' not in body:
        if body.get('code') == 0:  # an explicit success with nothing in it
            return make()
        raise UpstreamError(f"{what} response had no data")
    data = body['data']
    if data is None:
        return make()
    if not isinstance(data, kind):
        raise UpstreamError(f"{what} response had an unexpected shape")
    return data


def _require_ok(resp, what):
    """Raise unless the vendor accepted a write: HTTP < 400 and, when the body carries a `code`, code == 0."""
    if resp.status_code == 401:
        raise Exception("Unauthorized")
    if resp.status_code >= 300:
        raise UpstreamError(f"{what} failed (HTTP {resp.status_code})")
    if resp.status_code == 204:
        return
    try:
        body = resp.json()
    except ValueError:
        if (getattr(resp, "text", "") or "").strip():  # e.g. a proxy's HTML page
            raise UpstreamError(f"{what} response was not JSON") from None
        return  # an empty 2xx body is a plain success
    if isinstance(body, dict) and body.get('code') not in (None, 0):
        raise UpstreamError(f"{what} was rejected (code {body.get('code')})")


# What reaches the machine is bounded here, whatever the caller (web form, MCP agent, coach program). The numbers
# mirror the web editor's own limits (static/workout-logic.js): custom loads are kg, up to 100 (50 on one cable).
MAX_EXERCISES, MAX_SETS, MAX_REPS, MAX_SECONDS, MAX_REST_S = 30, 20, 99, 3600, 300
MAX_WEIGHT_KG, MAX_WEIGHT_KG_UNILATERAL, MAX_PRESET_RM, MAX_LEVEL = 100.0, 50.0, 20, 10
MAX_WEIGHT_LB, MAX_WEIGHT_LB_UNILATERAL = 220.0, 110.0  # the editor's imperial limits; the API stores the ACCOUNT's unit
REQUEST_TIMEOUT_S = 25
MAX_MODE = 5  # Standard, Chains, Eccentric, and the two the coach names (constant, spotter)
MAX_TEMPLATE_NAME = 100


def _bounded(value, what, hi):
    if isinstance(value, bool):
        raise ValueError(f"{what} must be a number")
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number") from None
    if not math.isfinite(x) or not 0 <= x <= hi:
        raise ValueError(f"{what} must be between 0 and {hi:g}")
    return x


def _group_id(value, where):
    """A library group id as an int ('5' and 5 are the same exercise; anything else is refused, never interpolated)."""
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip().isdigit():
        raise ValueError(f"{where}: groupId must be a positive whole number")
    n = int(str(value).strip())
    if not 0 < n < 10**12:
        raise ValueError(f"{where}: groupId must be a positive whole number")
    return n


def validate_workout(name, exercises, imperial=False):
    """Refuse a workout whose numbers could be wrong by an order of magnitude (NaN, negative, wrong unit, runaway counts).
    `imperial` is the ACCOUNT's unit: the template API stores weights in it, with no conversion (tests/test_unit.py).
    Messages name the field and the position, never the value."""
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_TEMPLATE_NAME:
        raise ValueError(f"name must be 1-{MAX_TEMPLATE_NAME} characters")
    if not isinstance(exercises, list) or not 1 <= len(exercises) <= MAX_EXERCISES:
        raise ValueError(f"exercises must be a list of 1-{MAX_EXERCISES}")
    for n, ex in enumerate(exercises, 1):
        if not isinstance(ex, dict) or not isinstance(ex.get('sets'), list) or not 1 <= len(ex['sets']) <= MAX_SETS:
            raise ValueError(f"exercise {n}: needs 1-{MAX_SETS} sets")
        _group_id(ex.get('groupId'), f"exercise {n}")
        preset = ex.get('preset_id')
        stat = _bounded(ex.get('data_stat_type') or 0, f"exercise {n}: data_stat_type", 100)
        for m, st in enumerate(ex['sets'], 1):
            if not isinstance(st, dict):
                raise ValueError(f"exercise {n} set {m}: must be an object")
            where = f"exercise {n} set {m}"
            seconds = str(st.get('unit', 'reps')).lower() == 'sec'
            _bounded(st.get('reps') or 0, f"{where}: reps", MAX_SECONDS if seconds else MAX_REPS)
            _bounded(st.get('rest') or 0, f"{where}: rest", MAX_REST_S)
            if st.get('mode') not in (None, ""):
                mode = _bounded(st['mode'], f"{where}: mode", MAX_MODE)
                if mode < 1 or mode != int(mode):
                    raise ValueError(f"{where}: mode must be a whole number from 1 to {MAX_MODE}")
            if stat == 6:
                hi = MAX_LEVEL
            elif preset in (None, -1, "-1"):
                hi = MAX_WEIGHT_LB if imperial else MAX_WEIGHT_KG
            else:
                hi = MAX_PRESET_RM
            _bounded(st.get('weight') or 0, f"{where}: weight", hi)


class SpeedianceClient:
    def __init__(self):
        # DATA_DIR lets the web app and the MCP server share one config.json
        # (the logged-in user's token) + library cache via a mounted volume.
        self.data_dir = os.environ.get("DATA_DIR", ".")
        try:
            os.makedirs(self.data_dir, exist_ok=True)
        except Exception:
            pass
        self.config_file = os.path.join(self.data_dir, "config.json")
        self.credentials = self.load_config()
        self.region = self.credentials.get("region", "Global")
        self.device_type = int(self.credentials.get("device_type", 1))
        self.allow_monster_moves = bool(self.credentials.get("allow_monster_moves", False))
        self.base_url = "https://euapi.speediance.com" if self.region == "EU" else "https://api2.speediance.com"
        self.host = "euapi.speediance.com" if self.region == "EU" else "api2.speediance.com"
        self.library_cache_file = self._get_library_cache_file()
        self.library_cache = self._load_library_cache()
        self.last_debug_info = {}

    def _get_tz_headers(self):
        """Best-effort Timezone / UTC offset headers similar to the mobile app."""
        # Prefer Docker/OS TZ if set (e.g. 'America/Edmonton')
        tz_name = os.environ.get("TZ") or (time.tzname[0] if time.tzname else "GMT")

        # Compute local UTC offset in ±HHMM
        # time.timezone / time.altzone are seconds WEST of UTC (positive in North America)
        if time.localtime().tm_isdst and time.daylight:
            offset_seconds = -time.altzone
        else:
            offset_seconds = -time.timezone

        sign = "+" if offset_seconds >= 0 else "-"
        offset_seconds = abs(offset_seconds)
        hh = offset_seconds // 3600
        mm = (offset_seconds % 3600) // 60
        utc_offset = f"{sign}{hh:02d}{mm:02d}"

        return {
            "Timezone": tz_name,
            "Utc_offset": utc_offset
        }

    def _get_library_cache_file(self):
        allow_flag = 1 if self.allow_monster_moves else 0
        return os.path.join(self.data_dir,
                            f"library_cache_v2_device{self.device_type}_allow{allow_flag}.json")

    def _load_library_cache(self):
        """Loads library from disk if available."""
        if os.path.exists(self.library_cache_file):
            try:
                with open(self.library_cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error loading library cache: {e}")
        return None

    def _save_library_cache(self, data):
        """Saves library to disk."""
        try:
            with open(self.library_cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving library cache: {e}")

    def _request(self, method, url, **kwargs):
        """Wrapper for requests to capture debug info."""
        kwargs.setdefault("timeout", REQUEST_TIMEOUT_S)  # a stalled vendor must not hold the only worker forever
        kwargs.setdefault("allow_redirects", False)
        try:
            resp = requests.request(method, url, **kwargs)
            
            # Capture debug info
            try:
                body_preview = resp.json()
            except:
                body_preview = resp.text[:500] + "..." if len(resp.text) > 500 else resp.text

            self.last_debug_info = {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "method": method,
                "url": url,
                "status": resp.status_code,
                "request_headers": dict(resp.request.headers),
                "request_body": kwargs.get('json') or kwargs.get('data'),
                "response_body": body_preview
            }
            
            # Check for application-level auth error (Code 91)
            if isinstance(body_preview, dict) and body_preview.get('code') == 91:
                raise Exception("Unauthorized")
                
            return resp
        except Exception as e:
            self.last_debug_info = {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "method": method,
                "url": url,
                "error": str(e)
            }
            raise e

    def load_config(self):
        if os.path.exists(self.config_file):
            with open(self.config_file, 'r') as f:
                return json.load(f)
        return {
            "user_id": "",
            "token": "",
            "region": "Global",
            "unit": 0,
            "custom_instruction": "",
            "device_type": 1,
            "allow_monster_moves": False,
            "owned_accessories": [],
            "owned_devices": [],
        }

    def save_config(self, user_id, token, region="Global", unit=0, custom_instruction="", device_type=1, allow_monster_moves=False, owned_accessories=None, owned_devices=None):
        self.credentials = {
            "user_id": user_id,
            "token": token,
            "region": region,
            "unit": int(unit),
            "custom_instruction": custom_instruction,
            "device_type": int(device_type),
            "allow_monster_moves": bool(allow_monster_moves),
            "owned_accessories": owned_accessories or [],
            "owned_devices": owned_devices or [],
        }
        self.region = region
        self.device_type = int(device_type)
        self.allow_monster_moves = bool(allow_monster_moves)
        self.host = "euapi.speediance.com" if self.region == "EU" else "api2.speediance.com"
        self.base_url = "https://" + self.host
        self.library_cache_file = self._get_library_cache_file()
        self.library_cache = self._load_library_cache()
        write_json_private(self.config_file, self.credentials)  # atomic, 0600: it holds the vendor session token

    def update_unit(self, unit):
        """Updates the unit setting on the server (0=Metric, 1=Imperial)"""
        url = f"{self.base_url}/api/app/userinfo"
        payload = {"unit": int(unit)}
        try:
            resp = self._request('PUT', url, headers=self._get_headers(), json=payload)
            if resp.status_code == 200:
                try:
                    _require_ok(resp, "unit update")
                except UpstreamError as e:
                    return False, str(e)
                # Update local config
                self.save_config(
                    self.credentials.get("user_id"),
                    self.credentials.get("token"),
                    self.credentials.get("region"),
                    unit,
                    self.credentials.get("custom_instruction", ""),
                    self.credentials.get("device_type", 1),
                    self.credentials.get("allow_monster_moves", False),
                    self.credentials.get("owned_accessories", []),
                    self.credentials.get("owned_devices", []),
                )
                return True, "Unit updated successfully"
            else:
                return False, f"Failed to update unit: {resp.text}"
        except Exception as e:
            return False, str(e)

    def login(self, email, password):
        # Common headers for login requests
        headers = {
            "Host": self.host,
            "User-Agent": "Dart/3.9 (dart:io)",
            "Content-Type": "application/json",
            "Timestamp": str(int(time.time() * 1000)),
            **self._get_tz_headers(),
            "Versioncode": "40304",
            "Mobiledevices": '{"brand":"google","device":"emulator64_x86_64_arm64","deviceType":"sdk_gphone64_x86_64","os":"","os_version":"31","manufacturer":"Google"}',
            "Accept-Language": "en",
            "App_type": "SOFTWARE",
            "Connection": "keep-alive",
            "Accept-Encoding": "gzip, deflate, br"
        }

        # Step 1: Verify Identity
        verify_url = f"{self.base_url}/api/app/v2/login/verifyIdentity"
        verify_payload = {"type": 2, "userIdentity": email}
        
        try:
            resp = self._request('POST', verify_url, json=verify_payload, headers=headers)
            if resp.status_code != 200:
                return False, "Verify failed", f"Status: {resp.status_code}\nResponse: {resp.text}"
            
            verify_data = resp.json().get('data', {})
            if verify_data.get('isExist') is False:
                return False, "Account does not exist. Please register using the official Speediance mobile app first.", None
            
            if verify_data.get('hasPwd') is False:
                return False, "Account exists but has no password set. Please set a password in the Speediance mobile app.", None

            # Step 2: ByPass (Login with password)
            bypass_url = f"{self.base_url}/api/app/v2/login/byPass"
            bypass_payload = {"userIdentity": email, "password": password, "type": 2}
            
            resp = self._request('POST', bypass_url, json=bypass_payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json().get('data', {})
                token = data.get('token')
                user_id = data.get('appUserId')
                
                if token and user_id:
                    self.save_config(
                        str(user_id),
                        token,
                        self.region,
                        self.credentials.get('unit', 0),
                        self.credentials.get('custom_instruction', ''),
                        self.credentials.get('device_type', 1),
                        self.credentials.get('allow_monster_moves', False),
                        self.credentials.get('owned_accessories', []),
                        self.credentials.get('owned_devices', []),
                    )
                    return True, "Login successful", None
                return False, "Token or appUserId not found in response", f"Response: {resp.text}"
            else:
                return False, "Login failed", f"Status: {resp.status_code}\nResponse: {resp.text}"
                
        except Exception as e:
            return False, "Connection Error", str(e)

    def logout(self):
        url = f"{self.base_url}/api/app/login/logout"
        headers = self._get_headers()
        
        try:
            self._request('POST', url, headers=headers)
        except Exception as e:
            print(f"Logout error: {e}")
        
        # Clear credentials but keep region/unit/instructions
        self.save_config(
            "",
            "",
            self.region,
            self.credentials.get('unit', 0),
            self.credentials.get('custom_instruction', ''),
            self.credentials.get('device_type', 1),
            self.credentials.get('allow_monster_moves', False),
            self.credentials.get('owned_accessories', []),
            self.credentials.get('owned_devices', []),
        )
        return True

    def _get_headers(self):
        return {
            "Host": self.host,
            "App_user_id": self.credentials.get("user_id", ""),
            "Token": self.credentials.get("token", ""),
            "Timestamp": str(int(time.time() * 1000)),
            **self._get_tz_headers(),
            "Versioncode": "40304",
            "App_type": "SOFTWARE",
            "Mobiledevices": '{"brand":"google","device":"emulator64_x86_64_arm64","deviceType":"sdk_gphone64_x86_64","os":"","os_version":"31","manufacturer":"Google"}',
            "Content-Type": "application/json",
            "User-Agent": "Dart/3.9 (dart:io)"
        }

    def get_categories(self):
        """Fetches the list of exercise categories (tabs)."""
        def fetch_categories(device_type):
            url = f"{self.base_url}/api/app/actionLibraryTab/list?deviceType={device_type}"
            resp = self._request('GET', url, headers=self._get_headers())
            return resp.json().get('data', [])

        try:
            if self.device_type == 2 and self.allow_monster_moves:
                cat_pal = fetch_categories(2)
                cat_monster = fetch_categories(1)
                merged = {}
                for cat in cat_pal + cat_monster:
                    name_key = (cat.get('name') or '').strip().lower()
                    if not name_key:
                        continue
                    entry = merged.setdefault(name_key, {"name": cat.get("name"), "ids": []})
                    entry["ids"].append(cat.get("id"))

                merged_list = []
                for entry in merged.values():
                    ids = [cid for cid in entry["ids"] if cid is not None]
                    if not ids:
                        continue
                    merged_list.append({
                        "id": ids[0],
                        "name": entry["name"],
                        "filter_ids": ",".join(str(cid) for cid in ids),
                    })
                return merged_list

            categories = fetch_categories(self.device_type)
            for cat in categories:
                cat["filter_ids"] = str(cat.get("id"))
            return categories
        except Exception as e:
            print(f"Error fetching categories: {e}")
            return []

    def get_library(self):
        if self.library_cache:
            return self.library_cache
            
        def fetch_categories(device_type):
            url = f"{self.base_url}/api/app/actionLibraryTab/list?deviceType={device_type}"
            resp = self._request('GET', url, headers=self._get_headers())
            return resp.json().get('data', [])

        try:
            # 1. Fetch all categories
            if self.device_type == 2 and self.allow_monster_moves:
                categories_by_device = {
                    2: fetch_categories(2),
                    1: fetch_categories(1),
                }
            else:
                categories_by_device = {self.device_type: self.get_categories()}

            all_basic_exercises = []

            # 2. Fetch exercises for each category
            for device_type, categories in categories_by_device.items():
                for category in categories:
                    tab_id = category['id']
                    url = f"{self.base_url}/api/app/actionLibraryGroup/trainingPartGroup?tabId={tab_id}&deviceTypeList={device_type}"
                    try:
                        resp = self._request('GET', url, headers=self._get_headers())
                        if resp.status_code == 200:
                            data = resp.json().get('data', [])
                            for muscle_group in data:
                                for action in muscle_group.get('actionLibraryGroupList', []):
                                    # Tag with category info and device source
                                    action['category_id'] = tab_id
                                    action['category_name'] = category['name']
                                    action['device_type'] = device_type
                                    all_basic_exercises.append(action)
                    except Exception as e:
                        print(f"Error fetching category {tab_id}: {e}")

            # 3. Deduplicate by ID (keep first occurrence)
            unique_exercises = {}
            for ex in all_basic_exercises:
                if ex['id'] not in unique_exercises:
                    ex['device_type_list'] = [ex.get('device_type')]
                    unique_exercises[ex['id']] = ex
                else:
                    existing = unique_exercises[ex['id']]
                    current = set(existing.get('device_type_list', [existing.get('device_type')]))
                    current.add(ex.get('device_type'))
                    existing['device_type_list'] = sorted(t for t in current if t)
            
            all_ids = list(unique_exercises.keys())
            detailed_library = []
            chunk_size = 50
            
            # 4. Fetch details in batches
            for i in range(0, len(all_ids), chunk_size):
                chunk_ids = all_ids[i:i + chunk_size]
                details = self.get_batch_details(chunk_ids)
                
                # Re-attach category info
                for d in details:
                    if d['id'] in unique_exercises:
                        original = unique_exercises[d['id']]
                        d['category_id'] = original.get('category_id')
                        d['category_name'] = original.get('category_name')
                        device_types = original.get('device_type_list', [original.get('device_type')])
                        d['device_type_list'] = device_types
                        d['device_type_tag'] = ",".join(str(t) for t in device_types if t)
                
                detailed_library.extend(details)
            
            self.library_cache = detailed_library
            self._save_library_cache(detailed_library)
            return detailed_library

        except Exception as e:
            if str(e) == "Unauthorized": raise e
            print(f"Error fetching library: {e}")
        return []
    
    def get_accessories(self):
        url = f"{self.base_url}/api/app/accessories/list"
        try:
            resp = self._request('GET', url, headers=self._get_headers())
            return resp.json().get('data', [])
        except Exception as e:
            if str(e) == "Unauthorized": raise e
            print(f"Error fetching accessories: {e}")
            return []
        
    def get_workout_detail(self, code):
        url = f"{self.base_url}/api/app/v3/customTrainingTemplate/detailByCode?code={_q(code)}"
        resp = self._request('GET', url, headers=self._get_headers())
        return _json_data(resp, "template detail", dict)  # {} = no such template; an outage raises

    def get_user_workouts(self):
        url = f"{self.base_url}/api/app/v4/customTrainingTemplate/appPage?pageNo=1&pageSize=-1&deviceTypes={self.device_type}"
        resp = self._request('GET', url, headers=self._get_headers())
        if resp.status_code == 401:
            raise Exception("Unauthorized")
        return resp.json().get('data', [])

    def delete_workout(self, template_id):
        url = f"{self.base_url}/api/app/customTrainingTemplate?ids={_q(template_id, ',')}"
        resp = self._request('DELETE', url, headers=self._get_headers())
        _require_ok(resp, "delete")

    def get_exercise_detail(self, exercise_id, strict=False):
        """strict=True (the save path) raises on a vendor error instead of returning {} (which reads as "bilateral")."""
        url = f"{self.base_url}/api/app/actionLibraryGroup/{_q(exercise_id)}?isDisplay=1"
        resp = self._request('GET', url, headers=self._get_headers())
        if strict:
            return _json_data(resp, "exercise detail", dict)
        return resp.json().get('data', {})

    def is_exercise_unilateral(self, group_id, strict=False):
        detail = self.get_exercise_detail(group_id, strict=strict)
        return detail.get('isLeftRight') == 1

    def get_batch_details(self, group_ids, strict=False):
        if not group_ids:
            return []
        query_parts = [f"ids={_q(gid)}" for gid in group_ids]
        query_str = "&".join(query_parts)
        url = f"{self.base_url}/api/app/actionLibraryGroup/list?{query_str}"

        if strict:  # the save path: an outage must not look like "no exercises" and wipe a template
            resp = self._request('GET', url, headers=self._get_headers())
            return _json_data(resp, "exercise library", list)
        try:
            resp = self._request('GET', url, headers=self._get_headers())
            return resp.json().get('data', [])
        except Exception as e:
            if str(e) == "Unauthorized": raise e
            print(f"Error fetching batch details: {e}")
            return []

    def get_calendar_month(self, date_str):
        """
        Fetches calendar data for a specific month.
        date_str: 'YYYY-MM'
        """
        url = f"{self.base_url}/api/app/v5/trainingCalendar/monthNew?date={_q(date_str)}&selectedDeviceType={self.device_type}"
        try:
            resp = self._request('GET', url, headers=self._get_headers())
        except Exception as e:
            if str(e) == "Unauthorized":
                raise
            raise UpstreamError(f"calendar fetch failed ({type(e).__name__})") from None
        return _json_data(resp, "calendar fetch", list)

    def schedule_workout(self, date_str, template_code, status):
        """
        Schedules or unschedules a workout.
        status: 1 to add, 0 to remove
        """
        url = f"{self.base_url}/api/app/templateReservation"
        payload = {
            "status": status,
            "deviceType": self.device_type,
            "thatDay": date_str,
            "templateCode": template_code
        }
        try:
            resp = self._request('POST', url, headers=self._get_headers(), json=payload)
            if resp.status_code == 401:
                raise Exception("Unauthorized")
            return resp.json().get('data', False)
        except Exception as e:
            if str(e) == "Unauthorized": raise e
            print(f"Error scheduling workout: {e}")
            return False

    def get_training_history(self, start_date, end_date):
        """
        Fetches the list of training records between two dates.
        Dates should be in 'YYYY-MM-DD' format.
        """
        url = f"{self.base_url}/api/mobile/v2/report/userTrainingDataRecord?startDate={_d(start_date)}&endDate={_d(end_date)}"
        try:
            resp = self._request('GET', url, headers=self._get_headers())
            return _json_data(resp, "training history", list)
        except Exception as e:
            # Do NOT return []: callers read an empty list as "no training" ("well rested") during an outage.
            print(f"Error fetching history: {type(e).__name__}")
            raise

    def schedule_course(self, date_str, course_id, status):
        """
        Schedules or unschedules an official course.
        status: 1 to add, 0 to remove
        """
        url = f"{self.base_url}/api/app/courseReservation"
        payload = {
            "status": status,
            "deviceType": self.device_type,
            "thatDay": date_str,
            "courseId": course_id
        }
        try:
            resp = self._request('POST', url, headers=self._get_headers(), json=payload)
            self.last_debug_info = {
                "url": url, "method": "POST",
                "request_body": payload,
                "response_body": resp.json() if resp.content else None,
                "status": resp.status_code,
            }
            if resp.status_code == 401:
                raise Exception("Unauthorized")
            return resp.json().get('data', False)
        except Exception as e:
            if str(e) == "Unauthorized": raise e
            print(f"Error scheduling course: {e}")
            return False

    def save_workout(self, name, exercises, template_id=None):
        """
        Speichert (ohne ID) oder Aktualisiert (mit ID).
        Behebt den 'Parameter Error' durch saubere Trennung von weights und counterweight2.
        """
        
        imperial = bool(self.credentials.get("unit"))
        validate_workout(name, exercises, imperial)
        exercises = [{**ex, "groupId": _group_id(ex.get("groupId"), "groupId")} for ex in exercises]
        group_ids = sorted({ex['groupId'] for ex in exercises})
        details = self.get_batch_details(group_ids, strict=True)
        
        id_map = {}
        for d in details:
            if d.get('actionLibraryList'):
                id_map[str(d['id'])] = d['actionLibraryList'][0]['id']
        
        action_library_list = []
        total_capacity = 0

        unilateral_check = {}
        for group_id in group_ids:
            unilateral_check[group_id] = self.is_exercise_unilateral(group_id, strict=True)

        for ex in exercises:
            group_id = int(ex['groupId'])
            sets = ex['sets']
            raw_preset_id = ex.get('preset_id')
            preset_id = int(raw_preset_id) if raw_preset_id is not None else -1
            data_stat_type = int(ex.get('data_stat_type') or 0)
            
            is_unilateral = unilateral_check.get(group_id, False)

            user_variant_id = ex.get('variant_id')
            real_variant_id = int(user_variant_id) if user_variant_id and str(user_variant_id).isdigit() else id_map.get(str(ex['groupId']))
            
            if not real_variant_id:  # dropping it silently would save (or, on an update, overwrite) a smaller workout
                raise ValueError(f"exercise {group_id}: not found in the exercise library")

            # Arrays für CSV (IMPORTANT: must be same length)
            reps_list = []
            weights_list = []  # only for custom
            counter_list = []  # only for preset
            break_list = []
            mode_list = []
            left_right_list = []
            level_list = []
            completion_list = []
            completion_method_list = []
            count_type_list = []
            
            set_capacity = 0

            for i, s in enumerate(sets):
                reps = int(s.get('reps') or 0)
                weight_val = float(s.get('weight') or 0)
                mode = int(s.get('mode') or 1)
                rest = int(s.get('rest') if s.get('rest') is not None else 60)  # 0 s rest is legitimate
                unit = str(s.get('unit', 'reps')).lower()
                one_cable_max = MAX_WEIGHT_LB_UNILATERAL if imperial else MAX_WEIGHT_KG_UNILATERAL
                if is_unilateral and preset_id == -1 and data_stat_type != 6 and weight_val > one_cable_max:
                    raise ValueError(f"exercise {group_id}: a single-cable load must be at most {one_cable_max:g} "
                                     f"{'lb' if imperial else 'kg'}")

                # Unilateral Logic
                if is_unilateral:
                    left_right_list.append("1" if i % 2 == 0 else "2")
                else:
                    left_right_list.append("0")

                reps_list.append(str(reps))
                break_list.append(str(rest))
                mode_list.append(str(mode))

                # Vita exercises (dataStatType==6): weight input = difficulty level (1-10)
                if data_stat_type == 6:
                    level_list.append(str(max(1, min(10, int(weight_val) or 1))))
                else:
                    level_list.append("0")

                # Completion fields: required by API (observed in app payloads)
                # - unit=='sec' => time-based completion
                # - unit=='reps' => rep-based completion
                if unit == 'sec':
                    completion_method_list.append("2")
                    count_type_list.append("2")
                else:
                    completion_method_list.append("1")
                    count_type_list.append("1")
                completion_list.append("1")

                # Weights vs counters
                if data_stat_type == 6:
                    # Vita: no cable weight, level already captured above
                    weights_list.append("0")
                elif preset_id == -1:
                    api_weight = weight_val  # JS already converted LBS→KG before sending
                    weights_list.append(f"{api_weight:.1f}")
                    set_capacity += (reps * api_weight)
                else:
                    # For presets, we MUST populate weights_list with dummy values (e.g. 3.5)
                    # AND populate counter_list with the RM value.
                    weights_list.append("3.5")
                    counter_list.append(str(int(weight_val)))
                    set_capacity += (reps * weight_val * 2.2)

            total_capacity += set_capacity

            final_weights = ",".join(weights_list)
            final_counter = ",".join(counter_list) if preset_id != -1 else ""

            action_obj = {
                # Required identifiers
                "groupId": group_id,
                "actionLibraryId": int(real_variant_id),

                "templatePresetId": preset_id,

                # Per-set CSV
                "setsAndReps": ",".join(reps_list),

                # Some backends expect both fields present
                "breakTime": ",".join(break_list),
                "breakTime2": ",".join(break_list),

                "sportMode": ",".join(mode_list),
                "leftRight": ",".join(left_right_list),

                # Completion-related
                "selectCompletionMethod": ",".join(completion_list),
                "completionMethod": ",".join(completion_method_list),
                "countType": ",".join(count_type_list),

                # Weights
                "weights": final_weights,
                "counterweight2": final_counter,
                "counterweight": final_counter, # Try sending both counterweight and counterweight2

                "level": ",".join(level_list),
                "capacity": set_capacity,
            }
            action_library_list.append(action_obj)

        payload = {
            "name": name,
            "actionLibraryList": action_library_list,
            "totalCapacity": total_capacity,
            "deviceType": self.device_type,
            "bgColor": 0
        }

        if template_id:
            payload['id'] = int(template_id)

        url = f"{self.base_url}/api/app/v2/customTrainingTemplate"
        resp = self._request('POST', url, headers=self._get_headers(), json=payload)
        if resp.status_code == 401:
            raise Exception("Unauthorized")
        if resp.status_code != 200:  # a gateway error can carry a success-looking body
            raise UpstreamError(f"save failed (HTTP {resp.status_code})")
        try:
            return resp.json()
        except ValueError:
            raise UpstreamError("save response was not JSON") from None

    def get_training_records(self, start_date, end_date):
        """Fetches training session records for a date range.
        GET /api/mobile/v2/report/userTrainingDataRecord?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD
        """
        url = f"{self.base_url}/api/mobile/v2/report/userTrainingDataRecord?startDate={_d(start_date)}&endDate={_d(end_date)}"
        try:
            resp = self._request('GET', url, headers=self._get_headers())
            return _json_data(resp, "training records", list)
        except Exception as e:
            if str(e) == "Unauthorized": raise e
            print(f"Error fetching training records: {type(e).__name__}")
            raise

    def get_training_stats(self, start_date, end_date):
        """Fetches aggregated training stats for a date range.
        GET /api/mobile/v2/report/userTrainingDataStat?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD
        """
        url = f"{self.base_url}/api/mobile/v2/report/userTrainingDataStat?startDate={_d(start_date)}&endDate={_d(end_date)}"
        try:
            resp = self._request('GET', url, headers=self._get_headers())
            return _json_data(resp, "training stats", dict)
        except Exception as e:
            if str(e) == "Unauthorized": raise e
            print(f"Error fetching training stats: {type(e).__name__}")
            raise

    def get_training_detail(self, training_id, training_type):
        """Fetches detailed info for a completed training session.
        training_type determines the endpoint:
          - Official Courses → /api/app/trainingInfo/courseTrainingInfoDetail/{id}
          - Custom Templates → /api/app/trainingInfo/cttTrainingInfoDetail/{id}
        """
        if training_type == 'course':
            url = f"{self.base_url}/api/app/trainingInfo/courseTrainingInfoDetail/{_q(training_id)}"
        else:
            url = f"{self.base_url}/api/app/trainingInfo/cttTrainingInfoDetail/{_q(training_id)}"
        try:
            resp = self._request('GET', url, headers=self._get_headers())
            return _json_data(resp, "training detail", (dict, list), empty=dict)
        except Exception as e:
            if str(e) == "Unauthorized": raise e
            print(f"Error fetching training detail: {type(e).__name__}")
            raise  # an empty detail would read as "a workout with no sets"

    def get_training_session_info(self, training_id):
        """Fetches overall session info (name, duration, calories, capacity).
        GET /api/app/trainingInfo/courseTrainingInfo/{id}
        """
        url = f"{self.base_url}/api/app/trainingInfo/courseTrainingInfo/{_q(training_id)}"
        try:
            resp = self._request('GET', url, headers=self._get_headers())
            if resp.status_code == 401:
                raise Exception("Unauthorized")
            return resp.json().get('data', {})
        except Exception as e:
            if str(e) == "Unauthorized": raise e
            print(f"Error fetching training session info: {e}")
            return {}

    def get_user_action_stats(self, group_id, page=1, size=12):
        """Fetches historical statistics for a specific exercise group."""
        url = f"{self.base_url}/api/app/actionLibraryGroup/userActionStatPage?id={group_id}&pageNo={page}&pageSize={size}"
        resp = self._request('GET', url, headers=self._get_headers())
        _require_ok(resp, "exercise stats")
        return resp.json()

    # ── Browse: Courses & Programs ──────────────────────────────

    def get_courses_page(self, page=1, page_size=200):
        """Fetches a page of courses (max 200 per page).
        GET /api/app/v2/course/page?pageNo={page}&pageSize={page_size}
        """
        url = f"{self.base_url}/api/app/v2/course/page?pageNo={page}&pageSize={page_size}"
        resp = self._request('GET', url, headers=self._get_headers())
        return _json_data(resp, "courses page", list)

    def get_course_detail(self, course_id):
        """Fetches full course detail including exercise list.
        GET /api/app/v2/course/info/{id}?weightConfig=1
        """
        url = f"{self.base_url}/api/app/v2/course/info/{_q(course_id)}?weightConfig=1"
        resp = self._request('GET', url, headers=self._get_headers())
        return _json_data(resp, "course detail", dict)

    def get_programs_page(self, page=1, page_size=200):
        """Fetches a page of programs.
        GET /api/mobile/exclusivePlan/page?pageNo={page}&pageSize={page_size}
        """
        url = f"{self.base_url}/api/mobile/exclusivePlan/page?pageNo={page}&pageSize={page_size}"
        resp = self._request('GET', url, headers=self._get_headers())
        return _json_data(resp, "programs page", list)

    def get_program_detail(self, plan_id):
        """Fetches full program detail including week/day structure.
        GET /api/app/exclusivePlan/{id}
        """
        url = f"{self.base_url}/api/app/exclusivePlan/{_q(plan_id)}"
        resp = self._request('GET', url, headers=self._get_headers())
        return _json_data(resp, "program detail", dict)
