"""Synthetic Speediance payloads: invented values, real structure. Safe to commit (no personal data).

Structure (key names, nesting, types) mirrors live recordings taken 2026-10-26; tests/test_harness_contract.py
re-checks that against real recordings whenever they exist on the developer's machine.
"""
import datetime as dt
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")


def ts_ms(year, month, day, hour=18, minute=0):
    return int(dt.datetime(year, month, day, hour, minute, tzinfo=PT).timestamp() * 1000)


def history_record(training_id, start_ms, title="Synthetic Push", rtype=5, seconds=2400, capacity=5000.0):
    return {
        "aiEntryPageReq": None, "belongUserHealth": 0, "bikeType": 0, "calorie": 180.0, "courseType": 0,
        "createTime": "2026-01-01 00:00:00", "deviceType": 1, "endTime": "2026-01-01 01:00:00",
        "endTimestamp": start_ms + seconds * 1000, "healthType": 0, "id": training_id, "isStageRiding": 0,
        "mileage": 0.0, "startTime": "2026-01-01 00:00:00", "startTimestamp": start_ms,
        "templateCoverImg3": "", "templateId": 1, "title": title, "totalCapacity": capacity, "totalEnergy": 0,
        "trainingId": training_id, "trainingPartSetsInfoList": [{"trainingPartId2": 11, "sets": 3}],
        "trainingSource": 1, "trainingTime": seconds, "type": rtype, "watchType": 0,
    }


def _set_row(reps, weight, target=None, left_right=0, seconds=45):
    return {
        "breakTime": 60, "capacity": float(reps * weight), "distance": 0, "finishedCount": reps, "id": 1,
        "leftCount": 0, "leftRight": left_right, "maxHeartRate": 0, "pace": 0, "rightCount": 0,
        "selectCompletionMethod": 1, "spm": 0, "targetCount": target if target is not None else reps,
        "time": seconds,
        "trainingInfoDetail": {
            "id": 1, "uuid": "synthetic", "actionLibraryId": 1, "weights": [float(weight)] * reps,
            "leftWatts": [100.0] * reps, "rightWatts": [100.0] * reps, "leftAmplitudes": [0.5] * reps,
            "rightAmplitudes": [0.5] * reps, "leftRopeSpeeds": [1.0] * reps, "rightRopeSpeeds": [1.0] * reps,
            "leftMinRopeLengths": [10.0] * (reps * 9), "rightMinRopeLengths": [10.0] * (reps * 9),
            "leftMaxRopeLengths": [90.0] * reps, "rightMaxRopeLengths": [90.0] * reps,
            "leftFinishedTimes": [1.0] * reps, "rightFinishedTimes": [1.0] * reps,
            "leftBreakTimes": [0.0] * reps, "rightBreakTimes": [0.0] * reps,
            "leftTimestamps": [0] * reps, "rightTimestamps": [0] * reps,
            "leftWeights": [float(weight)] * reps, "rightWeights": [float(weight)] * reps,
            "groupTrainingInfoId": 1, "cttTrainingInfoId": 1, "cttActionLibraryTrainingInfoId": 1,
        },
    }


def exercise(name, sets, timed=False, left_right=0):
    """sets: list of (reps, weight) or (reps, weight, target)."""
    rows = [_set_row(s[0], s[1], s[2] if len(s) > 2 else None, left_right) for s in sets]
    return {
        "actionLibraryGroupId": 1, "actionLibraryName": name, "actionRating": 0, "amplitudeStableScore": 0,
        "bilateralBalanceScore": 0, "categoryId": 1, "completionMethod": 0 if timed else 1,
        "completionScore": 0, "countType": 1, "finishedReps": rows, "forceControlScore": 0, "img": "",
        "isBarbell": 0, "isCustom": 0, "isFirst": 0, "isLeftRight": 1 if left_right else 0,
        "isOfflineEquipment": 0, "isShotEnd": 0, "maxWeight": max(s[1] for s in sets), "maxWeightCount": 0,
        "maxWeightPr": 0, "oneRepMax": 0, "oneRepMaxPr": 0, "plate": 0, "score": 0,
        "totalCapacity": float(sum(s[0] * s[1] for s in sets)), "totalCapacityPr": 0, "trainingPartId2": 11,
    }
