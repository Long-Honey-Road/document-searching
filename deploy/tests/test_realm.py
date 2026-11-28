import json
from pathlib import Path

REALM_PATH = Path(__file__).resolve().parents[1] / "keycloak" / "regagent-realm.json"


def test_realm_uses_oidc_code_flow_with_pkce() -> None:
    realm = json.loads(REALM_PATH.read_text())
    assert realm["realm"] == "regagent"
    assert realm["bruteForceProtected"]
    assert not realm["registrationAllowed"]

    [client] = realm["clients"]
    assert client["clientId"] == "regagent-web"
    assert client["publicClient"] and client["standardFlowEnabled"]
    assert not client["directAccessGrantsEnabled"]
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert "${PUBLIC_BASE_URL}/" in client["redirectUris"]

    mappers = {mapper["protocolMapper"]: mapper["config"] for mapper in client["protocolMappers"]}
    assert mappers["oidc-group-membership-mapper"]["full.path"] == "true"
    assert mappers["oidc-group-membership-mapper"]["claim.name"] == "groups"
    assert mappers["oidc-audience-mapper"]["included.client.audience"] == "regagent-web"


def test_demo_users_have_only_their_assigned_access() -> None:
    realm = json.loads(REALM_PATH.read_text())
    users = {user["username"]: user for user in realm["users"]}
    assert users["employee_public"].get("groups", []) == []
    assert users["employee_fin"]["groups"] == ["/levels/closed_fin"]
    assert users["qc_expert"]["realmRoles"] == ["qc"]
    assert users["qc_expert"].get("groups", []) == []
    assert users["admin"]["realmRoles"] == ["admin"]
    assert users["admin"]["groups"] == ["/levels/closed_fin"]

    for username, user in users.items():
        [credential] = user["credentials"]
        password_var = "ADMIN_DEMO" if username == "admin" else username.upper()
        assert credential["value"] == f"${{KEYCLOAK_{password_var}_PASSWORD}}"
