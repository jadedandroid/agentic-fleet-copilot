from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_API_KEYS: dict[str, str] = {
    "acme-demo-key": "acme",
    "globex-demo-key": "globex",
}


@dataclass(frozen=True)
class Settings:
    app_name: str
    cors_origins: list[str]
    data_dir: Path
    duckdb_path: Path
    sqlite_audit_path: Path
    jsonl_audit_path: Path
    api_keys: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_API_KEYS))
    seed_demo: bool = True
    # Platform admin credential for tenant lifecycle. None disables those endpoints.
    bootstrap_admin_key: str | None = None
    session_ttl_hours: int = 8


def _load_cors_origins() -> list[str]:
    csv_value = os.getenv("AFC_CORS_ORIGINS")
    if csv_value:
        origins = [item.strip() for item in csv_value.split(",") if item.strip()]
        if origins:
            return origins

    single_origin = os.getenv("AFC_CORS_ORIGIN")
    if single_origin:
        return [single_origin]

    return ["http://localhost:3000", "http://127.0.0.1:3000"]


def _load_api_keys() -> dict[str, str]:
    """Parse AFC_API_KEYS as ``key=tenant`` pairs separated by commas."""
    raw = os.getenv("AFC_API_KEYS")
    if not raw:
        return dict(DEFAULT_API_KEYS)

    mapping: dict[str, str] = {}
    for pair in raw.split(","):
        if not pair.strip():
            continue
        key, _, tenant = pair.partition("=")
        if not key.strip() or not tenant.strip():
            raise ValueError("AFC_API_KEYS entries must look like key=tenant")
        mapping[key.strip()] = tenant.strip()
    return mapping


def load_settings() -> Settings:
    base_dir = Path(os.getenv("AFC_DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
    return Settings(
        app_name="Agentic Fleet Copilot",
        cors_origins=_load_cors_origins(),
        data_dir=base_dir,
        duckdb_path=base_dir / "telemetry.duckdb",
        sqlite_audit_path=base_dir / "audit.sqlite3",
        jsonl_audit_path=base_dir / "audit.jsonl",
        api_keys=_load_api_keys(),
        seed_demo=os.getenv("AFC_SEED_DEMO", "true").lower() in {"1", "true", "yes"},
        bootstrap_admin_key=os.getenv("AFC_BOOTSTRAP_ADMIN_KEY") or None,
        session_ttl_hours=int(os.getenv("AFC_SESSION_TTL_HOURS", "8")),
    )
