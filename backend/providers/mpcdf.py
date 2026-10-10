"""MPCDF LLM Inference Service provider (``llm.mpcdf.mpg.de``).

The service is institutionally provided and free to the user, so the admin sets
one job URL and key for everyone (``key_scopes = {"shared"}``). Each endpoint is
an ephemeral Slurm job of up to 8 hours that is started by hand in the service's
web UI, so there is nothing to provision; what this provider adds is knowing
when a job has expired and what to do about it.
"""

import logging
from typing import Optional

from backend.providers.base import Provider
from backend.providers.types import Credentials, Health

logger = logging.getLogger(__name__)

SERVICE_URL = "https://llm.mpcdf.mpg.de"


class MpcdfProvider(Provider):
    id = "mpcdf"
    label = "MPCDF LLM Inference Service"
    key_scopes = frozenset({"shared"})
    default_scope = "shared"
    #: A job either answers or it does not; keep the probe short.
    http_timeout = 10.0

    def key_docs_url(self, env_var: str) -> Optional[str]:
        # Every shared field of this provider (URL and key) comes from a job
        # started in the service's web UI.
        return SERVICE_URL

    def unavailable_hint(self) -> Optional[str]:
        return (
            f"Start a new job in the MPCDF LLM Inference Service ({SERVICE_URL}) and paste "
            "its URL and key into Service API Keys."
        )

    def health(self, creds: Credentials) -> Optional[Health]:
        """Probe ``GET {base_url}/models`` with the shared key.

        200 is ``ready``; 401/403 means the key was rejected; 404, 405, a timeout
        or a connection error means the job expired or never started. Never
        ``cold`` (a job either runs or does not) and never raises.
        """
        if not creds.base_url or not creds.api_key:
            return Health(status="unreachable", detail="not configured")
        from backend.services.admin_settings_store import normalize_base_url

        url = normalize_base_url(creds.base_url) + "/models"
        try:
            resp = self._http().get(url, headers={"Authorization": f"Bearer {creds.api_key}"}, timeout=self.http_timeout)
        except Exception as exc:
            return Health(status="unreachable", detail=f"job expired or not started ({type(exc).__name__})")
        code = resp.status_code
        if 200 <= code < 300:
            return Health(status="ready", detail="")
        if code in (401, 403):
            return Health(status="unreachable", detail=f"key rejected (HTTP {code})")
        if code in (404, 405):
            return Health(status="unreachable", detail=f"job expired or not started (HTTP {code})")
        return Health(status="unreachable", detail=f"HTTP {code}")
