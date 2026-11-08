"""Spotify Web API client (sync, requests-based)."""

from __future__ import annotations

import time
from typing import Any

import requests

from .text_util import to_simplified

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"
PLAYLIST_ITEM_FIELDS = (
    "items(added_at,item(id,name,duration_ms,artists(name),"
    "album(name),external_ids(isrc))),next"
)
MAX_RETRIES = 3


class SpotifyClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.refresh_token_rotated = False
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    def _refresh_access_token(self) -> str:
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            auth=(self.client_id, self.client_secret),
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        self._access_token = body["access_token"]
        new_refresh_token = body.get("refresh_token")
        if new_refresh_token and new_refresh_token != self.refresh_token:
            self.refresh_token = str(new_refresh_token)
            self.refresh_token_rotated = True
        self._token_expires_at = time.time() + int(body.get("expires_in", 3600)) - 60
        return self._access_token

    def _auth_header(self) -> dict[str, str]:
        if not self._access_token or time.time() >= self._token_expires_at:
            self._refresh_access_token()
        return {"Authorization": f"Bearer {self._access_token}"}

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(MAX_RETRIES + 1):
            resp = requests.get(
                url, headers=self._auth_header(), params=params, timeout=30
            )
            if resp.status_code == 429:
                if attempt >= MAX_RETRIES:
                    resp.raise_for_status()
                retry_after = int(resp.headers.get("Retry-After", "1"))
                backoff = retry_after * (2 ** attempt)
                time.sleep(backoff)
                continue
            if resp.status_code == 401 and attempt == 0:
                self._refresh_access_token()
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("exhausted retries")

    def _request_json(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> dict[str, Any]:
        for attempt in range(MAX_RETRIES + 1):
            headers = self._auth_header()
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            resp = requests.request(
                method, url, headers=headers, params=params, json=json_body, timeout=30
            )
            if resp.status_code == 429:
                if attempt >= MAX_RETRIES:
                    resp.raise_for_status()
                retry_after = int(resp.headers.get("Retry-After", "1"))
                backoff = retry_after * (2 ** attempt)
                time.sleep(backoff)
                continue
            if resp.status_code == 401 and attempt == 0:
                self._refresh_access_token()
                continue
            resp.raise_for_status()
            if not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {}
        raise RuntimeError("exhausted retries")

    def list_playlists(self) -> list[dict[str, Any]]:
        """Return the user's Spotify playlists (paginated through `next`)."""
        out: list[dict[str, Any]] = []
        url: str | None = f"{API_BASE}/me/playlists"
        params: dict[str, Any] | None = {"limit": 50}
        while url:
            body = self._get(url, params=params)
            out.extend(body.get("items", []))
            url = body.get("next")
            params = None
        return out

    def find_playlist_by_name(self, name: str) -> dict[str, Any] | None:
        for pl in self.list_playlists():
            if pl.get("name") == name:
                return pl
        return None

    def get_current_user_id(self) -> str:
        body = self._get(f"{API_BASE}/me")
        uid = body.get("id")
        if not uid:
            raise RuntimeError("no user id in /me response")
        return uid

    def search_track(
        self, title: str, artist: str = "", limit: int = 5
    ) -> list[dict[str, Any]]:
        q_parts = [f'track:"{title}"'] if title else []
        if artist:
            q_parts.append(f'artist:"{artist}"')
        q = " ".join(q_parts) or title
        body = self._get(
            f"{API_BASE}/search",
            params={"q": q, "type": "track", "limit": limit},
        )
        items = ((body.get("tracks") or {}).get("items")) or []
        return [_normalize_track(t) for t in items]

    def create_playlist(
        self, user_id: str, name: str, public: bool = True, description: str = ""
    ) -> dict[str, Any]:
        # `user_id` kept for backwards compat; the Feb 2026 endpoint is /me/playlists.
        # The legacy /users/{user_id}/playlists path returns 403 for dev-mode apps.
        del user_id
        return self._request_json(
            "POST",
            f"{API_BASE}/me/playlists",
            json_body={"name": name, "public": public, "description": description},
        )

    def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        """Add tracks in batches of 100 (Spotify limit).

        Feb 2026: endpoint moved from /tracks to /items.
        """
        for i in range(0, len(track_ids), 100):
            batch = track_ids[i : i + 100]
            uris = [f"spotify:track:{tid}" for tid in batch]
            self._request_json(
                "POST",
                f"{API_BASE}/playlists/{playlist_id}/items",
                json_body={"uris": uris},
            )

    def remove_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        """Remove tracks in batches of 100 (Spotify limit)."""
        for i in range(0, len(track_ids), 100):
            batch = track_ids[i : i + 100]
            items = [{"uri": f"spotify:track:{tid}"} for tid in batch]
            self._request_json(
                "DELETE",
                f"{API_BASE}/playlists/{playlist_id}/items",
                json_body={"items": items},
            )

    def get_playlist_meta(self, playlist_id: str) -> dict[str, Any]:
        """Fetch lightweight playlist metadata (e.g. snapshot_id, name, total tracks)."""
        return self._get(
            f"{API_BASE}/playlists/{playlist_id}",
            params={"fields": "id,name,snapshot_id,tracks.total"},
        )

    def get_playlist_tracks(self, playlist_id: str) -> list[dict[str, Any]]:
        url: str | None = f"{API_BASE}/playlists/{playlist_id}/items"
        params: dict[str, Any] | None = {
            "fields": PLAYLIST_ITEM_FIELDS,
            "limit": 100,
        }
        out: list[dict[str, Any]] = []
        while url:
            body = self._get(url, params=params)
            for item in body.get("items", []):
                track = item.get("item") or item.get("track")
                if track is None:
                    continue
                out.append(_normalize_track(track, added_at=item.get("added_at")))
            url = body.get("next")
            params = None
        return out


def _normalize_track(
    track: dict[str, Any], *, added_at: str | None = None
) -> dict[str, Any]:
    # Traditional → Simplified so search queries match QQ's mainland catalog.
    artists = [
        to_simplified(a.get("name", "")) for a in track.get("artists", []) if a
    ]
    album = to_simplified((track.get("album") or {}).get("name", ""))
    title = to_simplified(track.get("name", ""))
    isrc = (track.get("external_ids") or {}).get("isrc")
    return {
        "id": track.get("id"),
        "title": title,
        "artists": artists,
        "album": album,
        "duration_ms": track.get("duration_ms", 0),
        "isrc": isrc,
        "added_at": added_at,
    }
