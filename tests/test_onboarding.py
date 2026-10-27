"""First-run setup and the sectioned Settings page: state rules, machine filtering, config compatibility, routes, escaping."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_round16  # noqa: F401  (sets the shared secret and imports the app the way the other page tests do)
from test_round16 import BASIC, client_with, gym_app

import onboarding as ob
from auth import LOCAL_ONLY_ENDPOINTS

FETCH = {**BASIC, "X-Requested-With": "fetch", "Accept": "application/json"}
VENDOR_TOKEN = "vendor-token"  # what client_with stores


def accessory(i, name="Rope", codes="monster3s,monster3", typ=0, **extra):
    return {"id": i, "name": name, "deviceCodes": codes, "type": typ, **extra}


def saved(client):
    with open(client.config_file) as fh:
        return json.load(fh)


def make(unit=0, confirmed=False, devices=(), **keys):
    """A signed-in client whose config file holds exactly these settings (and none of the first-run keys unless given)."""
    client = client_with(unit, confirmed)
    client.save_config("42", VENDOR_TOKEN, "Global", unit, "", 1, False, [], list(devices), unit_confirmed=confirmed, **keys)
    client.get_accessories = lambda: []
    return client


def real_login(client):
    """Run the real SpeedianceClient.login against a scripted vendor, so its save_config call is the one under test."""
    def request(method, url, **kw):
        if url.endswith("verifyIdentity"):
            return test_round16.Resp(200, {"data": {"isExist": True, "hasPwd": True}})
        return test_round16.Resp(200, {"data": {"token": "fresh-token", "appUserId": 42}})

    client._request = request
    return client.login("a@b.c", "pw")


def get(client, path="/settings"):
    with mock.patch.object(gym_app, "client", client):
        return gym_app.app.test_client().get(path, headers=BASIC).get_data(as_text=True)


def post(client, path, data=None, headers=FETCH, writes=False):
    env = {"GYM_ALLOW_WRITES": "1"} if writes else {}
    with mock.patch.object(gym_app, "client", client), mock.patch.dict(os.environ, env):
        return gym_app.app.test_client().post(path, headers=headers, data=data or {})


class MachineFilter(unittest.TestCase):
    def test_each_generation_matches_its_vendor_codes(self):
        codes = "monster2s,monster2,monster1"
        self.assertEqual(ob.accessory_machines(codes), ["monster12"])
        self.assertEqual(ob.accessory_machines("monster3s,monster3"), ["monster3"])
        self.assertEqual(ob.accessory_machines("monsterultra"), ["ultra"])
        self.assertEqual(ob.accessory_machines("monster3,monsterultra"), ["monster3", "ultra"])

    def test_codes_are_trimmed_and_case_insensitive(self):
        self.assertEqual(ob.accessory_machines(" MonsterUltra , x"), ["ultra"])

    def test_missing_or_odd_codes_mean_every_machine(self):
        everyone = [m["key"] for m in ob.MACHINES]
        for value in (None, "", "  ", " , ", [], ["monster1"], 7, {"a": 1}, True):
            self.assertEqual(ob.accessory_machines(value), everyone, repr(value))

    def test_codes_that_name_no_known_generation_fit_no_machine(self):
        self.assertEqual(ob.accessory_machines("gympal"), [])
        self.assertEqual(ob.accessory_machines("<script>alert(1)</script>"), [])

    def test_labels_keep_the_existing_wording_and_do_not_choke_on_odd_values(self):
        self.assertEqual(ob.generation_label("monster2s,monster2,monster1"), "Monster 1 · Monster 2 · Monster 2s")
        self.assertEqual(ob.generation_label("monsterultra"), "Ultra")
        for value in (None, [], 3, ""):
            self.assertEqual(ob.generation_label(value), "")

    def test_views_skip_entries_that_cannot_be_owned_and_coerce_text(self):
        views = ob.accessory_views([
            accessory(1), "junk", None, {"name": "no id"}, accessory(True), accessory(2.5), accessory("x9"),
            accessory("7", name=None, typ="1"), accessory(8, name=["a"]), accessory(9, img=12),
        ], [1, "7"])
        self.assertEqual([v["id"] for v in views], [1, 7, 8, 9])
        self.assertEqual([v["owned"] for v in views], [True, True, False, False])
        self.assertEqual([v["name"] for v in views], ["Rope", "Accessory 7", "Accessory 8", "Rope"])
        self.assertEqual(views[1]["type"], 1)
        self.assertEqual(views[3]["img"], "")

    def test_a_non_list_from_the_vendor_is_an_empty_list(self):
        for raw in (None, {"a": 1}, "x", 5):
            self.assertEqual(ob.accessory_views(raw, []), [])

    def test_groups_put_handles_before_benches_before_other_and_drop_empty_ones(self):
        views = ob.accessory_views([accessory(1, typ=1), accessory(2, typ=0), accessory(3, typ=9), accessory(4, typ=None)], [])
        groups = ob.group_accessories(views)
        self.assertEqual([g["title"] for g in groups], ["Handles and straps", "Benches", "Other"])
        self.assertEqual([[v["id"] for v in g["entries"]] for g in groups], [[2], [1], [3, 4]])
        self.assertEqual([g["title"] for g in ob.group_accessories(ob.accessory_views([accessory(1, typ=1)], []))], ["Benches"])

    def test_a_save_keeps_the_state_of_accessories_the_form_did_not_show(self):
        self.assertEqual(ob.merge_accessories([5, 6, 7], [6, 8], [5, 6, 8]), [7, 6, 8])  # 7 was hidden: kept; 5 unticked: gone
        self.assertEqual(ob.merge_accessories([5, 6], [], []), [5, 6])
        self.assertEqual(ob.merge_accessories([5, 6, 7], [6, 6], None), [6])  # the old form replaces everything


class MachineChoice(unittest.TestCase):
    def test_the_chosen_machine_sets_its_device_and_drops_the_one_it_replaces(self):
        gym_pal = ob.machine_by_key("gympal")
        ultra = ob.machine_by_key("ultra")
        self.assertEqual(ob.devices_after_machine([], None, ultra), [1])
        self.assertEqual(ob.devices_after_machine([1], "monster12", gym_pal), [2])
        self.assertEqual(ob.devices_after_machine([1, 3], "monster12", gym_pal), [2, 3])  # a second device is not touched
        self.assertEqual(ob.devices_after_machine([1], "monster12", ultra), [1])
        self.assertEqual(ob.devices_after_machine([1, 2], None, ultra), [1, 2])  # no known old machine: only add
        self.assertEqual(ob.devices_after_machine("junk", "nope", ultra), [1])

    def test_an_unknown_machine_key_is_no_machine(self):
        for key in (None, "", "monster1", ["ultra"], 3):
            self.assertIsNone(ob.machine_by_key(key))


class SetupState(unittest.TestCase):
    LEGACY = {"user_id": "42", "token": "t", "unit": 0, "unit_confirmed": True, "owned_devices": [1]}

    def test_a_config_from_before_setup_existed_is_not_sent_through_it(self):
        self.assertFalse(ob.needs_setup(self.LEGACY))

    def test_a_config_missing_a_login_a_unit_or_a_device_is_sent_through_it(self):
        for drop in ("token", "unit_confirmed", "owned_devices"):
            cfg = {k: v for k, v in self.LEGACY.items() if k != drop}
            self.assertTrue(ob.needs_setup(cfg), drop)
        self.assertTrue(ob.needs_setup({**self.LEGACY, "unit_confirmed": "true"}))  # hand-edited: not a human choice
        self.assertTrue(ob.needs_setup({}))

    def test_the_stored_flag_decides_when_it_is_a_real_boolean(self):
        self.assertFalse(ob.needs_setup({"onboarding_done": True}))
        self.assertTrue(ob.needs_setup({**self.LEGACY, "onboarding_done": False}))
        self.assertFalse(ob.needs_setup({**self.LEGACY, "onboarding_done": 0}))  # junk falls back to the legacy rule
        self.assertFalse(ob.needs_setup({**self.LEGACY, "onboarding_done": "yes"}))
        self.assertTrue(ob.needs_setup({"onboarding_done": "yes"}))

    def test_the_status_names_one_missing_thing_in_order(self):
        self.assertEqual(ob.setup_status({})["missing"], "login")
        self.assertEqual(ob.setup_status({"token": "t"})["missing"], "machine")
        self.assertEqual(ob.setup_status({"token": "t", "owned_devices": [1]})["missing"], "unit")
        done = ob.setup_status({"token": "t", "owned_devices": [2], "unit": 1, "unit_confirmed": True, "user_id": 5})
        self.assertEqual((done["complete"], done["missing"], done["unit"], done["machine"]), (True, None, "Imperial (lb)", "Gym Pal"))
        self.assertEqual(ob.setup_status({"token": "t", "owned_devices": [1], "unit": 1})["unit"], None)  # imperial but never chosen

    def test_the_status_never_carries_the_token(self):
        self.assertNotIn("SECRET-TOKEN", json.dumps(ob.setup_status({"token": "SECRET-TOKEN", "user_id": "1"})))

    def test_finishing_needs_a_login_a_generation_and_a_confirmed_unit(self):
        self.assertEqual(ob.finish_problems({}), ["login", "machine", "unit"])
        full = {"token": "t", "machine_generation": "ultra", "unit": 0, "unit_confirmed": True}
        self.assertEqual(ob.finish_problems(full), [])
        self.assertEqual(ob.finish_problems({**full, "machine_generation": "bogus"}), ["machine"])
        self.assertEqual(ob.finish_problems({**full, "unit_confirmed": False}), ["unit"])

    def test_the_flow_starts_at_the_first_unfinished_step_or_where_it_was_told(self):
        self.assertEqual(ob.start_step({}), 1)
        self.assertEqual(ob.start_step({"token": "t"}), 2)
        self.assertEqual(ob.start_step({"token": "t", "machine_generation": "ultra"}), 3)
        self.assertEqual(ob.start_step({"token": "t", "machine_generation": "ultra", "unit_confirmed": True}), 4)
        self.assertEqual(ob.start_step({"token": "t"}, rerun=True), 1)
        self.assertEqual(ob.start_step({}, "3"), 3)
        for bad in ("0", "6", "x", "-1", "2.5", None, ""):
            self.assertEqual(ob.start_step({"token": "t"}, bad), 2, repr(bad))


class ConfigCompatibility(unittest.TestCase):
    def test_an_old_file_without_the_new_keys_loads_and_stays_without_them_after_any_save(self):
        client = make(unit=1, confirmed=True, devices=[1])
        self.assertNotIn("onboarding_done", saved(client))
        self.assertNotIn("machine_generation", saved(client))
        post(client, "/settings/accessories", {"accessories": "5"})
        post(client, "/settings/owned_devices", {"owned_devices": "1"})
        client.update_unit(1)
        client.logout()
        self.assertTrue(real_login(client)[0])
        after = saved(client)
        self.assertNotIn("onboarding_done", after)
        self.assertNotIn("machine_generation", after)

    def test_an_old_file_is_not_sent_through_setup_and_gets_the_sections(self):
        html = get(make(unit=1, confirmed=True, devices=[1]))
        self.assertIn('id="status-card"', html)
        self.assertNotIn('id="wizard"', html)
        self.assertIn("All set.", html)

    def test_a_fresh_file_gets_the_first_run_flow(self):
        html = get(make())
        self.assertIn('id="wizard"', html)
        self.assertNotIn('id="status-card"', html)
        self.assertIn("Step 2 of 5", html)  # signed in already: starts at the machine

    def test_unrelated_saves_keep_the_setup_keys(self):
        client = make(unit=1, confirmed=True, devices=[1], onboarding_done=True, machine_generation="ultra")
        post(client, "/settings/accessories", {"accessories": "5"})
        post(client, "/settings/owned_devices", {"owned_devices": "1"})
        post(client, "/settings/unit", {"unit": "0"}, writes=True)
        client.logout()
        self.assertTrue(real_login(client)[0])
        post(client, "/settings", {"user_id": "42", "token": "", "region": "Global", "device_type": "1"})
        after = saved(client)
        self.assertEqual((after["onboarding_done"], after["machine_generation"]), (True, "ultra"))
        with mock.patch.object(gym_app, "client", client):
            gym_app.app.test_client().post("/settings/custom_instruction", headers=BASIC, json={"instruction": "x"})
        self.assertEqual(saved(client)["onboarding_done"], True)

    def test_another_account_is_set_up_again_and_its_unit_is_unknown(self):
        client = make(unit=1, confirmed=True, devices=[1], onboarding_done=True, machine_generation="ultra")
        client.save_config("999", "tok", "Global", 1, "", 1, False, [], [1])
        after = saved(client)
        self.assertEqual((after["onboarding_done"], after["unit_confirmed"], after["unit"]), (False, False, 0))
        self.assertEqual(after["machine_generation"], "ultra")  # kept: it prefills the flow

    def test_the_same_account_logging_in_again_keeps_everything(self):
        client = make(unit=1, confirmed=True, devices=[1], onboarding_done=True, machine_generation="ultra")
        client.save_config("42", "tok2", "Global", 1, "", 1, False, [], [1])
        after = saved(client)
        self.assertEqual((after["onboarding_done"], after["unit_confirmed"]), (True, True))

    def test_a_hand_edited_junk_generation_is_ignored_by_the_page(self):
        client = make(unit=1, confirmed=True, devices=[1], machine_generation="bogus", onboarding_done=True)
        client.get_accessories = lambda: [accessory(1)]
        html = get(client)
        self.assertNotIn('data-label="bogus"', html)
        self.assertIn("Choose yours to filter accessories", html)

    def test_a_logged_out_finished_config_shows_the_login_form_not_the_flow(self):
        client = make(unit=1, confirmed=True, devices=[1], onboarding_done=True, machine_generation="ultra")
        client.logout()
        html = get(client)
        self.assertIn('id="status-card"', html)
        self.assertIn("Not logged in", html)
        self.assertIn('name="email"', html)
        self.assertIn('href="#account"', html)


class Routes(unittest.TestCase):
    def test_new_local_routes_are_open_without_writes_but_the_unit_route_is_not(self):
        self.assertIn("update_machine", LOCAL_ONLY_ENDPOINTS)
        self.assertIn("finish_onboarding", LOCAL_ONLY_ENDPOINTS)
        self.assertNotIn("update_unit", LOCAL_ONLY_ENDPOINTS)
        self.assertNotIn("logout", LOCAL_ONLY_ENDPOINTS)
        client = make()
        self.assertEqual(post(client, "/settings/machine", {"machine": "ultra"}).status_code, 200)  # no GYM_ALLOW_WRITES
        self.assertEqual(post(client, "/settings/unit", {"unit": "1"}).status_code, 403)
        self.assertEqual(post(client, "/settings/unit", {"unit": "1"}).get_json()["error"], "writes_disabled")

    def test_a_cross_site_post_to_the_new_routes_is_refused(self):
        client = make()
        evil = {**FETCH, "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
        for path, data in (("/settings/machine", {"machine": "ultra"}), ("/settings/onboarding/finish", {})):
            self.assertEqual(post(client, path, data, headers=evil).status_code, 403, path)
        self.assertNotIn("machine_generation", saved(client))

    def test_choosing_a_machine_saves_it_and_its_device_and_answers_with_status(self):
        client = make()
        resp = post(client, "/settings/machine", {"machine": "monster3"})
        body = resp.get_json()
        self.assertEqual((resp.status_code, body["ok"], body["machine"]), (200, True, "Gym Monster 3 / 3S"))
        self.assertEqual(body["status"]["machine"], "Gym Monster 3 / 3S")
        self.assertEqual((saved(client)["machine_generation"], saved(client)["owned_devices"]), ("monster3", [1]))
        post(client, "/settings/machine", {"machine": "gympal"})
        self.assertEqual((saved(client)["machine_generation"], saved(client)["owned_devices"]), ("gympal", [2]))
        self.assertEqual(saved(client)["device_type"], 1)  # the library device is a separate setting, left alone

    def test_an_unknown_machine_is_refused_and_changes_nothing(self):
        client = make(machine_generation="ultra")
        for value in (None, "", "monster1", "../x", "ultra,gympal"):
            resp = post(client, "/settings/machine", {} if value is None else {"machine": value})
            self.assertEqual(resp.status_code, 400, repr(value))
            self.assertFalse(resp.get_json()["ok"])
        self.assertEqual(saved(client)["machine_generation"], "ultra")

    def test_a_plain_form_post_still_flashes_and_redirects(self):
        client = make()
        resp = post(client, "/settings/machine", {"machine": "ultra"}, headers=BASIC)
        self.assertEqual((resp.status_code, resp.headers["Location"]), (302, "/settings"))
        self.assertEqual(post(client, "/settings/machine", {"machine": "x"}, headers=BASIC).status_code, 302)

    def test_finishing_is_refused_until_login_machine_and_unit_are_set(self):
        client = make()
        resp = post(client, "/settings/onboarding/finish")
        self.assertEqual((resp.status_code, resp.get_json()["missing"]), (400, ["machine", "unit"]))
        self.assertNotIn("onboarding_done", saved(client))
        post(client, "/settings/machine", {"machine": "ultra"})
        self.assertEqual(post(client, "/settings/onboarding/finish").get_json()["missing"], ["unit"])
        post(client, "/settings/unit", {"unit": "0"}, writes=True)
        resp = post(client, "/settings/onboarding/finish")
        self.assertEqual((resp.status_code, resp.get_json()["ok"]), (200, True))
        self.assertIs(saved(client)["onboarding_done"], True)
        self.assertIn('id="status-card"', get(client))

    def test_a_logged_out_client_cannot_finish(self):
        client = make(unit=1, confirmed=True, devices=[1], machine_generation="ultra")
        client.logout()
        self.assertEqual(post(client, "/settings/onboarding/finish").get_json()["missing"], ["login"])

    def test_saving_the_unit_in_setup_confirms_it_and_sends_it_to_the_account(self):
        calls = []
        client = make()
        original = client._request

        def request(method, url, **kw):
            calls.append((method, url, kw.get("json")))
            return original(method, url, **kw)

        client._request = request
        self.assertFalse(saved(client)["unit_confirmed"])
        resp = post(client, "/settings/unit", {"unit": "1"}, writes=True)
        self.assertEqual((resp.status_code, resp.get_json()["unit"], resp.get_json()["status"]["unit"]), (200, 1, "Imperial (lb)"))
        self.assertEqual((saved(client)["unit"], saved(client)["unit_confirmed"]), (1, True))
        self.assertEqual(calls[-1][0:3:2], ("PUT", {"unit": 1}))

    def test_a_refused_unit_choice_saves_nothing_and_says_why(self):
        client = make()
        for data in ({}, {"unit": "2"}, {"unit": ""}):
            resp = post(client, "/settings/unit", data, writes=True)
            self.assertEqual((resp.status_code, resp.get_json()["ok"]), (400, False))
        client.update_unit = lambda unit: (False, "vendor says no " + "x" * 500)
        resp = post(client, "/settings/unit", {"unit": "1"}, writes=True)
        self.assertEqual(resp.status_code, 502)
        self.assertLessEqual(len(resp.get_json()["message"]), 240)
        self.assertFalse(saved(client)["unit_confirmed"])

    def test_accessories_save_merges_with_the_ones_the_form_did_not_show(self):
        client = make(devices=[1])
        post(client, "/settings/accessories", {"accessories": ["1", "2", "3"]})
        resp = post(client, "/settings/accessories", {"accessories": ["2", "9"], "visible": "1,2,9"})
        self.assertEqual(saved(client)["owned_accessories"], [3, 2, 9])
        self.assertEqual(resp.get_json()["count"], 3)
        post(client, "/settings/accessories", {"accessories": [], "visible": ""})  # nothing was on screen
        self.assertEqual(saved(client)["owned_accessories"], [3, 2, 9])

    def test_the_old_accessories_form_still_replaces_the_set_and_bad_values_are_refused(self):
        client = make()
        post(client, "/settings/accessories", {"accessories": ["1", "2"]}, headers=BASIC)
        post(client, "/settings/accessories", {"accessories": ["4"]}, headers=BASIC)
        self.assertEqual(saved(client)["owned_accessories"], [4])
        for data in ({"accessories": "abc"}, {"accessories": "1", "visible": "1,zz"}):
            self.assertEqual(post(client, "/settings/accessories", data).status_code, 400)
        self.assertEqual(saved(client)["owned_accessories"], [4])

    def test_login_answers_json_without_the_token_or_the_debug_blob(self):
        client = make()
        client.login = lambda e, p: (False, "Login failed", "REQ password=hunter2 token=zzz")
        resp = post(client, "/login", {"email": "a@b.c", "password": "hunter2"})
        text = resp.get_data(as_text=True)
        self.assertEqual((resp.status_code, resp.get_json()["message"]), (400, "Login failed"))
        for secret in ("hunter2", "token=zzz", VENDOR_TOKEN):
            self.assertNotIn(secret, text)
        client.login = lambda e, p: (True, "Login successful", None)
        ok = post(client, "/login", {"email": "a@b.c", "password": "p"})
        self.assertEqual((ok.status_code, ok.get_json()["ok"]), (200, True))
        self.assertNotIn(VENDOR_TOKEN, ok.get_data(as_text=True))
        self.assertEqual(post(client, "/login", {"email": "a@b.c"}).status_code, 400)

    def test_a_plain_login_still_goes_home_and_only_the_fixed_next_goes_to_settings(self):
        client = make()
        client.login = lambda e, p: (True, "Login successful", None)
        for extra, where in (({}, "/"), ({"next": "settings"}, "/settings"), ({"next": "https://evil.example"}, "/"),
                             ({"next": "//evil.example"}, "/"), ({"next": "/settings"}, "/")):
            resp = post(client, "/login", {"email": "a@b.c", "password": "p", **extra}, headers=BASIC)
            self.assertEqual((resp.status_code, resp.headers["Location"]), (302, where), extra)

    def test_a_nano_logout_from_the_page_never_calls_the_vendor(self):
        client = make()
        seen = []
        client._request = lambda *a, **k: seen.append(a)
        with mock.patch.dict(os.environ, {"GYM_APP_TYPE": "NANO"}):
            resp = post(client, "/logout", writes=True)
        self.assertEqual((resp.status_code, resp.get_json()["ok"]), (200, True))
        self.assertEqual(seen, [])
        self.assertEqual(saved(client)["token"], "")

    def test_manual_config_keeps_the_custom_instruction_when_the_form_does_not_send_it(self):
        client = make()
        client.save_config("42", VENDOR_TOKEN, "Global", 0, "keep me", 1, False, [], [1])
        post(client, "/settings", {"user_id": "42", "token": "", "region": "EU", "device_type": "1"})
        self.assertEqual(saved(client)["custom_instruction"], "keep me")
        post(client, "/settings", {"user_id": "42", "token": "", "region": "EU", "device_type": "1", "custom_instruction": "new"})
        self.assertEqual(saved(client)["custom_instruction"], "new")


class Page(unittest.TestCase):
    def test_the_vendor_token_is_in_no_page_and_no_json_answer(self):
        client = make(unit=1, confirmed=True, devices=[1], onboarding_done=True, machine_generation="ultra")
        client.get_accessories = lambda: [accessory(1, "Rope", "monsterultra")]
        for path in ("/settings", "/settings?setup=1", "/settings?setup=1&step=4"):
            self.assertNotIn(VENDOR_TOKEN, get(client, path), path)
        for path, data in (("/settings/machine", {"machine": "ultra"}), ("/settings/accessories", {"accessories": "1"}),
                           ("/settings/owned_devices", {"owned_devices": "1"}), ("/settings/onboarding/finish", {}),
                           ("/settings", {"region": "Global", "token": ""}), ("/logout", {})):
            self.assertNotIn(VENDOR_TOKEN, post(client, path, data, writes=True).get_data(as_text=True), path)

    def test_edit_setup_reruns_the_flow_with_the_current_values_filled_in(self):
        client = make(unit=1, confirmed=True, devices=[1], onboarding_done=True, machine_generation="ultra")
        client.get_accessories = lambda: [accessory(1, "Rope", "monsterultra"), accessory(2, "Bar", "monsterultra")]
        client.save_config("42", VENDOR_TOKEN, "Global", 1, "", 1, False, [2], [1], unit_confirmed=True)
        html = get(client, "/settings?setup=1")
        self.assertIn('id="wizard"', html)
        self.assertIn("Step 1 of 5", html)
        self.assertIn("Logged in as User ID: 42", html)
        self.assertRegex(html, r'value="ultra"[^>]*checked')
        self.assertRegex(html, r'name="unit" value="1"[^>]*checked')
        self.assertRegex(html, r'name="accessories" value="2"[^>]*checked')
        self.assertNotRegex(html, r'name="accessories" value="1"[^>]*checked')
        self.assertIn("Leave setup without changes", html)
        self.assertIn("Step 3 of 5", get(client, "/settings?setup=1&step=3"))
        self.assertIn("Step 1 of 5", get(client, "/settings?setup=1&step=99"))

    def test_the_unit_is_never_preselected_until_a_person_chose_it(self):
        html = get(make(unit=0, confirmed=False, devices=[1]))
        self.assertNotRegex(html, r'name="unit" value="[01]"[^>]*checked')
        self.assertIn("Not set: choose yours", html)

    def test_each_accessory_says_which_machines_it_fits_so_the_page_can_filter_without_a_reload(self):
        client = make()
        client.get_accessories = lambda: [accessory(1, "A", "monster2s,monster2,monster1"), accessory(2, "B", "monsterultra"),
                                          accessory(3, "C", None), accessory(4, "D", "gympal")]
        html = get(client)
        self.assertIn('data-machines="monster12"', html)
        self.assertIn('data-machines="ultra"', html)
        self.assertIn('data-machines="monster12 monster3 ultra gympal velonix"', html)  # no codes: every machine
        self.assertIn('data-machines=""', html)  # codes that name nothing known: only under "all generations"

    def test_accessories_are_grouped_by_type(self):
        client = make()
        client.get_accessories = lambda: [accessory(1, "Flat Bench", typ=1), accessory(2, "Strap", typ=0)]
        html = get(client)
        self.assertLess(html.index("Handles and straps"), html.index("Strap"))
        self.assertLess(html.index("Strap"), html.index("Benches"))
        self.assertLess(html.index("Benches"), html.index("Flat Bench"))

    def test_what_the_vendor_sends_is_escaped_in_every_new_field(self):
        evil = '"><img src=x onerror=alert(1)>'
        client = make(unit=1, confirmed=True, devices=[1], onboarding_done=True, machine_generation="ultra")
        client.get_accessories = lambda: [
            {"id": 1, "name": evil, "deviceCodes": evil, "type": 0, "img": evil},
            {"id": 2, "name": "<script>alert(2)</script>", "deviceCodes": "monsterultra", "type": 1},
            {"id": 3, "name": "ok", "deviceCodes": "monsterultra", "type": evil},
        ]
        for path in ("/settings", "/settings?setup=1&step=4"):
            html = get(client, path)
            self.assertNotIn("<img src=x", html, path)
            self.assertNotIn("<script>alert(2)", html, path)
            self.assertIn("&lt;script&gt;alert(2)&lt;/script&gt;", html, path)
            self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html, path)
        self.assertEqual(get(client).count('src="&#34;&gt;&lt;img'), 1)  # the image URL is an escaped attribute value

    def test_a_vendor_failure_shows_a_message_and_never_the_exception_text(self):
        client = make(unit=1, confirmed=True, devices=[1], onboarding_done=True, machine_generation="ultra")

        def down():
            raise RuntimeError("secret internal detail 10.0.0.7")

        client.get_accessories = down
        html = get(client)
        self.assertIn("Could not load your accessories.", html)
        self.assertIn("Speediance could not be reached.", html)
        self.assertNotIn("secret internal detail", html)
        self.assertNotIn('id="accessory-list"', html)

        def expired():
            raise Exception("Unauthorized")

        client.get_accessories = expired
        self.assertIn("session has expired", get(client))

    def test_no_accessories_returned_is_its_own_state_with_a_way_on(self):
        client = make()
        client.get_accessories = lambda: []
        html = get(client)
        self.assertIn("Speediance returned no accessories.", html)
        self.assertIn("Continue without accessories", html)  # step 4 of the flow
        client.get_accessories = lambda: [{"name": "no id"}, "junk"]
        self.assertIn("Speediance returned no accessories.", get(client))

    def test_logged_out_the_accessory_section_says_to_log_in(self):
        client = make(unit=1, confirmed=True, devices=[1], onboarding_done=True, machine_generation="ultra")
        client.logout()
        self.assertIn("Log in first.", get(client))

    def test_the_status_card_offers_one_button_for_the_one_missing_thing(self):
        client = make(unit=0, confirmed=False, devices=[1], onboarding_done=True, machine_generation="ultra")
        html = get(client)
        self.assertIn("One thing left.", html)
        self.assertIn('href="#unit"', html)
        self.assertIn("Choose your unit", html)
        self.assertIn('href="/settings?setup=1"', html)  # Edit setup

    def test_advanced_holds_manual_config_instructions_and_offline_content_collapsed(self):
        html = get(make(unit=1, confirmed=True, devices=[1], onboarding_done=True, machine_generation="ultra"))
        adv = html[html.index('<details id="advanced"'):]
        self.assertNotIn("open", adv[:adv.index(">")])
        for text in ("Manual Config", "Custom instructions", "Offline Content", 'action="/settings/preload"'):
            self.assertIn(text, adv)
        for text in ("Manual Config", "Offline Content"):
            self.assertLess(html.index('id="accessories"'), html.index(text))

    def test_the_three_main_sections_start_open(self):
        html = get(make(unit=1, confirmed=True, devices=[1], onboarding_done=True, machine_generation="ultra"))
        for section in ("account", "machine", "accessories"):
            self.assertIn(f'<details id="{section}" open', html)


if __name__ == "__main__":
    unittest.main()
