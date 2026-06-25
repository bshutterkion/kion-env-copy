"""Load configuration for a single Kion install from a .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass
class Config:
    url: str
    api_key: str
    default_permission_scheme_id: int | None
    verify_ssl: bool
    api_prefix: str

    @classmethod
    def load(cls, env_file: str = ".env") -> "Config":
        # override=True so the chosen --env-file wins over anything already exported
        # in the shell (e.g. a stale KION_URL).
        load_dotenv(env_file, override=True)

        url = (os.environ.get("KION_URL") or "").strip().rstrip("/")
        api_key = (os.environ.get("KION_API_KEY") or "").strip()

        missing = [
            name
            for name, val in (("KION_URL", url), ("KION_API_KEY", api_key))
            if not val
        ]
        if missing:
            raise SystemExit(
                f"Missing required config in {env_file}: {', '.join(missing)}. "
                f"Copy .env.example and fill it in."
            )

        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        raw_scheme = (os.environ.get("DEFAULT_PERMISSION_SCHEME_ID") or "").strip()
        default_scheme = int(raw_scheme) if raw_scheme else None

        verify = (os.environ.get("KION_VERIFY_SSL") or "true").strip().lower() not in (
            "false",
            "0",
            "no",
        )

        # "/api" for hosted installs; "" (root) when hitting the app directly.
        api_prefix = os.environ.get("KION_API_PREFIX")
        api_prefix = "/api" if api_prefix is None else api_prefix.strip()

        return cls(
            url=url,
            api_key=api_key,
            default_permission_scheme_id=default_scheme,
            verify_ssl=verify,
            api_prefix=api_prefix,
        )
