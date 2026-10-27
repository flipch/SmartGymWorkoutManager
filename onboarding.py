"""First-run setup and the Settings page: which machine, which accessories, what is still missing.

Pure functions over the config dict and the vendor's accessory list, so the rules can be tested without Flask.
Nothing here ever sees the vendor token.
"""
from api_client import unit_is_confirmed

STEPS = 5  # account, machine, unit, accessories, done

# One entry per choice in the first-run flow. `devices` is the owned-device id the Browse page filters on
# (1 Gym Monster, 2 Gym Pal, 3 Velonix); `codes` are the vendor's `deviceCodes` tokens that name that generation.
# Gym Pal and Velonix have no known codes: the vendor lists accessories per Gym Monster generation only (not verified
# for the other two), so for them every accessory is offered.
MACHINES = (
    {"key": "monster12", "label": "Gym Monster 1 / 2 / 2S", "devices": (1,), "codes": ("monster1", "monster2", "monster2s")},
    {"key": "monster3", "label": "Gym Monster 3 / 3S", "devices": (1,), "codes": ("monster3", "monster3s")},
    {"key": "ultra", "label": "Gym Monster Ultra", "devices": (1,), "codes": ("monsterultra",)},
    {"key": "gympal", "label": "Gym Pal", "devices": (2,), "codes": ()},
    {"key": "velonix", "label": "Velonix", "devices": (3,), "codes": ()},
)
DEVICE_LABELS = {1: "Gym Monster", 2: "Gym Pal", 3: "Velonix"}
UNIT_LABELS = {0: "Metric (kg)", 1: "Imperial (lb)"}
ACCESSORY_TYPES = ((0, "Handles and straps"), (1, "Benches"))  # the vendor's `type`; anything else is listed last as "Other"


def machine_by_key(key):
    if not isinstance(key, str):
        return None
    return next((m for m in MACHINES if m["key"] == key), None)


def as_int(value):
    """An int from an int or a string of digits; None for anything else (bool, float, text)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def int_list(values):
    return [n for n in (as_int(v) for v in values) if n is not None] if isinstance(values, (list, tuple)) else []


def _tokens(value):
    """The comma separated `deviceCodes` of one accessory, lower case. Anything but a string is no codes at all."""
    if not isinstance(value, str):
        return []
    return [t for t in (part.strip().lower() for part in value.split(",")) if t]


def accessory_machines(device_codes):
    """The machine keys one accessory is for. No codes (missing, null, a list, blank) means the vendor did not say, so it
    is offered for every machine. Codes that name no known generation fit none: they show only under "all generations"."""
    tokens = set(_tokens(device_codes))
    if not tokens:
        return [m["key"] for m in MACHINES]
    return [m["key"] for m in MACHINES if tokens & set(m["codes"])]


def generation_label(device_codes):
    """'Monster 1 · Monster 2 · Monster 2s' for the vendor's 'monster2s,monster2,monster1'. Unknown tokens are kept as text."""
    tokens = _tokens(device_codes)[:8]
    return " · ".join(t[:40] for t in reversed(tokens)).replace("monsterultra", "Ultra").replace("monster", "Monster ")


def accessory_views(raw, owned, limit=400):
    """What the template needs from the vendor's list: only entries with a usable integer id, text coerced to text."""
    owned = set(int_list(owned))
    out = []
    for acc in raw if isinstance(raw, list) else []:
        if not isinstance(acc, dict):
            continue
        acc_id = as_int(acc.get("id"))
        if acc_id is None:
            continue
        name = acc.get("name")
        img = acc.get("img")
        out.append({
            "id": acc_id,
            "name": name.strip() if isinstance(name, str) and name.strip() else f"Accessory {acc_id}",
            "img": img if isinstance(img, str) and img else "",
            "type": as_int(acc.get("type")),
            "label": generation_label(acc.get("deviceCodes")),
            "machines": " ".join(accessory_machines(acc.get("deviceCodes"))),
            "owned": acc_id in owned,
        })
        if len(out) >= limit:
            break
    return out


def group_accessories(views):
    """Handles and straps (type 0), benches (type 1), then anything else; empty groups are left out."""
    known = {t for t, _ in ACCESSORY_TYPES}
    groups = [{"key": f"type{t}", "title": title, "entries": [v for v in views if v["type"] == t]} for t, title in ACCESSORY_TYPES]
    groups.append({"key": "other", "title": "Other", "entries": [v for v in views if v["type"] not in known]})
    return [g for g in groups if g["entries"]]


def merge_accessories(previous, ticked, visible):
    """The owned set after a save. The form only lists the accessories for the chosen machine, so one it did not show keeps
    its old state; without a `visible` list (the old form) the ticked set replaces everything."""
    ticked = list(dict.fromkeys(ticked))
    if visible is None:
        return ticked
    shown = set(visible)
    return list(dict.fromkeys([n for n in previous if n not in shown] + ticked))


def devices_after_machine(previous_devices, previous_machine_key, machine):
    """Owned devices after choosing a machine: its device is added, and the device of the machine it replaces is dropped."""
    old = machine_by_key(previous_machine_key)
    keep = [d for d in int_list(previous_devices) if not (old and d in old["devices"] and d not in machine["devices"])]
    return sorted(set(keep) | set(machine["devices"]))


def logged_in(creds):
    return bool(creds.get("token"))


def has_machine(creds):
    return machine_by_key(creds.get("machine_generation")) is not None or bool(int_list(creds.get("owned_devices")))


def needs_setup(creds):
    """Show the first-run flow? Done once finished. A config from before it existed (no onboarding_done key) is not sent
    through it if it already has a login, a confirmed unit and an owned device; an explicit False always is."""
    done = creds.get("onboarding_done")
    if isinstance(done, bool):
        return not done
    return not (logged_in(creds) and unit_is_confirmed(creds) and int_list(creds.get("owned_devices")))


def machine_label(creds):
    machine = machine_by_key(creds.get("machine_generation"))
    if machine:
        return machine["label"]
    devices = [DEVICE_LABELS[d] for d in int_list(creds.get("owned_devices")) if d in DEVICE_LABELS]
    return " + ".join(devices) if devices else None


def unit_label(creds):
    return UNIT_LABELS.get(creds.get("unit")) if unit_is_confirmed(creds) else None


def setup_status(creds):
    """The one thing still missing, as text plus where to fix it. Order: log in, machine, unit."""
    base = {"machine": machine_label(creds), "unit": unit_label(creds), "logged_in": logged_in(creds),
            "user_id": str(creds.get("user_id") or "")}
    if not logged_in(creds):
        return {**base, "complete": False, "missing": "login", "message": "Not logged in to Speediance.",
                "action": "Log in", "anchor": "account"}
    if not has_machine(creds):
        return {**base, "complete": False, "missing": "machine", "message": "Your machine is not chosen.",
                "action": "Choose your machine", "anchor": "machine"}
    if not unit_is_confirmed(creds):
        return {**base, "complete": False, "missing": "unit",
                "message": "Your weight unit is not set, so saved weights could be mislabelled.",
                "action": "Choose your unit", "anchor": "unit"}
    return {**base, "complete": True, "missing": None, "message": "All set.", "action": "", "anchor": ""}


def finish_problems(creds):
    """What stops the first-run flow from being finished (an empty list means it can be)."""
    problems = []
    if not logged_in(creds):
        problems.append("login")
    if machine_by_key(creds.get("machine_generation")) is None:
        problems.append("machine")
    if not unit_is_confirmed(creds):
        problems.append("unit")
    return problems


def first_incomplete_step(creds):
    if not logged_in(creds):
        return 1
    if machine_by_key(creds.get("machine_generation")) is None:
        return 2
    if not unit_is_confirmed(creds):
        return 3
    return 4


def start_step(creds, requested=None, rerun=False):
    n = as_int(requested)
    if n is not None and 1 <= n <= STEPS:
        return n
    return 1 if rerun else first_incomplete_step(creds)


def build_view(creds, raw_accessories, state, error="", rerun=False, requested_step=None):
    """Everything settings.html shows beyond `creds`. `state` is how the accessory list came back: signed_out, ok, none or error."""
    needs = needs_setup(creds)
    machine = machine_by_key(creds.get("machine_generation"))
    views = accessory_views(raw_accessories, creds.get("owned_accessories")) if state == "ok" else []
    if state == "ok" and not views:
        state = "none"  # a list with nothing usable in it is no list
    owned_devices = int_list(creds.get("owned_devices"))
    return {
        "wizard": needs or rerun,
        "first_run": needs,
        "step": start_step(creds, requested_step, rerun),
        "steps": STEPS,
        "machines": [{**m, "selected": bool(machine) and m["key"] == machine["key"]} for m in MACHINES],
        "machine_key": machine["key"] if machine else "",
        "machine_label": machine_label(creds),
        "unit_known": unit_is_confirmed(creds),
        "unit_label": unit_label(creds),
        "devices": [{"id": d, "label": label, "owned": d in owned_devices} for d, label in DEVICE_LABELS.items()],
        "status": setup_status(creds),
        "accessories": {"state": state, "error": error, "groups": group_accessories(views), "count": len(views),
                        "owned_count": len(int_list(creds.get("owned_accessories")))},
    }
