from __future__ import annotations

import hmac
import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .actions import ActionToolbox
from .audit import ActionNotFoundError, ActionStateError, AuditLogger
from .config import Settings, load_settings
from .copilot import FleetCopilotService
from .grounding_judge import JudgeUnavailable, build_samples, run_judge
from .judge_keys import vault_from_env
from .identity import (
    DUMMY_PASSWORD_HASH,
    LoginThrottle,
    hash_password,
    hash_token,
    new_session_token,
    tenant_id_from_name,
    validate_password,
    verify_password,
)
from .models import (
    DEFAULT_LOOKBACK_MINUTES,
    MAX_LOOKBACK_MINUTES,
    ActionExecutionResponse,
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    CopilotQueryRequest,
    CopilotQueryResponse,
    FleetSnapshotIn,
    GroundingJudgeRequest,
    JudgeKeySaveRequest,
    LoginRequest,
    TelemetrySnapshotIn,
    TenantCreateIn,
    UserCreateIn,
)
from .seed import ensure_demo_account, seed_if_empty
from .store import TelemetryStore
from .uploads import MAX_UPLOAD_BYTES, UnsupportedUploadFormat, UploadRejected, parse_upload


@dataclass(frozen=True)
class Principal:
    """The authenticated caller. Tenant and role come from the server-side session or key, never from the client."""

    tenant_id: str
    role: str
    user_id: str | None
    email: str | None


def _format_sse(event: str, payload: Any) -> str:
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"


def create_app(settings: Settings | None = None) -> FastAPI:
    active = settings or load_settings()
    active.data_dir.mkdir(parents=True, exist_ok=True)

    store = TelemetryStore(active.duckdb_path)
    audit = AuditLogger(active.sqlite_audit_path, active.jsonl_audit_path)
    for tenant in sorted(set(active.api_keys.values())):
        store.ensure_tenant(tenant, tenant, datetime.now(tz=UTC))
    # Per email, so one account cannot be guessed. Per client address, so one source cannot spray many accounts.
    demo_password = os.getenv("AFC_DEMO_ACCOUNT_PASSWORD")
    if demo_password:
        ensure_demo_account(store, demo_password, datetime.now(tz=UTC))
    vault = vault_from_env()
    login_throttle = LoginThrottle(max_failures=5, window=timedelta(minutes=15))
    ip_throttle = LoginThrottle(max_failures=20, window=timedelta(minutes=15))
    copilot = FleetCopilotService(
        store,
        audit,
        known_tenants=store.tenant_identities,
        checkpoint_path=active.data_dir / "checkpoints.sqlite3",
    )
    toolbox = ActionToolbox()
    if active.seed_demo:
        seed_if_empty(store, set(active.api_keys.values()))

    def principal_for(presented: str | None) -> Principal:
        """Resolve a session token or a service key to a principal. Session tokens are checked first.

        Service keys from configuration are full-tenant admins. Tokens and keys for a deleted tenant are refused.
        """
        if not presented:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing credentials")

        session = store.get_session(hash_token(presented), datetime.now(tz=UTC))
        if session is not None:
            if not store.is_tenant_active(session["tenant_id"]):
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Tenant is no longer active")
            return Principal(
                tenant_id=session["tenant_id"],
                role=session["role"],
                user_id=session["user_id"],
                email=session["email"],
            )

        # Every configured key is compared, so timing does not reveal which matched.
        matched: str | None = None
        for key, tenant in active.api_keys.items():
            if hmac.compare_digest(key.encode(), presented.encode()):
                matched = tenant
        if matched is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
        if not store.is_tenant_active(matched):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Tenant is no longer active")
        return Principal(tenant_id=matched, role="admin", user_id=None, email=None)

    def require_principal(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> Principal:
        token = None
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[len("bearer ") :].strip()
        return principal_for(token or x_api_key)

    def require_tenant(principal: Principal = Depends(require_principal)) -> str:
        return principal.tenant_id

    def require_admin(principal: Principal = Depends(require_principal)) -> Principal:
        if principal.role != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This action requires the admin role")
        return principal

    def require_bootstrap(x_bootstrap_key: str | None = Header(default=None)) -> None:
        if not active.bootstrap_admin_key:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Platform administration is disabled")
        if not x_bootstrap_key or not hmac.compare_digest(
            x_bootstrap_key.encode(), active.bootstrap_admin_key.encode()
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bootstrap key")

    def owns_target(tenant_id: str, tool_name: str, tool_payload: dict[str, Any]) -> bool:
        """Defense in depth: approved payloads must still reference this tenant's own devices and employees."""
        if tool_name == "notify_employee":
            return store.employee_has_device(tenant_id, tool_payload["employee_id"])
        return store.get_device(tenant_id, tool_payload["device_id"]) is not None

    app = FastAPI(title=active.app_name, version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=active.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": active.app_name}

    @app.post("/api/v1/fleet/snapshots", status_code=status.HTTP_201_CREATED)
    def ingest_fleet(snapshot: FleetSnapshotIn, tenant_id: str = Depends(require_tenant)) -> dict[str, str]:
        snapshot_id = store.ingest_fleet_snapshot(tenant_id, snapshot)
        audit.append_event(
            "fleet.snapshot.ingested",
            tenant_id,
            {"snapshot_id": snapshot_id, "device_count": len(snapshot.devices), "source": snapshot.source},
        )
        return {"snapshot_id": snapshot_id}

    @app.get("/api/v1/fleet/devices")
    def list_devices(tenant_id: str = Depends(require_tenant)) -> dict[str, list[dict[str, Any]]]:
        return {"devices": [asdict(device) for device in store.list_devices(tenant_id)]}

    @app.post("/api/v1/telemetry/snapshots", status_code=status.HTTP_201_CREATED)
    def ingest_legacy_snapshot(snapshot: TelemetrySnapshotIn, tenant_id: str = Depends(require_tenant)) -> dict[str, str]:
        snapshot_id = store.insert_snapshot(tenant_id, snapshot)
        audit.append_event(
            "telemetry.snapshot.ingested",
            tenant_id,
            {"snapshot_id": snapshot_id, "point_count": len(snapshot.points), "source": snapshot.source},
        )
        return {"snapshot_id": snapshot_id}

    @app.post("/api/v1/copilot/query", response_model=CopilotQueryResponse)
    def run_query(payload: CopilotQueryRequest, tenant_id: str = Depends(require_tenant)) -> CopilotQueryResponse:
        return copilot.run_query(tenant_id=tenant_id, request=payload)

    @app.get("/api/v1/copilot/stream")
    def stream_query(
        question: str = Query(min_length=1, max_length=500),
        api_key: str | None = Query(default=None, alias="apiKey"),
        allow_remediation: bool = Query(default=False, alias="allowRemediation"),
        context_window_minutes: int = Query(
            default=DEFAULT_LOOKBACK_MINUTES,
            alias="contextWindowMinutes",
            ge=5,
            le=MAX_LOOKBACK_MINUTES,
        ),
        as_of: str | None = Query(default=None, alias="asOf"),
        llm_planner: bool = Query(default=False, alias="llmPlanner"),
    ) -> StreamingResponse:
        # EventSource cannot send headers, so the credential travels as a query param. Demo trade-off, documented in README.
        principal = principal_for(api_key)
        tenant_id = principal.tenant_id
        request = CopilotQueryRequest(
            question=question,
            allow_remediation=allow_remediation,
            context_window_minutes=context_window_minutes,
            as_of=as_of,
            llm_planner=llm_planner,
        )

        def event_stream() -> Iterator[str]:
            yield _format_sse("start", {"tenant_id": tenant_id, "question": question})
            for event, payload in copilot.stream_query(tenant_id, request):
                yield _format_sse(event, payload)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/v1/copilot/approvals/{action_id}", response_model=ApprovalDecisionResponse)
    def review_remediation_action(
        action_id: str,
        payload: ApprovalDecisionRequest,
        principal: Principal = Depends(require_admin),
    ) -> ApprovalDecisionResponse:
        tenant_id = principal.tenant_id
        # A signed-in reviewer is recorded by identity, so the client cannot name someone else.
        reviewer = principal.email or payload.reviewer
        try:
            action = audit.get_action(action_id=action_id, tenant_id=tenant_id)
            decision_at = audit.resolve_action(
                action_id=action_id,
                tenant_id=tenant_id,
                decision=payload.decision,
                reviewer=reviewer,
                comment=payload.comment,
            )
        except ActionNotFoundError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ActionStateError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error

        graph_resumed = copilot.resume(
            action["payload"].get("thread_id", ""),
            {"decision": payload.decision, "reviewer": reviewer},
        )
        return ApprovalDecisionResponse(
            action_id=action_id,
            status="approved" if payload.decision == "approve" else "rejected",
            reviewer=reviewer,
            decision_at=decision_at,
            graph_resumed=graph_resumed,
        )

    @app.post("/api/v1/copilot/actions/{action_id}/execute", response_model=ActionExecutionResponse)
    def execute_approved_action(action_id: str, tenant_id: str = Depends(require_tenant)) -> ActionExecutionResponse:
        try:
            action = audit.get_action(action_id=action_id, tenant_id=tenant_id)
        except ActionNotFoundError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

        tool_name = action["payload"]["tool_name"]
        tool_payload = action["payload"]["tool_payload"]
        if action["status"] == "executed":
            return ActionExecutionResponse(
                action_id=action_id,
                executed=True,
                tool_name=tool_name,
                result={"status": "already_executed"},
            )
        if tool_name not in toolbox.tool_names:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsupported tool: {tool_name}")
        if action["status"] != "approved":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Action must be approved before execution")
        if not owns_target(tenant_id, tool_name, tool_payload):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Action target does not belong to this tenant",
            )

        try:
            audit.begin_execution(action_id=action_id, tenant_id=tenant_id)
        except ActionStateError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error

        try:
            result = toolbox.execute(tool_name, tenant_id, tool_payload)
        except Exception:
            audit.revert_execution(action_id=action_id, tenant_id=tenant_id)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Tool execution failed")

        audit.mark_action_executed(action_id=action_id, tenant_id=tenant_id, execution_result=result)
        return ActionExecutionResponse(action_id=action_id, executed=True, tool_name=tool_name, result=result)

    # --- saved judge key (encrypted at rest, expires) ----------------------------------------------

    def _judge_key_view(record: dict[str, Any] | None) -> dict[str, Any]:
        if record is None:
            return {"configured": False}
        return {"configured": True, "last4": record["last4"], "expires_at": record["expires_at"]}

    def _require_user_for_judge_key(principal: Principal) -> str:
        if principal.user_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Saved judge keys belong to a user account. Sign in to save one.",
            )
        return principal.user_id

    @app.get("/api/v1/judge-key")
    def get_judge_key(principal: Principal = Depends(require_principal)) -> dict[str, Any]:
        user_id = _require_user_for_judge_key(principal)
        record = store.get_judge_key(user_id)
        if record and datetime.fromisoformat(record["expires_at"]) <= datetime.now(tz=UTC):
            store.delete_judge_key(user_id)
            audit.append_event("judge_key.expired", principal.tenant_id, {"user_id": user_id})
            record = None
        return _judge_key_view(record)

    @app.put("/api/v1/judge-key")
    def save_judge_key(payload: JudgeKeySaveRequest, principal: Principal = Depends(require_principal)) -> dict[str, Any]:
        user_id = _require_user_for_judge_key(principal)
        if vault is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Saved keys are disabled on this server. Set AFC_JUDGE_KEY_SECRET to enable them.",
            )
        key = payload.openai_api_key.get_secret_value()
        now = datetime.now(tz=UTC)
        expires_at = now + timedelta(days=payload.ttl_days)
        store.save_judge_key(user_id, principal.tenant_id, vault.seal(key), key[-4:], now, expires_at)
        audit.append_event(
            "judge_key.saved",
            principal.tenant_id,
            {"user_id": user_id, "last4": key[-4:], "expires_at": expires_at.isoformat()},
        )
        return _judge_key_view(store.get_judge_key(user_id))

    @app.delete("/api/v1/judge-key")
    def delete_judge_key(principal: Principal = Depends(require_principal)) -> dict[str, Any]:
        user_id = _require_user_for_judge_key(principal)
        if store.delete_judge_key(user_id):
            audit.append_event("judge_key.deleted", principal.tenant_id, {"user_id": user_id})
        return {"configured": False}

    def _usable_saved_key(principal: Principal) -> str | None:
        if principal.user_id is None or vault is None:
            return None
        record = store.get_judge_key(principal.user_id)
        if record is None:
            return None
        if datetime.fromisoformat(record["expires_at"]) <= datetime.now(tz=UTC):
            store.delete_judge_key(principal.user_id)
            audit.append_event("judge_key.expired", principal.tenant_id, {"user_id": principal.user_id})
            return None
        return vault.open(record["ciphertext"])

    @app.post("/api/v1/evals/grounding-judge")
    def grounding_judge(payload: GroundingJudgeRequest, principal: Principal = Depends(require_principal)) -> dict[str, Any]:
        """Run the copilot on a question, then judge its answer with an OpenAI key.

        A key in the request is used for this call only. Without one, the saved key is used if it has not expired.
        Keys are never returned, audited, or written to a report file.
        """
        key = payload.openai_api_key.get_secret_value() if payload.openai_api_key else _usable_saved_key(principal)
        if key is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No judge key: enter one for this run, or save one that has not expired.",
            )
        answer = copilot.run_query(
            principal.tenant_id,
            CopilotQueryRequest(
                question=payload.question,
                allow_remediation=False,
                as_of=payload.as_of,
                context_window_minutes=payload.context_window_minutes,
            ),
        )
        if answer.refusal_reason:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="A refused answer has nothing to judge.")
        if not answer.citations:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="The answer has no citations to judge.")

        samples = build_samples(
            [
                {
                    "question": payload.question,
                    "answer": answer.answer,
                    "citations": [citation.model_dump(mode="json") for citation in answer.citations],
                }
            ]
        )
        try:
            report = run_judge(samples, api_key=key, write_report=False)
        except JudgeUnavailable as error:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from None

        audit.append_event(
            "grounding.judged",
            principal.tenant_id,
            {"question": payload.question, "scores": report["scores"], "judge_model": report["judge_model"]},
        )
        return {
            "question": payload.question,
            "intent": answer.intent,
            "answer": answer.answer,
            "scores": report["scores"],
            "judge_model": report["judge_model"],
            "note": report["note"],
        }

    @app.get("/api/v1/audit/events")
    def recent_events(
        limit: int = Query(default=20, ge=1, le=100),
        tenant_id: str = Depends(require_tenant),
    ) -> dict[str, list[dict[str, Any]]]:
        return {"events": audit.list_recent_events(tenant_id=tenant_id, limit=limit)}

    def normalize_email(raw: str) -> str:
        email = raw.strip().lower()
        if "@" not in email or email.startswith("@") or email.endswith("@"):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Enter a valid email address")
        return email

    # --- sign-in ----------------------------------------------------------------------------------

    @app.post("/api/v1/auth/login")
    def login(payload: LoginRequest, request: Request) -> dict[str, Any]:
        email = normalize_email(payload.email)
        source = f"ip:{request.client.host if request.client else 'unknown'}"
        now = datetime.now(tz=UTC)
        # Checked before the password, so a locked account cannot be probed. Unknown emails are throttled too.
        retry_after = max(
            filter(None, [login_throttle.retry_after(email, now), ip_throttle.retry_after(source, now)]),
            default=None,
        )
        if retry_after is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed sign-in attempts. Try again later.",
                headers={"Retry-After": str(retry_after)},
            )
        user = store.get_user_by_email(email)
        # Hash even when the email is unknown, so response time does not reveal which emails exist.
        password_ok = verify_password(payload.password, user["password_hash"] if user else DUMMY_PASSWORD_HASH)
        if user is None or not password_ok or not store.is_tenant_active(user["tenant_id"]):
            login_throttle.record_failure(email, now)
            ip_throttle.record_failure(source, now)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
        login_throttle.reset(email)

        token, token_hash = new_session_token()
        now = datetime.now(tz=UTC)
        expires_at = now + timedelta(hours=active.session_ttl_hours)
        store.create_session(token_hash, user["user_id"], user["tenant_id"], now, expires_at)
        audit.append_event("auth.login", user["tenant_id"], {"user_id": user["user_id"], "email": email})
        return {
            "token": token,
            "tenant_id": user["tenant_id"],
            "role": user["role"],
            "email": email,
            "expires_at": expires_at.isoformat(),
        }

    @app.post("/api/v1/auth/logout")
    def logout(authorization: str | None = Header(default=None)) -> dict[str, str]:
        if authorization and authorization.lower().startswith("bearer "):
            store.delete_session(hash_token(authorization[len("bearer ") :].strip()))
        return {"status": "logged_out"}

    @app.get("/api/v1/auth/me")
    def who_am_i(principal: Principal = Depends(require_principal)) -> dict[str, Any]:
        return {
            "tenant_id": principal.tenant_id,
            "role": principal.role,
            "email": principal.email,
            "user_id": principal.user_id,
            "auth": "session" if principal.user_id else "service_key",
        }

    # --- tenant users (tenant admin) ----------------------------------------------------------------

    @app.get("/api/v1/tenant/users")
    def list_tenant_users(principal: Principal = Depends(require_admin)) -> dict[str, list[dict[str, Any]]]:
        return {"users": store.list_users(principal.tenant_id)}

    @app.post("/api/v1/tenant/users", status_code=status.HTTP_201_CREATED)
    def create_tenant_user(payload: UserCreateIn, principal: Principal = Depends(require_admin)) -> dict[str, str]:
        email = normalize_email(payload.email)
        if store.get_user_by_email(email) is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is already in use")
        try:
            validate_password(payload.password)
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

        user_id = str(uuid4())
        store.create_user(
            user_id, principal.tenant_id, email, hash_password(payload.password), payload.role, datetime.now(tz=UTC)
        )
        audit.append_event(
            "tenant.user_created",
            principal.tenant_id,
            {"user_id": user_id, "email": email, "role": payload.role, "created_by": principal.user_id or "service_key"},
        )
        return {"user_id": user_id, "email": email, "role": payload.role}

    # --- platform administration (bootstrap key) ----------------------------------------------------

    @app.get("/api/v1/admin/tenants", dependencies=[Depends(require_bootstrap)])
    def list_tenants() -> dict[str, list[dict[str, Any]]]:
        return {"tenants": store.list_tenants()}

    @app.post("/api/v1/admin/tenants", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_bootstrap)])
    def create_tenant(payload: TenantCreateIn) -> dict[str, str]:
        tenant_id = payload.tenant_id or tenant_id_from_name(payload.name)
        if not tenant_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Tenant name must contain letters or digits",
            )
        # Deleted tenant IDs are never reused, so audit history stays unambiguous.
        if store.tenant_exists(tenant_id):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Tenant ID '{tenant_id}' already exists")
        email = normalize_email(payload.admin_email)
        if store.get_user_by_email(email) is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is already in use")

        admin_id = str(uuid4())
        store.create_tenant_with_admin(
            tenant_id,
            payload.name.strip(),
            admin_id,
            email,
            hash_password(payload.admin_password),
            datetime.now(tz=UTC),
        )
        audit.append_event(
            "tenant.created",
            tenant_id,
            {"name": payload.name.strip(), "admin_user_id": admin_id, "admin_email": email},
        )
        return {"tenant_id": tenant_id, "name": payload.name.strip(), "admin_user_id": admin_id}

    @app.delete("/api/v1/admin/tenants/{tenant_id}", dependencies=[Depends(require_bootstrap)])
    def delete_tenant(tenant_id: str) -> dict[str, Any]:
        if not store.tenant_exists(tenant_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        if not store.is_tenant_active(tenant_id):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tenant is already deleted")

        now = datetime.now(tz=UTC)
        removed = store.delete_tenant_data(tenant_id, now)
        # Written after the delete commits. Audit rows are never removed.
        audit.append_event("tenant.deleted", tenant_id, {"deleted_at": now.isoformat(), "removed_rows": removed})
        return {"tenant_id": tenant_id, "status": "deleted", "removed_rows": removed}

    @app.get("/api/v1/admin/tenants/{tenant_id}/audit", dependencies=[Depends(require_bootstrap)])
    def tenant_audit(tenant_id: str, limit: int = Query(default=50, ge=1, le=200)) -> dict[str, list[dict[str, Any]]]:
        if not store.tenant_exists(tenant_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        return {"events": audit.list_recent_events(tenant_id=tenant_id, limit=limit)}

    # --- user-supplied telemetry ----------------------------------------------------------------------

    @app.post("/api/v1/fleet/uploads", status_code=status.HTTP_201_CREATED)
    async def upload_fleet(
        request: Request,
        filename: str = Query(default="upload", min_length=1, max_length=120),
        principal: Principal = Depends(require_principal),
    ) -> dict[str, Any]:
        """Ingest a JSON or CSV file. Raw body with a Content-Type header. All-or-nothing: any error stores nothing."""
        raw = await request.body()
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Upload exceeds {MAX_UPLOAD_BYTES} bytes",
            )
        try:
            snapshots = parse_upload(
                raw,
                request.headers.get("content-type", ""),
                filename,
                source_name=f"upload:{filename}"[:120],
            )
        except UnsupportedUploadFormat as error:
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(error)) from error
        except UploadRejected as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"message": "Upload rejected; nothing was stored", "errors": error.errors},
            ) from error

        snapshot_ids = store.ingest_fleet_snapshots(principal.tenant_id, snapshots)
        device_count = sum(len(snapshot.devices) for snapshot in snapshots)
        audit.append_event(
            "fleet.upload.ingested",
            principal.tenant_id,
            {"filename": filename, "snapshot_ids": snapshot_ids, "device_count": device_count},
        )
        return {"snapshot_ids": snapshot_ids, "snapshot_count": len(snapshot_ids), "device_count": device_count}

    return app


_default_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    """Build the default app on first access so importing this module has no side effects (uvicorn reads `app`)."""
    global _default_app
    if name == "app":
        if _default_app is None:
            _default_app = create_app()
        return _default_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
