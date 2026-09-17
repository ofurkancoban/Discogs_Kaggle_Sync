"""Fills the per-file and per-column descriptions that Kaggle's public API ignores.

The documented dataset API (DatasetApiService/UpdateDatasetMetadata) accepts a file
description and a column schema and then silently drops both. That has been open since
2020 as kaggle-api#248, and it still reproduces on the current client: the request goes
out correctly formed, comes back without an error, and nothing changes.

Kaggle's own web UI writes these fields through a different, undocumented service
(datasets.databundles.DatabundleService) that addresses every file and column by an
opaque Firestore path rather than by name, which is why the public API cannot reach
them. This module drives that same service.

Two consequences worth knowing before relying on it:

- It authenticates by browser session cookie, not by the API token in kaggle.json, so a
  session has to be exported from a logged-in browser (see load_session).
- It is undocumented and can change without notice. Everything else in this project uses
  the supported API; this is the one place that does not.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

API_ROOT = "https://www.kaggle.com/api/i"
DEFAULT_SESSION_PATH = Path.home() / ".kaggle" / "web_session.json"


class KaggleWebSessionError(RuntimeError):
    """Raised when the browser session is missing, malformed, or no longer accepted."""


def load_session(path: Path = DEFAULT_SESSION_PATH) -> dict[str, str]:
    """Reads the browser session exported from a logged-in Kaggle tab.

    The file is JSON with two keys, both copied from any authenticated request in the
    browser's network inspector:

        {"cookie": "<the whole Cookie request header>",
         "xsrfToken": "<the x-xsrf-token request header>"}
    """
    if not path.exists():
        raise KaggleWebSessionError(
            f"No Kaggle web session at {path}. Copy the 'Cookie' and 'x-xsrf-token' "
            "request headers from a logged-in kaggle.com tab into that file as "
            '{"cookie": "...", "xsrfToken": "..."}.'
        )
    session = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in ("cookie", "xsrfToken") if not session.get(k)]
    if missing:
        raise KaggleWebSessionError(f"{path} is missing: {', '.join(missing)}")
    return session


def session_expiry(session: dict[str, str] | None = None) -> datetime | None:
    """Returns when the session's client token stops being valid, if it can be read.

    The token is an unsigned JWT sitting in the cookie jar; only its expiry is read here,
    to warn before a monthly run silently loses the ability to write descriptions.
    """
    session = session or load_session()
    jar = dict(
        part.strip().split("=", 1)
        for part in session["cookie"].split(";")
        if "=" in part
    )
    token = jar.get("CLIENT-TOKEN")
    if not token or token.count(".") < 2:
        return None
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return datetime.fromisoformat(claims["exp"].replace("Z", "+00:00"))
    except Exception:
        return None


def check_session(session: dict[str, str] | None = None) -> tuple[bool, str]:
    """Confirms the exported session still authenticates, before relying on it."""
    session = session or load_session()
    client = KaggleWebClient(session)
    try:
        user = client._post("users.UsersService/GetCurrentUser", {})
    except KaggleWebSessionError as e:
        return False, str(e)

    name = user.get("displayName") or user.get("userName")
    if not name:
        return False, "Kaggle answered but did not recognise the session as signed in."

    expiry = session_expiry(session)
    if expiry:
        days = (expiry - datetime.now(timezone.utc)).days
        return True, f"Signed in as {name}; session valid for about {days} more day(s)."
    return True, f"Signed in as {name}."


def save_session_from_curl(curl_text: str, path: Path = DEFAULT_SESSION_PATH) -> Path:
    """Builds the session file out of a 'Copy as cURL' command from the browser.

    Kaggle does not refresh these cookies on the requests this module makes (no
    Set-Cookie comes back), and the client token is minted for 30 days, so the session
    has to be re-exported by hand every so often. Parsing the browser's own clipboard
    format keeps that chore to a paste instead of hand-editing JSON.
    """
    cookie = None
    xsrf = None

    # Both `-b '<cookies>'` and `-H 'cookie: <cookies>'` appear depending on the browser.
    for pattern, setter in (
        (r"-b\s+'([^']*)'", "cookie"),
        (r"-H\s+'cookie:\s*([^']*)'", "cookie"),
        (r"-H\s+'x-xsrf-token:\s*([^']*)'", "xsrf"),
    ):
        match = re.search(pattern, curl_text, re.IGNORECASE)
        if match:
            if setter == "cookie" and not cookie:
                cookie = match.group(1).strip()
            elif setter == "xsrf":
                xsrf = match.group(1).strip()

    if not cookie or not xsrf:
        missing = []
        if not cookie:
            missing.append("the Cookie header")
        if not xsrf:
            missing.append("the x-xsrf-token header")
        raise KaggleWebSessionError(
            "Could not read " + " or ".join(missing) + " out of that cURL command. Copy it "
            "from a request to kaggle.com/api/i/... in a logged-in tab."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookie": cookie, "xsrfToken": xsrf}, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return path


class KaggleWebClient:
    def __init__(self, session: dict[str, str] | None = None):
        self._session = session or load_session()
        self._http = requests.Session()

    def _post(self, service_method: str, payload: dict, referer: str = "https://www.kaggle.com/") -> dict:
        response = self._http.post(
            f"{API_ROOT}/{service_method}",
            json=payload,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": "https://www.kaggle.com",
                "referer": referer,
                "cookie": self._session["cookie"],
                "x-xsrf-token": self._session["xsrfToken"],
            },
            timeout=60,
        )
        if response.status_code in (401, 403):
            raise KaggleWebSessionError(
                f"{service_method} returned {response.status_code}. The exported session has "
                "most likely expired; copy a fresh cookie and x-xsrf-token from the browser."
            )
        if response.status_code == 404:
            # The UI hits this whenever a page outlives the version it was rendered from,
            # e.g. after the dataset is deleted or a new version replaces it.
            raise RuntimeError(
                f"{service_method} returned 404. The dataset or version referenced no longer "
                "exists; re-read the current version before writing to it."
            )
        response.raise_for_status()
        return response.json()

    def dataset_basics(self, owner_slug: str, dataset_slug: str) -> dict:
        """Returns the dataset id plus the current version's Firestore path and id."""
        return self._post(
            "datasets.DatasetDetailService/GetDatasetBasics",
            {"ownerSlug": owner_slug, "datasetSlug": dataset_slug},
            referer=f"https://www.kaggle.com/datasets/{owner_slug}/{dataset_slug}",
        )

    def file_columns(self, dataset_id: int, version_id: int, file_firestore_path: str) -> list[dict]:
        """Returns each column of one file with the Firestore path needed to write to it."""
        result = self._post(
            "datasets.databundles.DatabundleService/GetDatabundleExternalColumns",
            {
                "verificationInfo": {"datasetId": dataset_id, "databundleVersionId": version_id},
                "firestorePath": file_firestore_path,
            },
        )
        return result.get("columns") or []

    def column_details(self, dataset_id: int, version_id: int, column_paths: list[str]) -> list[dict]:
        """Returns Kaggle's detected type for each column, so updates can preserve it."""
        if not column_paths:
            return []
        result = self._post(
            "datasets.databundles.DatabundleService/GetDatabundleExternalColumnsByFirestorePath",
            {
                "verificationInfo": {"datasetId": dataset_id, "databundleVersionId": version_id},
                "firestorePaths": column_paths,
            },
        )
        return result.get("columns") or []

    def update_file_metadata(
        self,
        dataset_id: int,
        version_id: int,
        file_firestore_path: str,
        description: str,
        columns: list[dict],
    ) -> dict:
        """Writes one file's description and all of its column descriptions.

        Returns Kaggle's usability rating breakdown, which is the only reliable way to
        confirm the write landed: the public read APIs never return these fields.
        """
        result = self._post(
            "datasets.databundles.DatabundleService/UpdateDatabundleMetadataExternal",
            {
                "firestorePath": file_firestore_path,
                "description": description,
                "columns": columns,
                # Without this the server rejects the write with "You must specify
                # exactly one databundle source".
                "verificationInfo": {"datasetId": dataset_id, "databundleVersionId": version_id},
            },
        )
        return result.get("usabilityRating") or {}


def apply_descriptions(
    owner_slug: str,
    dataset_slug: str,
    file_descriptions: dict[str, str],  # csv file name -> description
    column_descriptions: dict[str, dict[str, str]],  # csv file name -> {column -> description}
    client: KaggleWebClient | None = None,
) -> dict:
    """Applies file and column descriptions to the dataset's current version.

    Only files present in both `file_descriptions` and the live version are touched.
    Returns the final usability rating breakdown.
    """
    client = client or KaggleWebClient()

    basics = client.dataset_basics(owner_slug, dataset_slug)
    dataset_id = basics["datasetId"]
    data = basics.get("data") or {}
    version_path = data.get("firestorePath")
    version_id = data.get("versionId")
    if not version_path or not version_id:
        raise RuntimeError(
            f"{owner_slug}/{dataset_slug} has no readable version yet; wait for the upload "
            "to finish processing before writing descriptions."
        )

    rating: dict = {}
    for file_name, file_description in file_descriptions.items():
        file_path = f"{version_path}/files/{file_name}"
        columns = client.file_columns(dataset_id, version_id, file_path)
        if not columns:
            logger.warning("%s: no columns reported by Kaggle, skipping", file_name)
            continue

        details = client.column_details(
            dataset_id, version_id, [c["firestorePath"] for c in columns]
        )
        type_by_path = {
            d["path"]: (d.get("tableColumnInfo") or {}) for d in details
        }

        wanted = column_descriptions.get(file_name, {})
        payload_columns = []
        undescribed = []
        for column in columns:
            path = column["firestorePath"]
            info = type_by_path.get(path, {})
            description = wanted.get(column["name"], "")
            if not description:
                undescribed.append(column["name"])
            payload_columns.append({
                "firestorePath": path,
                "description": description,
                # Echo back Kaggle's own detected typing; omitting it resets the column.
                "extendedType": info.get("extendedType", "EXTENDED_DATA_TYPE_UNSPECIFIED"),
                "type": info.get("type", "STRING"),
            })

        if undescribed:
            logger.warning(
                "%s: %d column(s) have no description and will be left blank: %s",
                file_name, len(undescribed), ", ".join(undescribed[:10]),
            )

        rating = client.update_file_metadata(
            dataset_id, version_id, file_path, file_description, payload_columns
        )
        logger.info(
            "%s: wrote description and %d column(s). Usability now %.4f "
            "(file=%s, columns=%s)",
            file_name, len(payload_columns), rating.get("score", 0),
            rating.get("fileDescriptionScore"), rating.get("columnDescriptionScore"),
        )

    return rating
