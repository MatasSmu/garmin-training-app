#!/usr/bin/env python3
"""Shared Garmin Connect authentication helper.

Used by garmin_export.py and app/ingest/sync.py so both keep the same
login/token-restore behavior instead of duplicating it.
"""

from __future__ import annotations

from getpass import getpass
from pathlib import Path

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)

DEFAULT_TOKEN_DIR = Path.home() / ".garminconnect"


def connect(token_dir: Path = DEFAULT_TOKEN_DIR) -> Garmin:
    """Restore saved tokens, or prompt for credentials on the first run."""
    token_dir = token_dir.expanduser()

    try:
        client = Garmin()
        client.login(str(token_dir))
        print(f"Logged in using tokens from {token_dir}")
        return client
    except (
        FileNotFoundError,
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
    ):
        print("No valid saved login was found. Please sign in.")

    client = Garmin(
        email=input("Garmin email: ").strip(),
        password=getpass("Garmin password: "),
        prompt_mfa=lambda: input("MFA code: ").strip(),
    )
    client.login(str(token_dir))
    print(f"Login successful; tokens saved in {token_dir}")
    return client
