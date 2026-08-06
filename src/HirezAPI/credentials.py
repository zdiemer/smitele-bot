"""Credential loading, from the environment or from config.json.

Resolves the long-standing "TODO: Implement an environment variable method for
loading secrets" in smitele_bot. Both sources are supported and the environment
wins, so a checkout with a config.json keeps working untouched while a
container can be handed a Secret without baking a file into the image.

The three secrets and the variables that carry them:

    discordToken  SMITELE_DISCORD_TOKEN
    hirezDevId    SMITELE_HIREZ_DEV_ID
    hirezAuthKey  SMITELE_HIREZ_AUTH_KEY
    egressProxy   SMITELE_EGRESS_PROXY

`egressProxy` is never required — empty means tracker.gg traffic leaves from the
host's own address — but it lives here because it carries a password and belongs
wherever the other secrets do.
"""

from __future__ import annotations

import json
import os
from json.decoder import JSONDecodeError
from typing import Dict

import paths

ENV_BY_KEY: Dict[str, str] = {
    "discordToken": "SMITELE_DISCORD_TOKEN",
    "hirezDevId": "SMITELE_HIREZ_DEV_ID",
    "hirezAuthKey": "SMITELE_HIREZ_AUTH_KEY",
    "egressProxy": "SMITELE_EGRESS_PROXY",
}


def load(*required: str) -> Dict[str, str]:
    """Return the merged config, verifying every `required` key is present.

    A missing or malformed config.json is only an error if the environment
    didn't supply what was asked for — that's the normal container case, where
    no such file exists at all.
    """
    config: Dict[str, str] = {}

    try:
        with open(paths.CONFIG_FILE, "r", encoding="utf-8") as file:
            config = json.load(file)
    except (FileNotFoundError, JSONDecodeError):
        config = {}

    for key, env_var in ENV_BY_KEY.items():
        value = os.environ.get(env_var)
        if value:
            config[key] = value

    missing = [key for key in required if not config.get(key)]
    if any(missing):
        raise RuntimeError(
            "Missing required credentials: "
            + ", ".join(f"{key} (set {ENV_BY_KEY.get(key, key)})" for key in missing)
            + f" — provide them in the environment or in {paths.CONFIG_FILE}."
        )

    return config
