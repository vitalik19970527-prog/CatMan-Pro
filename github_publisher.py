"""GitHub Contents + Releases API client used by the superadmin "Web сайт" tab
to publish site edits and app builds without a local git checkout.

Credentials live in secrets/github_publish.json (never bundled into any
built exe/apk — see build_release.ps1 / flet build apk --exclude and
scripts/prepare_release_payload.py), not in .env, because superadmin_dashboard.py
is compiled into both the customer-facing and the superadmin-only exe.
"""

import base64
import json
import os
import time

import requests

from logger import logger

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "secrets", "github_publish.json")

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"


def load_publish_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        logger.exception("Failed to read %s", CONFIG_PATH)
        return {}


def save_publish_config(cfg: dict) -> bool:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        logger.exception("Failed to write %s", CONFIG_PATH)
        return False


class GitHubPublisher:
    def __init__(self):
        cfg = load_publish_config()
        self.token = (cfg.get("token") or "").strip()
        self.owner = (cfg.get("owner") or "").strip()
        self.repo = (cfg.get("repo") or "").strip()
        self.branch = (cfg.get("branch") or "main").strip()

    @property
    def configured(self) -> bool:
        return bool(self.token and self.owner and self.repo)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }

    def _repo_url(self, path: str = "") -> str:
        return f"{API_BASE}/repos/{self.owner}/{self.repo}{path}"

    # ---------------- Contents API ----------------

    def get_file_sha(self, repo_path: str) -> str | None:
        """Returns the current blob SHA of repo_path on self.branch, or None
        if the file doesn't exist there yet."""
        resp = requests.get(
            self._repo_url(f"/contents/{repo_path}"),
            headers=self._headers(),
            params={"ref": self.branch},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json().get("sha")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return None

    def get_file(self, repo_path: str) -> tuple[bytes | None, str]:
        """Reads repo_path's current content on self.branch directly from
        GitHub (Contents API) — no local disk involved. Returns (None, "")
        if the file doesn't exist (a normal state for e.g. an empty
        screenshot slot), or (None, error) for anything else."""
        if not self.configured:
            return None, "GitHub публікація не налаштована (немає токена/repo)"
        try:
            resp = requests.get(
                self._repo_url(f"/contents/{repo_path}"),
                headers=self._headers(),
                params={"ref": self.branch},
                timeout=30,
            )
        except requests.RequestException as e:
            return None, f"Мережева помилка читання {repo_path}: {e}"
        if resp.status_code == 404:
            return None, ""
        if resp.status_code != 200:
            return None, f"GitHub {resp.status_code} для {repo_path}: {resp.text[:300]}"
        data = resp.json()
        try:
            return base64.b64decode(data.get("content", "")), ""
        except Exception as e:
            return None, f"Не вдалося декодувати {repo_path}: {e}"

    def list_dir(self, repo_path: str) -> tuple[list[dict], str]:
        """Lists repo_path (a directory) on self.branch via the Contents API.
        Returns ([], "") for a directory that doesn't exist yet (e.g. img/
        before the first image was ever pushed)."""
        if not self.configured:
            return [], "GitHub публікація не налаштована (немає токена/repo)"
        try:
            resp = requests.get(
                self._repo_url(f"/contents/{repo_path}"),
                headers=self._headers(),
                params={"ref": self.branch},
                timeout=30,
            )
        except requests.RequestException as e:
            return [], f"Мережева помилка списку {repo_path}: {e}"
        if resp.status_code == 404:
            return [], ""
        if resp.status_code != 200:
            return [], f"GitHub {resp.status_code} для {repo_path}: {resp.text[:300]}"
        data = resp.json()
        if not isinstance(data, list):
            return [], f"{repo_path} не є директорією"
        return [e for e in data if isinstance(e, dict) and e.get("type") == "file"], ""

    def raw_url(self, repo_path: str, cache_bust: str = "") -> str:
        """Direct raw.githubusercontent.com URL — Flet's ft.Image can load
        this straight from the web, no need to fetch bytes into the app
        just to display a thumbnail.

        raw.githubusercontent.com is served through a CDN that keeps
        serving the old bytes for a path for a while after that path's
        content changes — pass the file's current blob `sha` (from a
        fresh list_dir()/get_file_sha() call) as cache_bust so a replaced
        image's URL actually changes and the CDN is forced to fetch fresh
        content instead of a locally-updated GitHub commit looking, from
        the app's point of view, like the image "didn't change"."""
        url = f"https://raw.githubusercontent.com/{self.owner}/{self.repo}/{self.branch}/{repo_path}"
        if cache_bust:
            url += f"?v={cache_bust}"
        return url

    def put_file(
        self, repo_path: str, content_bytes: bytes, message: str
    ) -> tuple[bool, str]:
        """Creates or updates repo_path on self.branch. Returns (ok, error).

        Retries once if GitHub's Contents API 404s our SHA lookup for a file
        that (per a 422 "sha wasn't supplied" response) actually already
        exists — observed read-after-write lag right after a file's first
        creation, not a hypothetical edge case."""
        if not self.configured:
            return False, "GitHub публікація не налаштована (немає токена/repo)"

        for attempt in range(2):
            try:
                sha = self.get_file_sha(repo_path)
            except requests.RequestException as e:
                return False, f"Не вдалося перевірити файл на GitHub: {e}"

            payload = {
                "message": message,
                "content": base64.b64encode(content_bytes).decode("ascii"),
                "branch": self.branch,
            }
            if sha:
                payload["sha"] = sha

            try:
                resp = requests.put(
                    self._repo_url(f"/contents/{repo_path}"),
                    headers=self._headers(),
                    json=payload,
                    timeout=60,
                )
            except requests.RequestException as e:
                return False, f"Мережева помилка публікації {repo_path}: {e}"

            if resp.status_code in (200, 201):
                return True, ""
            if resp.status_code == 422 and "sha" in resp.text and attempt == 0:
                time.sleep(1.5)
                continue
            return False, f"GitHub {resp.status_code} для {repo_path}: {resp.text[:300]}"

        return False, f"GitHub 422 для {repo_path} (sha lookup lag) — спробуйте ще раз"

    def delete_file(self, repo_path: str, message: str) -> tuple[bool, str]:
        if not self.configured:
            return False, "GitHub публікація не налаштована (немає токена/repo)"
        try:
            sha = self.get_file_sha(repo_path)
        except requests.RequestException as e:
            return False, f"Не вдалося перевірити файл на GitHub: {e}"
        if not sha:
            return True, ""  # already gone

        try:
            resp = requests.delete(
                self._repo_url(f"/contents/{repo_path}"),
                headers=self._headers(),
                json={"message": message, "sha": sha, "branch": self.branch},
                timeout=30,
            )
        except requests.RequestException as e:
            return False, f"Мережева помилка видалення {repo_path}: {e}"

        if resp.status_code == 200:
            return True, ""
        return False, f"GitHub {resp.status_code} для {repo_path}: {resp.text[:300]}"

    # ---------------- Releases API ----------------

    def _find_release_by_tag(self, tag: str) -> dict | None:
        resp = requests.get(
            self._repo_url(f"/releases/tags/{tag}"),
            headers=self._headers(),
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()
        return None

    def create_draft_release(
        self, tag: str, name: str, body: str = ""
    ) -> tuple[dict | None, str]:
        """Creates a draft release, auto-retrying with -1, -2, ... suffixes if
        the tag was burned by a previous immutable (published) release — see
        the documented GitHub "immutable releases" gotcha. Returns (release, error)."""
        base_tag = tag
        for attempt in range(10):
            candidate_tag = base_tag if attempt == 0 else f"{base_tag}-{attempt}"
            try:
                resp = requests.post(
                    self._repo_url("/releases"),
                    headers=self._headers(),
                    json={
                        "tag_name": candidate_tag,
                        "target_commitish": self.branch,
                        "name": name,
                        "body": body,
                        "draft": True,
                        "prerelease": False,
                    },
                    timeout=30,
                )
            except requests.RequestException as e:
                return None, f"Мережева помилка створення релізу: {e}"

            if resp.status_code == 201:
                return resp.json(), ""

            msg = resp.text or ""
            if resp.status_code == 422 and (
                "already_exists" in msg or "immutable" in msg
            ):
                # Tag burned by an earlier immutable release -- GitHub has
                # used at least two different error shapes for this over
                # time ("already_exists" as well as a "custom" code with
                # "tag_name was used by an immutable release" wording, seen
                # even after the old release itself was deleted, since the
                # tag stays locked independently of the release object).
                continue
            return None, f"GitHub {resp.status_code} під час створення релізу: {msg[:300]}"

        return None, f"Не вдалося підібрати вільний тег на основі '{base_tag}'"

    def upload_asset(
        self, release: dict, file_path: str, asset_name: str
    ) -> tuple[bool, str]:
        upload_url = (release.get("upload_url") or "").split("{", 1)[0]
        if not upload_url:
            return False, "У релізу немає upload_url"

        # If an asset with this name already exists on the (draft) release,
        # delete it first — the API rejects duplicate asset names.
        for asset in release.get("assets") or []:
            if asset.get("name") == asset_name:
                requests.delete(
                    self._repo_url(f"/releases/assets/{asset['id']}"),
                    headers=self._headers(),
                    timeout=30,
                )

        headers = self._headers()
        headers["Content-Type"] = "application/octet-stream"
        try:
            # Passing an open file object as `data=` lets requests fall back to
            # chunked transfer encoding when it can't cheaply determine the
            # size — GitHub's asset-upload endpoint rejects chunked uploads
            # with a bare HTML "Bad request" (not a JSON API error) instead of
            # honoring them, which large release assets (exe/apk, 100MB+) hit
            # every time. Reading the bytes upfront gives requests a concrete
            # Content-Length instead.
            with open(file_path, "rb") as f:
                file_bytes = f.read()
            resp = requests.post(
                upload_url,
                headers=headers,
                params={"name": asset_name},
                data=file_bytes,
                timeout=600,
            )
        except (requests.RequestException, OSError) as e:
            return False, f"Помилка завантаження {asset_name}: {e}"

        if resp.status_code == 201:
            return True, ""
        return False, f"GitHub {resp.status_code} для {asset_name}: {resp.text[:300]}"

    def publish_release(self, release_id: int) -> tuple[bool, str]:
        try:
            resp = requests.patch(
                self._repo_url(f"/releases/{release_id}"),
                headers=self._headers(),
                json={"draft": False},
                timeout=30,
            )
        except requests.RequestException as e:
            return False, f"Мережева помилка публікації релізу: {e}"

        if resp.status_code == 200:
            return True, ""
        return False, f"GitHub {resp.status_code} під час публікації релізу: {resp.text[:300]}"

    def publish_build(
        self,
        tag: str,
        name: str,
        assets: list[tuple[str, str]],
        body: str = "",
    ) -> tuple[bool, str]:
        """High-level helper: draft -> upload all assets -> publish.
        assets is a list of (local_file_path, asset_name)."""
        release, err = self.create_draft_release(tag, name, body)
        if not release:
            return False, err

        for file_path, asset_name in assets:
            ok, err = self.upload_asset(release, file_path, asset_name)
            if not ok:
                return False, err

        return self.publish_release(release["id"])
