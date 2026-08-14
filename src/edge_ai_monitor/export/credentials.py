"""Resolving sink credentials from outside the service definition.

Open Horizon service definitions and deployment policies are stored in the
exchange and readable by anyone who can query it, so a credential written there
is effectively published. Sinks therefore name where their secret lives —
``token_env`` or ``token_file`` — rather than carrying its value.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# Settings that hold secrets. Each may be given directly, or via <name>_env or
# <name>_file, which are resolved here.
SECRET_KEYS = ("token", "password", "api_key", "secret")

ENV_SUFFIX = "_env"
FILE_SUFFIX = "_file"


class MissingCredential(Exception):
    """A sink named a credential source that does not resolve."""


def resolve_settings(
    name: str, settings: Dict[str, Any], env: Dict[str, str]
) -> Tuple[Dict[str, Any], List[str]]:
    """Return settings with secrets resolved, plus the secret values found.

    The returned secret list is handed to the sink so it can scrub those values
    out of anything it logs.
    """
    resolved = dict(settings)
    secrets: List[str] = []

    for key in SECRET_KEYS:
        env_key = f"{key}{ENV_SUFFIX}"
        file_key = f"{key}{FILE_SUFFIX}"

        if env_key in resolved:
            variable = str(resolved.pop(env_key))
            value = env.get(variable)
            if not value:
                raise MissingCredential(
                    f"sink '{name}' expects credential in ${variable}, which is unset"
                )
            resolved[key] = value
            secrets.append(value)

        elif file_key in resolved:
            path = Path(str(resolved.pop(file_key)))
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise MissingCredential(
                    f"sink '{name}' could not read credential file {path}: {exc}"
                ) from None
            if not value:
                raise MissingCredential(f"sink '{name}' credential file {path} is empty")
            resolved[key] = value
            secrets.append(value)

        elif key in resolved and resolved[key]:
            # Supplied inline. Works, but the value is only as private as
            # whatever holds the configuration.
            logger.warning(
                "sink '%s' has an inline %s; prefer %s or %s so the secret stays "
                "out of the service definition",
                name,
                key,
                env_key,
                file_key,
            )
            secrets.append(str(resolved[key]))

    return resolved, secrets


def redact_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """A copy safe to log: secret values replaced with a marker."""
    safe = {}
    for key, value in settings.items():
        if key in SECRET_KEYS and value:
            safe[key] = "***"
        else:
            safe[key] = value
    return safe
