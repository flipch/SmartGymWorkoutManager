"""pytest config: never collect the live-account e2e test unless explicitly asked.

test_e2e_workouts.py creates and deletes real templates on a real Speediance account.
Run it only deliberately:  GYM_E2E=1 pytest test_e2e_workouts.py
"""
import os

collect_ignore = [] if os.environ.get("GYM_E2E") == "1" else ["test_e2e_workouts.py"]
