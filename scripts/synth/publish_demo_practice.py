"""Publish bundled demo practice through the same QC HTTP flow as the web UI."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import yaml
from regagent_backend.qc_models import CreateTicket, PublishDraft

from deploy.prod.deploy import LoopbackHttp, _sign_in, parse_env


def _json_request(
    http: LoopbackHttp,
    method: str,
    path: str,
    token: str,
    *,
    payload: dict[str, Any] | None = None,
    expected: int = 200,
) -> dict[str, Any]:
    status, _, body = http.request(method, path, token=token, payload=payload)
    if status != expected:
        raise RuntimeError(f"{method} {path} returned HTTP {status}")
    result: dict[str, Any] = json.loads(body)
    return result


def _seeds(path: Path) -> list[tuple[str, CreateTicket, PublishDraft]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("practices"), list):
        raise ValueError("invalid demo practice file")
    seeds = []
    for item in data["practices"]:
        key = item["key"]
        if not isinstance(key, str) or not key:
            raise ValueError("practice key is required")
        ticket = CreateTicket(
            submission_id=f"demo-seed:{key}",
            question=item["question"],
            comment=item.get("comment"),
        )
        seeds.append((key, ticket, PublishDraft.model_validate(item["draft"])))
    if len({key for key, _, _ in seeds}) != len(seeds):
        raise ValueError("duplicate practice key")
    return seeds


def publish(root: Path, seed_file: Path, wait_seconds: int) -> None:
    seeds = _seeds(seed_file)
    env = parse_env((root / ".env").read_text(encoding="utf-8"))
    base = env["PUBLIC_BASE_URL"].rstrip("/")
    client_id = env.get("VITE_OIDC_CLIENT_ID") or "regagent-web"
    issuer = f"{base}/auth/realms/regagent"
    port = int(env.get("REGAGENT_ORIGIN_PORT") or "18082")
    employee = LoopbackHttp(port, base)
    reviewer = LoopbackHttp(port, base)
    employee_token = _sign_in(
        employee,
        issuer,
        client_id,
        base,
        "employee_public",
        env["KEYCLOAK_EMPLOYEE_PUBLIC_PASSWORD"],
    )
    reviewer_token = _sign_in(
        reviewer, issuer, client_id, base, "admin", env["KEYCLOAK_ADMIN_DEMO_PASSWORD"]
    )
    for key, ticket, draft in seeds:
        created = _json_request(
            employee,
            "POST",
            "/api/qc/tickets",
            employee_token,
            payload=ticket.model_dump(),
            expected=201,
        )
        ticket_id = created["id"]
        detail = _json_request(
            reviewer, "GET", f"/api/qc/admin/tickets/{ticket_id}", reviewer_token
        )
        if detail["status"] == "new":
            detail = _json_request(
                reviewer,
                "POST",
                f"/api/qc/admin/tickets/{ticket_id}/publish",
                reviewer_token,
                payload=draft.model_dump(),
            )
        elif detail["draft"] != draft.model_dump():
            raise RuntimeError(f"{key}: existing draft differs from the seed")
        deadline = time.monotonic() + wait_seconds
        while detail["status"] == "publishing" and time.monotonic() < deadline:
            time.sleep(3)
            detail = _json_request(
                reviewer, "GET", f"/api/qc/admin/tickets/{ticket_id}", reviewer_token
            )
        print(f"{key}: {detail['status']} ({ticket_id})")
        if detail["status"] != "published":
            raise RuntimeError(f"{key}: publication did not complete")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.home() / "srv/regagent_prod")
    parser.add_argument(
        "--seeds",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "data/practice_seeds.yaml",
    )
    parser.add_argument("--wait-seconds", type=int, default=900)
    parser.add_argument(
        "--check", action="store_true", help="Validate seeds without contacting the stand"
    )
    args = parser.parse_args()
    seeds = _seeds(args.seeds)
    if args.check:
        print(f"Valid practice seeds: {len(seeds)}")
        return
    publish(args.root, args.seeds, args.wait_seconds)


if __name__ == "__main__":
    main()
