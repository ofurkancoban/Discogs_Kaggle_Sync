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
    """Reads the browser session exported from a logged-in Kaggle tab."""
    if not path.exists():
        raise KaggleWebSessionError(
            f"No Kaggle web session at {path}. Copy the 'Cookie' and 'x-xsrf-token' "
            "request headers from a logged-in kaggle.com tab into that file or set "
            "KAGGLE_USER and KAGGLE_PASSWORD in environment / .env for auto-login."
        )
    session = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in ("cookie", "xsrfToken") if not session.get(k)]
    if missing:
        raise KaggleWebSessionError(f"{path} is missing: {', '.join(missing)}")
    return session


def auto_login(
    user_identifier: str | None = None,
    password: str | None = None,
    path: Path = DEFAULT_SESSION_PATH,
) -> dict[str, str]:
    """Automatically authenticates to Kaggle via HTTP login request using credentials.

    Reads user_identifier (email/username) and password from parameters or environment
    variables KAGGLE_USER / KAGGLE_USERNAME / KAGGLE_EMAIL and KAGGLE_PASSWORD (or .env).
    Saves the resulting session cookies and XSRF token to `path` (default ~/.kaggle/web_session.json).
    """
    import os
    user_identifier = (
        user_identifier
        or os.getenv("KAGGLE_EMAIL")
        or os.getenv("KAGGLE_USERNAME")
        or os.getenv("KAGGLE_USER")
    )
    password = password or os.getenv("KAGGLE_PASSWORD")

    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip("'\"")
            if not user_identifier and k in ("KAGGLE_EMAIL", "KAGGLE_USERNAME", "KAGGLE_USER"):
                user_identifier = v
            if not password and k == "KAGGLE_PASSWORD":
                password = v

    if not user_identifier or not password:
        raise KaggleWebSessionError(
            "Automatic login requires credentials. Set KAGGLE_USER (or KAGGLE_EMAIL) "
            "and KAGGLE_PASSWORD environment variables or add them to .env file."
        )

    session = requests.Session()

    init_res = session.get("https://www.kaggle.com/account/login", timeout=30)
    init_res.raise_for_status()

    xsrf_token = session.cookies.get("XSRF-TOKEN") or session.cookies.get("CSRF-TOKEN")
    if not xsrf_token:
        raise KaggleWebSessionError("Could not retrieve XSRF token from Kaggle login page.")

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "origin": "https://www.kaggle.com",
        "referer": "https://www.kaggle.com/account/login",
        "x-xsrf-token": xsrf_token,
    }

    payloads = [
        {"email": user_identifier, "password": password},
        {"userIdentifier": user_identifier, "password": password},
        {"username": user_identifier, "password": password},
    ]

    last_resp = None
    for payload in payloads:
        try:
            resp = session.post(
                "https://www.kaggle.com/api/i/users.LegacyUsersService/EmailSignIn",
                json=payload,
                headers=headers,
                timeout=30,
            )
            last_resp = resp
            if resp.status_code == 200 and ("redirectUrl" in resp.text or "__Host-KAGGLEID" in session.cookies):
                break
        except Exception:
            continue

    if not session.cookies or ("__Host-KAGGLEID" not in session.cookies and "ka_db" not in session.cookies):
        detail = last_resp.text if last_resp else "No response"
        raise KaggleWebSessionError(
            f"Kaggle automatic login failed (status {getattr(last_resp, 'status_code', 'unknown')}): {detail}"
        )

    # The sign-in response only sets __Host-KAGGLEID/ka_db; CLIENT-TOKEN (the JWT the
    # internal api/i/* services actually authenticate against) still holds the
    # pre-login anonymous identity at this point and every api/i/* call 400s until it's
    # refreshed. Loading any page mints a fresh CLIENT-TOKEN tied to the now-authenticated
    # session cookies, so do that before exporting the session.
    home_res = session.get("https://www.kaggle.com/", timeout=30)
    home_res.raise_for_status()

    cookie_parts = [f"{k}={v}" for k, v in session.cookies.items()]
    cookie_str = "; ".join(cookie_parts)
    xsrf = session.cookies.get("XSRF-TOKEN") or xsrf_token

    session_data = {"cookie": cookie_str, "xsrfToken": xsrf}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session_data, indent=2), encoding="utf-8")
    path.chmod(0o600)
    logger.info("Successfully refreshed Kaggle web session at %s", path)
    return session_data


def session_expiry(session: dict[str, str] | None = None) -> datetime | None:
    """Returns when the session's client token stops being valid, if it can be read."""
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


def check_session(session: dict[str, str] | None = None, auto_refresh: bool = True) -> tuple[bool, str]:
    """Confirms the exported session still authenticates, before relying on it."""
    try:
        session = session or load_session()
    except KaggleWebSessionError as e:
        if auto_refresh:
            try:
                session = auto_login()
            except Exception as login_err:
                return False, f"{e} (Auto-login failed: {login_err})"
        else:
            return False, str(e)

    client = KaggleWebClient(session)
    try:
        user = client._post("users.UsersService/GetCurrentUser", {})
    except KaggleWebSessionError as e:
        if auto_refresh:
            try:
                session = auto_login()
                client = KaggleWebClient(session)
                user = client._post("users.UsersService/GetCurrentUser", {})
            except Exception as login_err:
                return False, f"Session expired ({e}); auto-login failed: {login_err}"
        else:
            return False, str(e)
    except requests.RequestException as e:
        return False, f"Could not confirm the Kaggle session: {e}"

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
        if response.status_code in (400, 401, 403):
            # A stale CLIENT-TOKEN (the pre-login/anonymous one, e.g. from a session
            # exported before the homepage reload that mints the authenticated one -
            # see auto_login) makes every api/i/* call 400 rather than 401/403, so it
            # has to be treated the same as an expired session.
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

    def usability_rating(self, dataset_id: int) -> dict:
        """Returns the same score breakdown shown on the dataset page's "Pending Actions"
        panel (score, columnDescriptionScore, provenanceScore, publicKernelScore, etc.) -
        the only reliable way to confirm a dataset is actually complete, since the
        documented API can report a publish as "successful" while several of these
        checklist items are still unmet (see module docstring)."""
        return self._post(
            "datasets.DatasetDetailService/GetDatasetUsabilityRating",
            {"datasetId": dataset_id, "hashLink": ""},
        ).get("rating") or {}

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

    def update_provenance(
        self,
        version_id: int,
        user_specified_sources: str,
        collection_methods: str,
    ) -> dict:
        """Writes the Provenance section's Sources and Collection Methodology fields on
        the dataset's current version.

        Unlike userSpecifiedSources on the public API's DatasetSettings
        (kaggle_publish.update_dataset_settings), this is the call that actually triggers
        Kaggle's usability-rating recompute - the public API accepts and stores that field
        fine, but the "Specify provenance" checklist item (and every other checklist item,
        expectedUpdateFrequency included) never clears until something calls this service,
        and collectionMethods has no field in DatasetSettings at all to write through the
        public API in the first place. Returns the resulting usability rating breakdown
        (this call's response *is* the rating, not a separate confirmation).
        """
        return self._post(
            "datasets.DatasetService/UpdateDatasetMetadata",
            {
                "datasetVersionId": version_id,
                "datasetVersionMetadata": {
                    "collectionMethods": collection_methods,
                    "datasetVersionAuthors": [],
                    "userSpecifiedSources": user_specified_sources,
                    "citations": [],
                },
                "updateMask": "collectionMethods,userSpecifiedSources",
            },
        )


def apply_provenance(
    owner_slug: str,
    dataset_slug: str,
    user_specified_sources: str,
    collection_methods: str,
    client: KaggleWebClient | None = None,
) -> dict:
    """Applies Sources and Collection Methodology to the dataset's current version, which
    also triggers Kaggle to recompute the usability rating (including expectedUpdateFrequency,
    already set via the public API but never scored until this fires). Returns the
    resulting usability rating breakdown."""
    client = client or KaggleWebClient()
    basics = client.dataset_basics(owner_slug, dataset_slug)
    version_id = basics.get("datasetVersionId")
    if not version_id:
        raise RuntimeError(f"{owner_slug}/{dataset_slug} has no datasetVersionId yet.")
    return client.update_provenance(version_id, user_specified_sources, collection_methods)


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
