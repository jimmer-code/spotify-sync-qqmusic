"""Integration tests for the sync orchestrator.

Mocks both clients + credential loading at `src.sync_service` import sites
so no network or subprocess activity occurs. Match rescue now uses a
title-only fallback search (no external alias API).
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import db
from src import sync_service as svc
from src.config import Config


def _cfg(
    tmpdir: str, threshold: float = 0.2, direction: str = "spotify-to-qq"
) -> Config:
    return Config(
        spotify_client_id="cid",
        spotify_client_secret="csec",
        spotify_refresh_token="rtok",
        spotify_playlist_name="测试同步",
        qq_playlist_name="测试同步",
        qq_credential_json='{"musicid": 1, "musickey": "K"}',
        sync_direction=direction,
        gh_pat_secrets_write=None,
        mirror_delete_threshold=threshold,
        db_path=os.path.join(tmpdir, "sync.db"),
        log_path=os.path.join(tmpdir, "sync.log"),
        unmatched_path=os.path.join(tmpdir, "unmatched.txt"),
        musicbrainz_user_agent="test-ua",
    )


def _sp_track(
    tid: str,
    title: str,
    artist: str,
    duration_ms: int = 200000,
    isrc: str | None = None,
    added_at: str | None = None,
) -> dict:
    return {
        "id": tid,
        "title": title,
        "artists": [artist],
        "album": "Album",
        "duration_ms": duration_ms,
        "isrc": isrc,
        "added_at": added_at,
    }


def _qq_cand(
    sid: int,
    title: str,
    artist: str,
    duration_s: int = 200,
    type_: int = 0,
    mid: str = "mid",
) -> dict:
    return {
        "id": sid,
        "mid": mid,
        "title": title,
        "artists": [artist],
        "album": "Album",
        "duration": duration_s,
        "type": type_,
    }


def _make_sp_mock(tracks: list[dict]) -> MagicMock:
    sp = MagicMock()
    sp.find_playlist_by_name.return_value = {"id": "pl1", "name": "测试同步"}
    sp.get_playlist_tracks.return_value = tracks
    return sp


def _make_qq_mock(
    current: list[dict],
    search_map: dict[str, list[dict]],
    dirid: int = 777,
) -> MagicMock:
    qq = MagicMock()
    qq.find_or_create_playlist.return_value = {"dirid": dirid, "dirname": "测试同步"}
    qq.get_playlist_songs.return_value = current
    qq.search_song.side_effect = lambda kw, num=10: search_map.get(kw, [])
    qq.add_songs.return_value = True
    qq.del_songs.return_value = True
    return qq


class TestRunSync(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_persists_rotated_spotify_refresh_token(self):
        sp = MagicMock()
        sp.refresh_token_rotated = True
        sp.refresh_token = "RT_NEW"
        cfg = _cfg(self.tmpdir)
        notes: list[str] = []

        with patch.object(
            svc, "_persist_local_env_var", return_value=(True, "local .env updated")
        ) as local, patch.object(
            svc, "_push_secret_if_possible", return_value=(True, "secret rotated")
        ) as github:
            svc._persist_spotify_refresh_token_if_rotated(sp, cfg, notes)

        local.assert_called_once_with("SPOTIFY_REFRESH_TOKEN", "RT_NEW")
        github.assert_called_once_with("SPOTIFY_REFRESH_TOKEN", "RT_NEW", cfg)
        assert sp.refresh_token_rotated is False
        assert "spotify_refresh_token_rotated=local+github" in notes[0]

    def _patches(
        self,
        sp_mock,
        qq_mock,
        rotated: bool = False,
        aliases: dict[str, list[str]] | None = None,
    ):
        """Stack all the patches the orchestrator needs."""
        credential = MagicMock()
        alias_client = MagicMock()
        alias_map = aliases or {}
        alias_client.get_aliases_for_isrc.side_effect = (
            lambda isrc, artist: alias_map.get(artist, [artist])
        )
        return [
            patch.object(svc, "SpotifyClient", return_value=sp_mock),
            patch.object(svc, "load_credential", return_value=credential),
            patch.object(svc, "ensure_fresh", return_value=(credential, rotated)),
            patch.object(svc, "dump_credential", return_value='{"new":"blob"}'),
            patch.object(svc, "QQClient", return_value=qq_mock),
            patch.object(svc, "MusicBrainzClient", return_value=alias_client),
        ]

    def test_split_artist_aliases_from_qq_singer_name(self):
        self.assertEqual(
            svc._split_artist_aliases("方大同 (Khalil Fong)"),
            ["方大同 (Khalil Fong)", "Khalil Fong", "方大同"],
        )

    def test_adds_in_added_at_ascending_order(self):
        """Spotify returns out-of-order; sync must add to QQ in asc order
        so QQ's added-time-desc view matches Spotify's."""
        sp_tracks = [
            _sp_track("sp_c", "C", "X", added_at="2024-03-01T00:00:00Z"),
            _sp_track("sp_a", "A", "X", added_at="2024-01-01T00:00:00Z"),
            _sp_track("sp_b", "B", "X", added_at="2024-02-01T00:00:00Z"),
        ]
        search_map = {
            "C X": [_qq_cand(303, "C", "X", 200, 0)],
            "A X": [_qq_cand(101, "A", "X", 200, 0)],
            "B X": [_qq_cand(202, "B", "X", 200, 0)],
        }
        sp = _make_sp_mock(sp_tracks)
        qq = _make_qq_mock([], search_map)

        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(_cfg(self.tmpdir), dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        qq.add_songs.assert_called_once()
        _, pairs = qq.add_songs.call_args.args
        self.assertEqual(pairs, [(101, 0), (202, 0), (303, 0)])

    def test_incremental_adds_new_song_after_existing_without_rebuild(self):
        sp_tracks = [
            _sp_track("old", "Old", "X", added_at="2024-01-01T00:00:00Z"),
            _sp_track("new", "New", "X", added_at="2024-02-01T00:00:00Z"),
        ]
        search_map = {
            "Old X": [_qq_cand(101, "Old", "X", 200, 0)],
            "New X": [_qq_cand(202, "New", "X", 200, 0)],
        }
        sp = _make_sp_mock([sp_tracks[0]])
        qq_first = _make_qq_mock([], search_map)

        cfg = _cfg(self.tmpdir)
        patches = self._patches(sp, qq_first)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)

        sp.get_playlist_tracks.return_value = sp_tracks
        qq_second = _make_qq_mock([_qq_cand(101, "Old", "X", 200, 0)], search_map)
        patches = self._patches(sp, qq_second)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        qq_second.del_songs.assert_not_called()
        qq_second.add_songs.assert_called_once()
        _, add_pairs = qq_second.add_songs.call_args.args
        self.assertEqual(add_pairs, [(202, 0)])

    def test_bidirectional_qq_addition_is_added_to_spotify(self):
        old_sp = _sp_track("old", "Old", "X", added_at="2024-01-01T00:00:00Z")
        new_sp = _sp_track("new", "New", "Y", duration_ms=200000)
        old_qq = _qq_cand(101, "Old", "X", 200, 0)
        new_qq = _qq_cand(202, "New", "Y", 200, 0)
        search_map = {"Old X": [old_qq]}
        cfg = _cfg(self.tmpdir, direction="bidirectional")

        sp = _make_sp_mock([old_sp])
        qq_first = _make_qq_mock([], search_map)
        patches = self._patches(sp, qq_first)
        for p in patches:
            p.start()
        try:
            self.assertEqual(svc.run_sync(cfg), 0)
        finally:
            for p in patches:
                p.stop()

        sp.get_playlist_tracks.return_value = [old_sp]
        sp.search_track.return_value = [new_sp]
        qq_second = _make_qq_mock([old_qq, new_qq], search_map)
        patches = self._patches(sp, qq_second)
        for p in patches:
            p.start()
        try:
            self.assertEqual(svc.run_sync(cfg), 0)
        finally:
            for p in patches:
                p.stop()

        sp.add_tracks.assert_called_once_with("pl1", ["new"])
        sp.remove_tracks.assert_not_called()
        qq_second.add_songs.assert_not_called()
        qq_second.del_songs.assert_not_called()

    def test_bidirectional_qq_deletion_is_removed_from_spotify(self):
        old_sp = _sp_track("old", "Old", "X", added_at="2024-01-01T00:00:00Z")
        old_qq = _qq_cand(101, "Old", "X", 200, 0)
        search_map = {"Old X": [old_qq]}
        cfg = _cfg(self.tmpdir, threshold=1.0, direction="bidirectional")

        sp = _make_sp_mock([old_sp])
        qq_first = _make_qq_mock([], search_map)
        patches = self._patches(sp, qq_first)
        for p in patches:
            p.start()
        try:
            self.assertEqual(svc.run_sync(cfg), 0)
        finally:
            for p in patches:
                p.stop()

        sp.get_playlist_tracks.return_value = [old_sp]
        qq_second = _make_qq_mock([], search_map)
        patches = self._patches(sp, qq_second)
        for p in patches:
            p.start()
        try:
            self.assertEqual(svc.run_sync(cfg), 0)
        finally:
            for p in patches:
                p.stop()

        sp.remove_tracks.assert_called_once_with("pl1", ["old"])
        sp.add_tracks.assert_not_called()
        qq_second.add_songs.assert_not_called()
        qq_second.del_songs.assert_not_called()

    def test_order_drift_rebuilds_playlist(self):
        sp_tracks = [
            _sp_track("sp_a", "A", "X", added_at="2024-01-01T00:00:00Z"),
            _sp_track("sp_b", "B", "X", added_at="2024-02-01T00:00:00Z"),
            _sp_track("sp_c", "C", "X", added_at="2024-03-01T00:00:00Z"),
        ]
        search_map = {
            "A X": [_qq_cand(101, "A", "X", 200, 0)],
            "B X": [_qq_cand(202, "B", "X", 200, 0)],
            "C X": [_qq_cand(303, "C", "X", 200, 0)],
        }
        sp = _make_sp_mock(sp_tracks)
        qq_first = _make_qq_mock([], search_map)

        cfg = _cfg(self.tmpdir, threshold=0.5)
        patches = self._patches(sp, qq_first)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)

        # Current QQ order is neither insertion-asc [101, 202, 303] nor
        # display-desc [303, 202, 101], so sync must rebuild.
        qq_second = _make_qq_mock(
            [
                _qq_cand(202, "B", "X", 200, 0),
                _qq_cand(101, "A", "X", 200, 0),
                _qq_cand(303, "C", "X", 200, 0),
            ],
            search_map,
        )
        patches = self._patches(sp, qq_second)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        qq_second.del_songs.assert_called_once()
        _, del_pairs = qq_second.del_songs.call_args.args
        self.assertEqual(del_pairs, [(202, 0), (101, 0), (303, 0)])
        qq_second.add_songs.assert_called_once()
        _, add_pairs = qq_second.add_songs.call_args.args
        self.assertEqual(add_pairs, [(101, 0), (202, 0), (303, 0)])

    def test_mirror_adds_new_tracks(self):
        sp_tracks = [
            _sp_track("sp1", "Song One", "Alice"),
            _sp_track("sp2", "Song Two", "Bob"),
        ]
        qq_current: list[dict] = []
        search_map = {
            "Song One Alice": [_qq_cand(101, "Song One", "Alice", 200, 0)],
            "Song Two Bob": [_qq_cand(102, "Song Two", "Bob", 200, 0)],
        }
        sp = _make_sp_mock(sp_tracks)
        qq = _make_qq_mock(qq_current, search_map)

        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(_cfg(self.tmpdir), dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        qq.add_songs.assert_called_once()
        dirid_arg, pairs_arg = qq.add_songs.call_args.args
        self.assertEqual(dirid_arg, 777)
        self.assertEqual(sorted(pairs_arg), [(101, 0), (102, 0)])
        qq.del_songs.assert_not_called()

    def test_mirror_removes_stale_tracks(self):
        # Spotify has 1 track; QQ has 5 → remove 4 stale (4/5 = 80% > default 20%)
        # so raise threshold so the delete is allowed.
        sp_tracks = [_sp_track("sp1", "Keeper", "Alice")]
        qq_current = [
            _qq_cand(101, "Keeper", "Alice", 200, 0),
            _qq_cand(201, "Stale A", "X", 200, 0),
            _qq_cand(202, "Stale B", "Y", 200, 0),
            _qq_cand(203, "Stale C", "Z", 200, 0),
            _qq_cand(204, "Stale D", "W", 200, 0),
        ]
        search_map = {"Keeper Alice": [_qq_cand(101, "Keeper", "Alice", 200, 0)]}
        sp = _make_sp_mock(sp_tracks)
        qq = _make_qq_mock(qq_current, search_map)

        cfg = _cfg(self.tmpdir, threshold=0.9)
        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        qq.add_songs.assert_not_called()
        qq.del_songs.assert_called_once()
        _, pairs_arg = qq.del_songs.call_args.args
        self.assertEqual({p[0] for p in pairs_arg}, {201, 202, 203, 204})

    def test_safety_threshold_aborts_mass_delete(self):
        sp_tracks = [_sp_track("sp1", "Keeper", "Alice")]
        qq_current = [
            _qq_cand(101, "Keeper", "Alice", 200, 0),
            _qq_cand(201, "Stale A", "X", 200, 0),
            _qq_cand(202, "Stale B", "Y", 200, 0),
            _qq_cand(203, "Stale C", "Z", 200, 0),
            _qq_cand(204, "Stale D", "W", 200, 0),
        ]
        search_map = {"Keeper Alice": [_qq_cand(101, "Keeper", "Alice", 200, 0)]}
        sp = _make_sp_mock(sp_tracks)
        qq = _make_qq_mock(qq_current, search_map)

        cfg = _cfg(self.tmpdir, threshold=0.2)  # 4/5 = 80% > 20% → abort.
        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 2)
        qq.add_songs.assert_not_called()
        qq.del_songs.assert_not_called()

    def test_dry_run_does_not_mutate(self):
        sp_tracks = [_sp_track("sp1", "Song One", "Alice")]
        qq_current: list[dict] = []
        search_map = {"Song One Alice": [_qq_cand(101, "Song One", "Alice", 200, 0)]}
        sp = _make_sp_mock(sp_tracks)
        qq = _make_qq_mock(qq_current, search_map)

        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(_cfg(self.tmpdir), dry_run=True)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        qq.add_songs.assert_not_called()
        qq.del_songs.assert_not_called()

    def test_unmatched_track_lands_in_unmatched_txt(self):
        sp_tracks = [_sp_track("sp1", "Unfindable Track", "Nobody")]
        search_map = {"Unfindable Track Nobody": []}  # zero candidates
        sp = _make_sp_mock(sp_tracks)
        qq = _make_qq_mock([], search_map)

        cfg = _cfg(self.tmpdir)
        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        # No matches → no add/delete, run succeeds.
        self.assertEqual(rc, 0)
        qq.add_songs.assert_not_called()

        with open(cfg.unmatched_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Unfindable Track", content)
        self.assertIn("Nobody", content)

    def test_incremental_snapshot_skips_reused_tracks(self):
        """Second run should diff against snapshot: only new tracks get searched."""
        sp_tracks = [_sp_track("sp1", "Song One", "Alice")]
        search_map = {"Song One Alice": [_qq_cand(101, "Song One", "Alice", 200, 0)]}
        sp = _make_sp_mock(sp_tracks)
        qq_first = _make_qq_mock([], search_map)

        cfg = _cfg(self.tmpdir)
        patches = self._patches(sp, qq_first)
        for p in patches:
            p.start()
        try:
            svc.run_sync(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(qq_first.search_song.call_count, 1)

        # Second run: QQ playlist now contains the matched song. Incremental
        # snapshot → no new tracks to search.
        qq_second = _make_qq_mock(
            [_qq_cand(101, "Song One", "Alice", 200, 0)],
            search_map,
        )
        patches = self._patches(sp, qq_second)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        # Snapshot hit → search_song should NOT have been called.
        qq_second.search_song.assert_not_called()
        qq_second.add_songs.assert_not_called()
        qq_second.del_songs.assert_not_called()

    def test_incremental_without_snapshot_uses_cached_and_unmatched_progress(self):
        sp_tracks = [
            _sp_track("sp1", "Song One", "Alice"),
            _sp_track("sp2", "Unfindable Track", "Nobody"),
        ]
        search_map = {
            "Song One Alice": [_qq_cand(101, "Song One", "Alice", 200, 0)],
            "Unfindable Track Nobody": [],
            "Unfindable Track": [],
        }
        sp = _make_sp_mock(sp_tracks)
        qq_first = _make_qq_mock([], search_map)

        cfg = _cfg(self.tmpdir)
        patches = self._patches(sp, qq_first)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        self.assertGreaterEqual(qq_first.search_song.call_count, 2)

        conn = db.connect(cfg.db_path)
        try:
            db.snapshot_clear(conn, "pl1")
        finally:
            conn.close()

        qq_second = _make_qq_mock(
            [_qq_cand(101, "Song One", "Alice", 200, 0)],
            search_map,
        )
        patches = self._patches(sp, qq_second)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        qq_second.search_song.assert_not_called()
        qq_second.add_songs.assert_not_called()
        qq_second.del_songs.assert_not_called()

    def test_full_flag_bypasses_snapshot(self):
        """--full forces a re-search even when snapshot matches."""
        sp_tracks = [_sp_track("sp1", "Song One", "Alice")]
        search_map = {"Song One Alice": [_qq_cand(101, "Song One", "Alice", 200, 0)]}
        sp = _make_sp_mock(sp_tracks)
        qq_first = _make_qq_mock([], search_map)

        cfg = _cfg(self.tmpdir)
        patches = self._patches(sp, qq_first)
        for p in patches:
            p.start()
        try:
            svc.run_sync(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        # Second run with full=True — snapshot exists but should be ignored.
        qq_second = _make_qq_mock(
            [_qq_cand(101, "Song One", "Alice", 200, 0)],
            search_map,
        )
        patches = self._patches(sp, qq_second)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(cfg, dry_run=False, full=True)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        # Full mode → every track gets re-searched.
        qq_second.search_song.assert_called_with("Song One Alice", num=10)
        self.assertEqual(qq_second.search_song.call_count, 1)

    def test_title_only_retry_rescues_weak_primary(self):
        """When title+artist scores <0.8, retry with title only should salvage."""
        # Spotify: romanized name "Jay Chou"; QQ catalog indexes Chinese artist.
        sp_tracks = [_sp_track("sp1", "晴天", "Jay Chou", duration_ms=269000)]
        # Primary `title+artist` search returns only a wrong-artist candidate.
        primary_cand = _qq_cand(99, "晴天", "Unknown", duration_s=100, type_=0)
        # Title-only search surfaces the canonical Chinese-artist version.
        title_only_cand = _qq_cand(901, "晴天", "周杰伦", duration_s=269, type_=0)
        search_map = {
            "晴天 Jay Chou": [primary_cand],
            "晴天": [title_only_cand],
        }
        sp = _make_sp_mock(sp_tracks)
        qq = _make_qq_mock([], search_map)

        patches = self._patches(sp, qq, aliases={"Jay Chou": ["Jay Chou", "周杰伦"]})
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(_cfg(self.tmpdir), dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        qq.add_songs.assert_called_once()
        _, pairs_arg = qq.add_songs.call_args.args
        self.assertEqual(pairs_arg, [(901, 0)])
        called_queries = {c.args[0] for c in qq.search_song.call_args_list}
        self.assertIn("晴天 Jay Chou", called_queries)
        self.assertIn("晴天", called_queries)

    def test_qq_singer_alias_rescues_romanized_artist(self):
        sp_tracks = [_sp_track("sp1", "暖", "Khalil Fong", duration_ms=200000)]
        cand = _qq_cand(301, "暖", "方大同", duration_s=200, type_=0)
        search_map = {
            "暖 Khalil Fong": [cand],
            "暖": [cand],
        }
        sp = _make_sp_mock(sp_tracks)
        qq = _make_qq_mock([], search_map)
        qq.search_singers.return_value = [
            {
                "id": 1,
                "mid": "s1",
                "name": "方大同 (Khalil Fong)",
                "title": "方大同 (Khalil Fong)",
            }
        ]

        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(_cfg(self.tmpdir), dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        qq.add_songs.assert_called_once()
        _, pairs_arg = qq.add_songs.call_args.args
        self.assertEqual(pairs_arg, [(301, 0)])
        qq.search_singers.assert_called_with("Khalil Fong", num=5)

    def test_title_fallback_tries_base_title_before_dash(self):
        sp_tracks = [
            _sp_track(
                "sp1",
                "Morning Selection - Instrumental; 2019 Remaster",
                "HONEY&B-BOYS",
                duration_ms=200000,
            )
        ]
        cand = _qq_cand(401, "Morning Selection", "HONEY&B-BOYS", duration_s=200)
        search_map = {
            "Morning Selection - Instrumental; 2019 Remaster HONEY&B-BOYS": [],
            "Morning Selection - Instrumental; 2019 Remaster": [],
            "morning selection - instrumental; 2019 remaster": [],
            "Morning Selection": [cand],
        }
        sp = _make_sp_mock(sp_tracks)
        qq = _make_qq_mock([], search_map)

        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(_cfg(self.tmpdir), dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        qq.add_songs.assert_called_once()
        _, pairs_arg = qq.add_songs.call_args.args
        self.assertEqual(pairs_arg, [(401, 0)])
        self.assertIn(
            "Morning Selection",
            [c.args[0] for c in qq.search_song.call_args_list],
        )

    def test_title_duration_wrong_artist_is_rejected(self):
        """Same title + same duration is not enough when artist differs."""
        sp_tracks = [_sp_track("sp1", "Song One", "Alice")]
        search_map = {
            "Song One Alice": [_qq_cand(909, "Song One", "Mallory", 200, 0)],
            "Song One": [_qq_cand(909, "Song One", "Mallory", 200, 0)],
        }
        sp = _make_sp_mock(sp_tracks)
        qq = _make_qq_mock([], search_map)

        cfg = _cfg(self.tmpdir)
        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        qq.add_songs.assert_not_called()
        with open(cfg.unmatched_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Song One", content)
        self.assertIn("Alice", content)

    def test_title_only_retry_skipped_when_primary_already_strong(self):
        """If primary search already scores ≥0.8, no title-only retry fires."""
        sp_tracks = [_sp_track("sp1", "Song One", "Alice")]
        search_map = {
            "Song One Alice": [_qq_cand(101, "Song One", "Alice", 200, 0)],
        }
        sp = _make_sp_mock(sp_tracks)
        qq = _make_qq_mock([], search_map)

        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(_cfg(self.tmpdir), dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        self.assertEqual(qq.search_song.call_count, 1)


if __name__ == "__main__":
    unittest.main()
