"""QQ Music client — sync facade over the async `qqmusic_api` library.

`qqmusic_api` is fully async (httpx-based). The orchestrator wants a sync
surface, so every public method here wraps a coroutine with a fresh event
loop via `_run()`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from qqmusic_api import Client, Credential
from qqmusic_api.modules.search import SearchType

from .text_util import to_simplified

BATCH_SIZE = 30


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def load_credential(cred_json: str) -> Credential:
    """Parse a JSON blob of credential fields into a `Credential`."""
    data = json.loads(cred_json)
    return Credential.model_validate(data)


def dump_credential(credential: Credential) -> str:
    """Serialize a `Credential` to a compact JSON string for secret storage."""
    return json.dumps(credential.model_dump(by_alias=True), ensure_ascii=False)


def ensure_fresh(credential: Credential) -> tuple[Credential, bool]:
    """Check + refresh credential. Returns (credential, rotated).

    `rotated` is True when the `musickey` changed so the caller can persist
    the new blob (e.g. push it back to GitHub Secrets).
    """
    original_key = credential.musickey

    async def _do() -> Credential:
        client = Client(credential=credential)
        try:
            expired = await client.login.check_expired(credential)
            if not expired:
                return credential
            return await client.login.refresh_credential(credential)
        finally:
            await client.close()

    fresh = _run(_do())
    rotated = fresh.musickey != original_key
    return fresh, rotated


class QQClient:
    """Sync wrapper around `qqmusic_api.Client` for the sync service."""

    def __init__(self, credential: Credential):
        self.credential = credential

    def _client(self) -> Client:
        return Client(credential=self.credential)

    def list_user_songlists(self) -> list[dict[str, Any]]:
        async def _do() -> list[dict[str, Any]]:
            client = self._client()
            try:
                resp = await client.user.get_created_songlist(
                    self.credential.musicid, credential=self.credential
                )
                return [
                    {"dirid": p.dirid, "dirname": p.title, "song_count": p.songnum}
                    for p in resp.playlists
                ]
            finally:
                await client.close()

        return _run(_do())

    def find_or_create_playlist(self, name: str) -> dict[str, Any]:
        """Return `{dirid, dirname}` for a user playlist matching `name`.

        Matches exactly on title; creates a new playlist if none found.
        """
        existing = self.list_user_songlists()
        for pl in existing:
            if pl["dirname"] == name:
                return {"dirid": pl["dirid"], "dirname": pl["dirname"]}

        async def _do() -> dict[str, Any]:
            client = self._client()
            try:
                resp = await client.songlist.create(
                    dirname=name, credential=self.credential
                )
                return {"dirid": resp.dirid, "dirname": resp.name}
            finally:
                await client.close()

        return _run(_do())

    def get_playlist_songs(self, dirid: int) -> list[dict[str, Any]]:
        """Fetch all songs in a user-owned playlist (paginated)."""

        async def _do() -> list[dict[str, Any]]:
            client = self._client()
            out: list[dict[str, Any]] = []
            try:
                page = 1
                page_size = 100
                while True:
                    resp = await client.songlist.get_detail(
                        songlist_id=0,
                        dirid=dirid,
                        num=page_size,
                        page=page,
                    )
                    for song in resp.songs:
                        out.append(
                            {
                                "id": song.id,
                                "mid": song.mid,
                                "title": to_simplified(song.title or song.name),
                                "artists": [to_simplified(s.name) for s in song.singer],
                                "duration": song.interval,
                                "type": song.type,
                            }
                        )
                    if not resp.hasmore or not resp.songs:
                        break
                    page += 1
                return out
            finally:
                await client.close()

        return _run(_do())

    def search_song(self, keyword: str, limit: int = 10, num: int | None = None) -> list[dict[str, Any]]:
        """Search songs by keyword. Returns normalized candidates."""
        actual_num = num if num is not None else limit

        async def _do() -> list[dict[str, Any]]:
            client = self._client()
            try:
                resp = await client.search.search_by_type(
                    keyword=keyword, search_type=SearchType.SONG, num=actual_num
                )
                return [
                    {
                        "id": song.id,
                        "mid": song.mid,
                        "title": to_simplified(song.title or song.name),
                        "artists": [to_simplified(s.name) for s in song.singer],
                        "album": to_simplified(song.album.name) if song.album else "",
                        "duration": song.interval,
                        "type": song.type,
                    }
                    for song in resp.song
                ]
            finally:
                await client.close()

        return _run(_do())

    def search_singers(self, keyword: str, num: int = 5) -> list[dict[str, Any]]:
        """Search singers by keyword. Returns normalized alias candidates."""

        async def _do() -> list[dict[str, Any]]:
            client = self._client()
            try:
                resp = await client.search.search_by_type(
                    keyword=keyword, search_type=SearchType.SINGER, num=num
                )
                return [
                    {
                        "id": singer.id,
                        "mid": singer.mid,
                        "name": to_simplified(singer.name),
                        "title": to_simplified(singer.title),
                    }
                    for singer in resp.singer
                ]
            finally:
                await client.close()

        return _run(_do())

    def add_songs(self, dirid: int, items: list[tuple[int, int]]) -> bool:
        """Add songs to a playlist in batches of 30, one retry per batch."""
        return _run(self._batch_op(dirid, items, op="add"))

    def del_songs(self, dirid: int, items: list[tuple[int, int]]) -> bool:
        """Remove songs from a playlist in batches of 30, one retry per batch."""
        # QQ's songlist delete endpoint expects songType=0 even when playlist
        # detail/search models expose normal songs as type=1. Passing through
        # the exposed type can return success without removing the row.
        delete_items = [(song_id, 0) for song_id, _song_type in items]
        return _run(self._batch_op(dirid, delete_items, op="del"))

    async def _batch_op(
        self, dirid: int, items: list[tuple[int, int]], *, op: str
    ) -> bool:
        if not items:
            return True
        client = self._client()
        try:
            all_ok = True
            for i in range(0, len(items), BATCH_SIZE):
                batch = items[i : i + BATCH_SIZE]
                method = (
                    client.songlist.add_songs if op == "add" else client.songlist.del_songs
                )
                ok = False
                for attempt in range(2):
                    try:
                        ok = await method(
                            dirid=dirid,
                            song_info=batch,
                            credential=self.credential,
                        )
                        if ok:
                            break
                    except Exception:
                        if attempt == 1:
                            raise
                all_ok = all_ok and ok
            return all_ok
        finally:
            await client.close()
