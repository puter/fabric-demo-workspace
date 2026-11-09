"""Secret resolution: environment variable first, then Azure Key Vault.

Secrets are never read from YAML and never logged. In Azure Container Apps
the Key Vault is reached with the app's managed identity
(DefaultAzureCredential); locally, `az login` works the same way.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# logical name -> (environment variable, Key Vault secret name)
SECRETS = {
    "ohgo_api_key": ("OHGO_API_KEY", "ohgo-api-key"),
    "eventstream_connection_string": ("MVOI_EVENTSTREAM_CONNECTION_STRING",
                                      "eventstream-connection-string"),
    "replay_eventstream_connection_string": ("MVOI_REPLAY_EVENTSTREAM_CONNECTION_STRING",
                                             "replay-eventstream-connection-string"),
}


class SecretResolver:
    def __init__(self, key_vault_url: str | None = None, env: dict[str, str] | None = None):
        self._kv_url = key_vault_url
        self._env = os.environ if env is None else env
        self._client = None

    def get(self, name: str, required: bool = True) -> str | None:
        env_var, kv_name = SECRETS[name]
        value = self._env.get(env_var)
        if value:
            return value
        if self._kv_url:
            value = self._from_key_vault(kv_name)
            if value:
                return value
        if required:
            raise RuntimeError(
                f"secret '{name}' not found: set {env_var} or Key Vault secret '{kv_name}'")
        return None

    def _from_key_vault(self, kv_name: str) -> str | None:
        try:
            if self._client is None:
                from azure.identity import DefaultAzureCredential
                from azure.keyvault.secrets import SecretClient

                self._client = SecretClient(self._kv_url, DefaultAzureCredential())
            return self._client.get_secret(kv_name).value
        except Exception as exc:  # noqa: BLE001 — report without leaking details
            log.error("key vault lookup failed", extra={"secret": kv_name,
                                                        "error": exc.__class__.__name__})
            return None
