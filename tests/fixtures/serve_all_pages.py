"""Throwaway development server that renders every page of the UI with canned, realistic-looking data.

    .venv/bin/python -B tests/fixtures/serve_all_pages.py [port]      # default 5099, 127.0.0.1 only

Nothing real is involved: a scratch DATA_DIR, a dummy 32 character GYM_API_TOKEN, and every vendor-facing method of the
client replaced by a function that returns made-up data (images are inline SVG data URIs, so there is no network use for
media). Open pages with HTTP Basic auth: any user name, the password printed on start. It exists to look at the UI in a
browser; it is not part of the deployed app and no test imports it.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from urllib.parse import quote

DUMMY_TOKEN = "dummy-token-for-ui-fixture-00000"  # 32 characters, not a credential
os.environ["GYM_API_TOKEN"] = DUMMY_TOKEN
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="gym-ui-fixture-")
os.environ.pop("GYM_AUTH_DISABLED", None)
os.environ["GYM_ALLOW_WRITES"] = ""
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

import app as gym_app  # noqa: E402
import features  # noqa: E402

TODAY = date.today()


def pic(label, hue):
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' width='320' height='200'><rect width='320' height='200' fill='hsl({hue},45%,38%)'/>"
           f"<text x='160' y='108' font-size='28' text-anchor='middle' fill='white' font-family='sans-serif'>{label}</text></svg>")
    return "data:image/svg+xml;utf8," + quote(svg)


EXERCISES = [
    ("Barbell Bench Press", 11, "Chest", 1, True), ("Incline Dumbbell Press", 11, "Chest", 1, False),
    ("Cable Fly", 11, "Chest", 1, False), ("Lat Pulldown", 13, "Back", 1, False), ("Seated Cable Row", 13, "Back", 1, False),
    ("Overhead Press", 12, "Shoulders", 1, True), ("Lateral Raise", 12, "Shoulders", 1, False),
    ("Biceps Curl", 16, "Arms", 1, False), ("Triceps Pushdown", 16, "Arms", 1, False),
    ("Back Squat", 15, "Legs", 1, True), ("Romanian Deadlift", 15, "Legs", 1, True), ("Leg Curl", 15, "Legs", 2, False),
    ("Cable Crunch", 17, "Core", 1, False), ("A name that is long enough to wrap onto a second line in a narrow card", 17, "Core", 2, False),
]
ACCESSORIES = [  # the vendor lists one entry per machine generation, so names repeat
    {"id": 1, "name": "Handle", "icon": pic("H", 200), "device": "Gym Monster"}, {"id": 2, "name": "Handle", "icon": pic("H", 210), "device": "Gym Monster 2"},
    {"id": 3, "name": "Barbell", "icon": pic("B", 20), "device": "Gym Monster"}, {"id": 4, "name": "Barbell", "icon": pic("B", 30), "device": "Gym Monster 2"},
    {"id": 5, "name": "Ankle strap", "icon": pic("A", 120), "device": "Gym Monster"},
]


def library():
    out = []
    for i, (title, cat, cat_name, dev, barbell) in enumerate(EXERCISES, start=1):
        out.append({"id": i, "title": title, "img": pic(title.split()[0], 160 + i * 14), "category_id": cat, "category_name": cat_name,
                    "device_type": dev, "device_type_tag": dev, "accessories": f"{1 + i % 5},{1 + (i + 1) % 5}", "variants": [],
                    "isBarbell": barbell, "mainMuscleGroupName": cat_name, "auxiliaryMuscleGroupList": [{"name": "Triceps"}, {"name": "Core"}],
                    "trainingPartId": cat})
    return out


CATEGORIES = [{"id": 11, "name": "Chest", "filter_ids": "11"}, {"id": 13, "name": "Back", "filter_ids": "13"},
              {"id": 12, "name": "Shoulders", "filter_ids": "12"}, {"id": 15, "name": "Legs", "filter_ids": "15"}]

WORKOUTS = [
    {"id": 100 + n, "code": f"CODE{n}", "name": name, "totalCapacity": 3000 + n * 410, "durationMinute": 35 + n * 4, "actionNum": 4 + n,
     "actionInfoList": [[{"img": pic(str(g), 180 + g * 25)}] for g in range(1, 5 + n % 3)]}
    for n, name in enumerate(["Push day", "Pull day", "Leg day", "Full body, travel edition", "Upper / lower split with a very long template name"], start=1)
]


def calendar_month(date_str):
    year, month = (int(p) for p in date_str.split("-")[:2])
    first = date(year, month, 1)
    days = []
    d = first
    while d.month == month:
        plans = []
        if d.day % 5 == 0:
            plans.append({"title": "Push day", "code": "CODE1", "isReservation": d >= TODAY})
        if d.day % 7 == 3:
            plans.append({"title": "Leg day", "code": "CODE3", "isReservation": d >= TODAY})
        if d.day % 11 == 2 and d > TODAY:
            plans.append({"title": "Program: Strength 8", "isReservation": False, "exclusivePlanName": "Strength 8"})
        if d < TODAY and d.day % 6 == 0:
            plans.append({"title": "Pull day", "code": "CODE2", "isReservation": False})
        days.append({"date": d.isoformat(), "trainingPlanList": plans})
        d += timedelta(days=1)
    return days


def history_records(start=None, end=None):
    out = []
    for n in range(0, 40):
        day = TODAY - timedelta(days=n * 2 + (n % 3))
        stamp = int(datetime(day.year, day.month, day.day, 18, 30).timestamp() * 1000)
        out.append({"trainingId": 5000 + n, "title": ["Push day", "Pull day", "Leg day", "Strength 8: Week 2", "Quick burn"][n % 5],
                    "type": [5, 5, 5, 2, 7][n % 5], "startTimestamp": stamp, "endTimestamp": stamp + 3600 * 1000,
                    "trainingTime": 2700 + n * 20, "calorie": 280 + n * 3, "totalCapacity": 4100 + n * 55,
                    "courseImg": pic("C", 190 + n * 7) if n % 5 == 3 else ""})
    return out


def detail_session(training_id, training_type):
    return [{"title": t, "actionLibraryId": i, "setsAndReps": "10,10,8", "weights": "40,40,45", "leftWeights": "", "rightWeights": "",
             "finishedCount": 3, "targetCount": 3, "capacity": 1200, "img": pic(t.split()[0], 150 + i * 20)}
            for i, t in enumerate(["Barbell Bench Press", "Incline Dumbbell Press", "Cable Fly"], start=1)]


COURSES = [{"id": 900 + n, "courseTitle": t, "courseImg": pic("Course", 160 + n * 12), "durationMinute": 20 + n * 5, "difficultyId": 1 + n % 4,
            "categoryList": [{"categoryName": c}], "trainedUserCount": 1200 * n, "isNew": n % 3 == 0, "isHot": n % 4 == 0}
           for n, (t, c) in enumerate([("Full-body strength", "Strength"), ("Glute focus", "Lower body"), ("Core in 20", "Core"),
                                       ("Upper-body pump", "Strength"), ("Mobility flow", "Yoga"), ("HIIT burner", "Cardio")], start=1)]
PROGRAMS = [{"id": 700 + n, "name": t, "description": "Eight weeks, three sessions a week.", "coverImg": pic("Plan", 20 + n * 30),
             "difficultyId": 1 + n % 3, "weekCount": 8, "weekTrainingFrequency": 3, "isHot": n == 1}
            for n, t in enumerate(["Strength 8", "Lean & strong", "Home gym basics"], start=1)]

INSIGHTS_DEEP = {
    "detail_sessions": 12, "detail_weeks": 5, "sessions_failed": 0, "partial": False,
    "data_note": "Per-muscle & strength metrics from 12 detailed sessions over 5 week(s).",
    "muscle_volume": [{"muscle": m, "sets_per_wk": s, "mev": 8, "mav": 14, "mrv": 20, "status": st, "freq_per_wk": f}
                      for m, s, st, f in [("chest", 12.5, "in_range", 2.0), ("back", 17.0, "in_range", 2.4), ("quads", 5.5, "under_mev", 1.0),
                                          ("shoulders", 22.0, "over_mrv", 3.0), ("biceps", 9.0, "in_range", 1.6)]],
    "untrained_muscles": ["calves", "forearms"],
    "lifts": [{"name": n, "e1rm_now": v, "e1rm_delta": d, "trend": t, "e1rm_series": [{"e1rm": v - d + k * d / 4 + (k % 2)} for k in range(5)]}
              for n, v, d, t in [("Barbell Bench Press", 92.5, 7.5, "up"), ("Back Squat", 120.0, -2.5, "down"), ("Overhead Press", 55.0, 0.0, "flat")]],
    "balance": {"push_sets": 34, "pull_sets": 22, "push_pull_ratio": 1.55, "upper_sets": 70, "lower_sets": 18, "verdict": "push_biased"},
    "frequency": {"muscles_below_2x": ["quads", "calves"]},
    "weekly": [{"week": f"2026-W{40 + k}", "sessions": 3, "tonnage": 9000 + k * 1500, "calories": 900, "minutes": 150} for k in range(6)],
    "prs": [{"name": "Back Squat", "weight": 140}, {"name": "Barbell Bench Press", "weight": 100}, {"name": "Overhead Press", "weight": 60}],
}


def install(client):
    client.credentials = {"token": "fixture", "user_id": "1", "unit": 0, "unit_confirmed": True, "region": "Global", "device_type": 1,
                          "allow_monster_moves": False, "owned_accessories": [], "owned_devices": [], "custom_instruction": ""}
    client.device_type, client.allow_monster_moves = 1, False
    client.get_library = lambda strict=False: library()
    client.get_categories = lambda strict=False: [dict(c) for c in CATEGORIES]
    client.get_accessories = lambda: [dict(a) for a in ACCESSORIES]
    client.get_user_workouts = lambda: [dict(w) for w in WORKOUTS]
    client.get_calendar_month = calendar_month
    client.get_training_records = lambda s, e: history_records()
    client.get_training_history = lambda s, e: history_records()
    client.get_training_stats = lambda s, e: {"trainingTime": 3600 * 22, "calorie": 12500, "totalCapacity": 190000}
    client.get_training_detail = lambda tid, kind: detail_session(tid, kind)
    client.get_training_session_info = lambda tid: {"trainingTime": 3100, "calorie": 410, "totalCapacity": 4800}
    client.get_courses_page = lambda page=1, size=200: COURSES if page == 1 else []
    client.get_programs_page = lambda page=1, size=200: PROGRAMS if page == 1 else []
    client.get_course_detail = lambda cid: {"estimatedCalorie": 320, "estimatedCapacity": 3500, "actionLibraryList": [
        {"title": "Squat", "img": pic("Squat", 30), "setsAndReps": "10,10,10", "recommendedWeight": 40, "completionMethod": 1, "mainMuscleGroupName": "Legs"}]}
    client.get_program_detail = lambda pid: {"name": "Strength 8", "description": "Eight weeks.", "weeks": []}
    client.get_exercise_detail = lambda eid, strict=False: {
        "title": EXERCISES[(eid - 1) % len(EXERCISES)][0], "accessories": "1,3", "mainMuscleGroupName": "Chest", "mainMuscleGroupConfigId": 11,
        "auxiliaryMuscleGroupList": [{"name": "Triceps"}], "recommendedWeight": 40, "metValue": 5, "isBarbell": True,
        "actionLibraryList": [{"coach": "Coach A", "videoPath": "", "leftVideo": ""}],
        "breathingRate": "Exhale on the push.", "motionFeeling": "Chest stretch at the bottom.", "errorCorrection": "Keep the wrists stacked.",
        "outPosition": "Elbows at 45 degrees.", "foldingStoolAngle": "30", "context": "A horizontal press.",
        "showDetails": '[{"context": "Lie back and set your feet.", "img": ""}, {"context": "Press to lockout.", "img": ""}]'}
    client.get_workout_detail = lambda code: {"name": "Push day", "id": 101, "actionLibraryList": [
        {"groupId": 1, "actionLibraryId": 1, "title": "Barbell Bench Press", "setsAndReps": "10,10", "weights": "40,40", "level": "", "sportMode": "1,1",
         "breakTime2": "60,60", "leftRight": "0,0", "counterweight2": "", "templatePresetId": -1, "img": pic("Bench", 200)}]}
    client.logout = lambda: None
    gym_app.app.jinja_env.filters["local_cache"] = lambda url, force=False: url
    features._deep_insights = lambda days=90, max_sessions=24: INSIGHTS_DEEP


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5099
    install(gym_app.client)
    print(f"serving the UI fixture on http://127.0.0.1:{port}  (Basic auth: any user, password {DUMMY_TOKEN})", flush=True)
    gym_app.app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
