import os, sys
from types import SimpleNamespace
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.overrides.registry import HOOKS


def _fake_ctx(id_map, owners_result=([1], [2]), current_user_id=7):
    return SimpleNamespace(
        id_map=id_map,
        current_user_id=current_user_id,
        resolve_owners=lambda rec, label: owners_result,
    )


# -- ami ---------------------------------------------------------------

def test_ami_hook_builds_payload():
    ctx = _fake_ctx(id_map={"account": {"5": 500}})
    rec = {
        "name": "golden-image", "aws_ami_id": "ami-123", "region": "us-east-1",
        "__srcid__account_id": 5, "description": "desc",
    }
    paths, payload = HOOKS["ami"].build_create_payload(rec, ctx)
    assert paths == ["/v3/ami"]
    assert payload["account_id"] == 500
    assert payload["owner_user_ids"] == [1]
    assert payload["owner_user_group_ids"] == [2]
    assert payload["description"] == "desc"


def test_ami_hook_skips_on_unresolved_account():
    ctx = _fake_ctx(id_map={"account": {}})
    rec = {"name": "golden-image", "__srcid__account_id": 5}
    assert HOOKS["ami"].build_create_payload(rec, ctx) is None


# -- azure_role ----------------------------------------------------------

def test_azure_role_hook_builds_payload_and_omits_car_restricted_users():
    ctx = _fake_ctx(id_map={})
    rec = {
        "name": "AzRole", "role_permissions": ["read"], "description": "d",
        "car_restricted_user_ids": [1, 2], "car_restricted_user_group_ids": [3],
    }
    paths, payload = HOOKS["azure_role"].build_create_payload(rec, ctx)
    assert paths == ["/v3/azure-role"]
    assert payload["owner_user_ids"] == [1]
    assert "car_restricted_user_ids" not in payload
    assert "car_restricted_user_group_ids" not in payload


# -- cft -------------------------------------------------------------------

def test_cft_hook_builds_payload():
    ctx = _fake_ctx(id_map={})
    rec = {"name": "CFT1", "policy": "{}", "regions": ["us-east-1"], "sns_arns": ["arn"]}
    paths, payload = HOOKS["cft"].build_create_payload(rec, ctx)
    assert paths == ["/v3/cft"]
    assert payload["policy"] == "{}"
    assert payload["regions"] == ["us-east-1"]
    assert payload["sns_arns"] == ["arn"]
    assert payload["owner_user_ids"] == [1]


# -- compliance_standard ----------------------------------------------------

def test_compliance_standard_hook_uses_running_user():
    ctx = _fake_ctx(id_map={}, current_user_id=42)
    rec = {"name": "Std1", "description": "d"}
    paths, payload = HOOKS["compliance_standard"].build_create_payload(rec, ctx)
    assert paths == ["/v3/compliance/standard"]
    assert payload["created_by_user_id"] == 42


def test_compliance_standard_hook_skips_without_running_user():
    ctx = _fake_ctx(id_map={}, current_user_id=None)
    rec = {"name": "Std1"}
    assert HOOKS["compliance_standard"].build_create_payload(rec, ctx) is None


# -- iam_policy --------------------------------------------------------------

def test_iam_policy_hook_builds_payload_and_omits_car_restricted_users():
    ctx = _fake_ctx(id_map={})
    rec = {
        "name": "Policy1", "policy": "{}", "car_restricted": True,
        "car_restricted_user_ids": [9], "car_restricted_user_group_ids": [10],
        "aws_iam_path": "/custom/",
    }
    paths, payload = HOOKS["iam_policy"].build_create_payload(rec, ctx)
    assert paths == ["/v3/iam-policy"]
    assert payload["car_restricted"] is True
    assert payload["aws_iam_path"] == "/custom/"
    assert "car_restricted_user_ids" not in payload
    assert "car_restricted_user_group_ids" not in payload


# -- ou_cloud_access_role -----------------------------------------------------

def test_ou_cloud_access_role_hook_builds_payload_and_omits_user_ids():
    ctx = _fake_ctx(id_map={"ou": {"11": 110}})
    rec = {
        "name": "CAR1", "aws_iam_role_name": "role", "__srcid__ou_id": 11,
        "user_ids": [1, 2], "user_group_ids": [3],
        "long_term_access_keys": True,
    }
    paths, payload = HOOKS["ou_cloud_access_role"].build_create_payload(rec, ctx)
    assert paths == ["/v3/ou-cloud-access-role"]
    assert payload["ou_id"] == 110
    assert payload["long_term_access_keys"] is True
    assert "user_ids" not in payload
    assert "user_group_ids" not in payload


def test_ou_cloud_access_role_hook_skips_on_unresolved_ou():
    ctx = _fake_ctx(id_map={"ou": {}})
    rec = {"name": "CAR1", "__srcid__ou_id": 11}
    assert HOOKS["ou_cloud_access_role"].build_create_payload(rec, ctx) is None


# -- ou_note -----------------------------------------------------------------

def test_ou_note_hook_builds_payload():
    ctx = _fake_ctx(id_map={"ou": {"11": 110}}, current_user_id=7)
    rec = {"name": "Note1", "text": "hello", "__srcid__ou_id": 11}
    paths, payload = HOOKS["ou_note"].build_create_payload(rec, ctx)
    assert paths == ["/v3/ou-note"]
    assert payload["ou_id"] == 110
    assert payload["create_user_id"] == 7


def test_ou_note_hook_skips_on_unresolved_ou():
    ctx = _fake_ctx(id_map={"ou": {}}, current_user_id=7)
    rec = {"name": "Note1", "__srcid__ou_id": 11}
    assert HOOKS["ou_note"].build_create_payload(rec, ctx) is None


def test_ou_note_hook_skips_without_running_user():
    ctx = _fake_ctx(id_map={"ou": {"11": 110}}, current_user_id=None)
    rec = {"name": "Note1", "__srcid__ou_id": 11}
    assert HOOKS["ou_note"].build_create_payload(rec, ctx) is None


# -- project_cloud_access_role -------------------------------------------------

def test_project_cloud_access_role_hook_resolves_accounts_and_drops_missing():
    ctx = _fake_ctx(id_map={"project": {"20": 200}, "account": {"1": 100}})
    rec = {
        "name": "PCAR1", "aws_iam_role_name": "role",
        "__srcid__project_id": 20, "__srcid__account_ids": [1, 999],
        "apply_to_all_accounts": False, "user_ids": [1],
    }
    paths, payload = HOOKS["project_cloud_access_role"].build_create_payload(rec, ctx)
    assert paths == ["/v3/project-cloud-access-role"]
    assert payload["project_id"] == 200
    assert payload["account_ids"] == [100]
    assert "user_ids" not in payload


def test_project_cloud_access_role_hook_skips_on_unresolved_project():
    ctx = _fake_ctx(id_map={"project": {}, "account": {}})
    rec = {"name": "PCAR1", "__srcid__project_id": 20, "__srcid__account_ids": []}
    assert HOOKS["project_cloud_access_role"].build_create_payload(rec, ctx) is None


# -- project_note --------------------------------------------------------------

def test_project_note_hook_builds_payload_with_project():
    ctx = _fake_ctx(id_map={"project": {"20": 200}}, current_user_id=7)
    rec = {"name": "PNote1", "text": "hi", "__srcid__project_id": 20}
    paths, payload = HOOKS["project_note"].build_create_payload(rec, ctx)
    assert paths == ["/v3/project-note"]
    assert payload["project_id"] == 200
    assert payload["create_user_id"] == 7


def test_project_note_hook_omits_project_when_unresolved_and_never_skips():
    ctx = _fake_ctx(id_map={"project": {}}, current_user_id=None)
    rec = {"name": "PNote1", "text": "hi", "__srcid__project_id": 20}
    paths, payload = HOOKS["project_note"].build_create_payload(rec, ctx)
    assert paths == ["/v3/project-note"]
    assert "project_id" not in payload
    assert "create_user_id" not in payload


# -- service_catalog -------------------------------------------------------

def test_service_catalog_hook_builds_payload():
    ctx = _fake_ctx(id_map={"account": {"5": 500}})
    rec = {
        "name": "Catalog1", "portfolio_id": "port-1", "region": "us-east-1",
        "__srcid__account_id": 5,
    }
    paths, payload = HOOKS["service_catalog"].build_create_payload(rec, ctx)
    assert paths == ["/v3/service-catalog"]
    assert payload["account_id"] == 500
    assert payload["portfolio_id"] == "port-1"


def test_service_catalog_hook_skips_on_unresolved_account():
    ctx = _fake_ctx(id_map={"account": {}})
    rec = {"name": "Catalog1", "__srcid__account_id": 5}
    assert HOOKS["service_catalog"].build_create_payload(rec, ctx) is None


# -- service_control_policy --------------------------------------------------

def test_service_control_policy_hook_builds_payload():
    ctx = _fake_ctx(id_map={})
    rec = {"name": "SCP1", "policy": "{}", "description": "d"}
    paths, payload = HOOKS["service_control_policy"].build_create_payload(rec, ctx)
    assert paths == ["/v3/service-control-policy"]
    assert payload["policy"] == "{}"
    assert payload["owner_user_ids"] == [1]


# -- user_group ---------------------------------------------------------------

def test_user_group_hook_builds_payload_and_omits_viewer_and_member_ids():
    ctx = _fake_ctx(id_map={"idms": {"3": 300}})
    rec = {
        "name": "Group1", "description": "d", "__srcid__idms_id": 3,
        "viewer_user_ids": [1], "viewer_user_group_ids": [2], "user_ids": [4, 5],
    }
    paths, payload = HOOKS["user_group"].build_create_payload(rec, ctx)
    assert paths == ["/v3/user-group"]
    assert payload["idms_id"] == 300
    assert "viewer_user_ids" not in payload
    assert "viewer_user_group_ids" not in payload
    assert "user_ids" not in payload


def test_user_group_hook_skips_on_unresolved_idms():
    ctx = _fake_ctx(id_map={"idms": {}})
    rec = {"name": "Group1", "__srcid__idms_id": 3}
    assert HOOKS["user_group"].build_create_payload(rec, ctx) is None
