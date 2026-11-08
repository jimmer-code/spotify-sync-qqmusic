"""Orchestrator for Spotify/QQ Music playlist reconciliation.

Wires together the clients, matcher, diff engine, DB, and report writers
to execute the 9-step flow from the plan. Returns an exit code suitable
for `sys.exit`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from typing import Any


def _log(msg: str) -> None:
    """Flush-safe progress line to stderr (doesn't pollute stdout summary)."""
    print(msg, file=sys.stderr, flush=True)

from . import db as dbm
from . import incremental
from . import report
from .config import Config
from .diff_engine import compute_mirror_diff, safety_check
from .matcher import (
    is_confident_match,
    normalize_artist,
    normalize_title,
    pick_best,
    score_candidate,
)
from .musicbrainz_client import MusicBrainzClient
from .text_util import explain_method
from .qqmusic_client import (
    QQClient,
    dump_credential,
    ensure_fresh,
    load_credential,
)
from .spotify_client import SpotifyClient


def _added_at_sort_key(track: dict[str, Any]) -> tuple[int, str, str]:
    """Sort tracks by Spotify `added_at` ascending; missing values go first.

    Stable on Spotify track id so consecutive runs see identical order. When
    `add_songs` runs in this order, the most-recently-added Spotify track is
    the freshest QQ entry — putting it at the top of QQ's "added time desc"
    view, matching the user's Spotify view.
    """
    added_at = track.get("added_at") or ""
    has_added = 1 if added_at else 0
    return (has_added, added_at, str(track.get("id") or ""))


def _primary_artist(track: dict[str, Any]) -> str:
    artists = track.get("artists") or []
    if not artists:
        return ""
    first = artists[0]
    if isinstance(first, dict):
        return first.get("name", "") or ""
    return str(first)


def _artist_strings(track: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for artist in track.get("artists") or []:
        if isinstance(artist, dict):
            name = artist.get("name", "")
        else:
            name = str(artist)
        name = name.strip()
        if name:
            out.append(name)
    return out


def _search_query(track: dict[str, Any]) -> str:
    title = track.get("title") or track.get("name") or ""
    artist = _primary_artist(track)
    return f"{title} {artist}".strip()


def _persist_local_env_var(key: str, value: str) -> tuple[bool, str]:
    """Persist one rotated credential in the project `.env`, best-effort."""
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
    )
    if not os.path.exists(env_path):
        return False, ".env not found"
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        prefix = f"{key}="
        replaced = False
        out: list[str] = []
        for line in lines:
            if line.startswith(prefix):
                out.append(f"{prefix}{value}\n")
                replaced = True
            else:
                out.append(line)
        if not replaced:
            out.append(f"{prefix}{value}\n")
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(out)
    except OSError as exc:
        return False, f"local .env write failed: {exc}"
    return True, "local .env updated"


def _push_secret_if_possible(
    secret_name: str, new_blob: str, cfg: Config
) -> tuple[bool, str]:
    """Push a rotated credential to a GH secret. Never raise."""
    if not cfg.gh_pat_secrets_write:
        return False, "no PAT configured"
    if shutil.which("gh") is None:
        return False, "gh CLI not available"
    env = os.environ.copy()
    env["GH_TOKEN"] = cfg.gh_pat_secrets_write
    try:
        proc = subprocess.run(
            ["gh", "secret", "set", secret_name, "--body", new_blob],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"gh invocation failed: {exc}"
    if proc.returncode != 0:
        return False, f"gh secret set rc={proc.returncode}: {proc.stderr.strip()}"
    return True, "secret rotated"


def _push_qq_secret_if_possible(new_blob: str, cfg: Config) -> tuple[bool, str]:
    """Backward-compatible wrapper for the QQ credential secret."""
    return _push_secret_if_possible("QQ_CREDENTIAL_JSON", new_blob, cfg)


def _persist_spotify_refresh_token_if_rotated(
    sp: SpotifyClient, cfg: Config, notes_parts: list[str]
) -> None:
    """Persist a refresh-token rotation without making sync fail."""
    if getattr(sp, "refresh_token_rotated", False) is not True:
        return
    local_ok, local_msg = _persist_local_env_var(
        "SPOTIFY_REFRESH_TOKEN", sp.refresh_token
    )
    gh_ok, gh_msg = _push_secret_if_possible(
        "SPOTIFY_REFRESH_TOKEN", sp.refresh_token, cfg
    )
    destinations: list[str] = []
    if local_ok:
        destinations.append("local")
    if gh_ok:
        destinations.append("github")
    if not destinations:
        destinations.append("none")
    notes_parts.append(
        f"spotify_refresh_token_rotated={'+'.join(destinations)} "
        f"(local: {local_msg}; github: {gh_msg})"
    )
    _log(
        "      -> Spotify refresh token rotated; "
        f"persisted to {', '.join(destinations)}"
    )
    if destinations:
        sp.refresh_token_rotated = False


def _cache_row_for(
    sp_track: dict[str, Any],
    qq: dict[str, Any],
    score: float,
    method: str,
) -> dict[str, Any]:
    return {
        "spotify_track_id": sp_track.get("id"),
        "spotify_title": sp_track.get("title"),
        "spotify_artist": _primary_artist(sp_track),
        "spotify_isrc": sp_track.get("isrc"),
        "qq_song_id": qq.get("id"),
        "qq_song_mid": qq.get("mid"),
        "qq_song_type": qq.get("type"),
        "qq_title": qq.get("title"),
        "qq_artist": (qq.get("artists") or [""])[0],
        "match_score": score,
        "match_method": method,
    }


def _qq_pair_from_cache(row: Any) -> tuple[int, int] | None:
    if row is None:
        return None
    song_id = row["qq_song_id"]
    song_type = row["qq_song_type"]
    if song_id is None or song_type is None:
        return None
    return (int(song_id), int(song_type))


def _qq_pair_from_song(song: dict[str, Any]) -> tuple[int, int] | None:
    song_id = song.get("id")
    song_type = song.get("type")
    if song_id is None or song_type is None:
        return None
    return (int(song_id), int(song_type))


def _ordered_unique_pairs(
    sp_tracks: list[dict[str, Any]],
    matched: list[tuple[dict[str, Any], tuple[int, int]]],
) -> list[tuple[int, int]]:
    by_sp_id: dict[str, tuple[int, int]] = {}
    for track, pair in matched:
        sp_id = track.get("id")
        if sp_id is None:
            continue
        by_sp_id[str(sp_id)] = pair

    ordered: list[tuple[int, int]] = []
    seen_qq_ids: set[int] = set()
    for track in sp_tracks:
        sp_id = track.get("id")
        if sp_id is None:
            continue
        pair = by_sp_id.get(str(sp_id))
        if pair is None or pair[0] in seen_qq_ids:
            continue
        ordered.append(pair)
        seen_qq_ids.add(pair[0])
    return ordered


def _qq_current_ids(qq_current: list[dict[str, Any]]) -> list[int]:
    return [int(s["id"]) for s in qq_current if s.get("id") is not None]


def _incremental_would_match_order(
    current_ids: list[int],
    target_ids_asc: list[int],
    add_ids_asc: list[int],
    remove_ids: set[int],
) -> tuple[bool, str]:
    """Return whether delta writes can produce the target QQ order.

    QQ APIs are not explicit about whether playlist detail returns insertion
    order or display order, so accept either orientation after applying the
    planned delta:

    - insertion order: old kept songs followed by newly added songs
    - display order: newly added songs first, then old kept songs
    """
    current_after_remove = [qid for qid in current_ids if qid not in remove_ids]
    target_ids_desc = list(reversed(target_ids_asc))

    final_if_insertion_order = current_after_remove + add_ids_asc
    if final_if_insertion_order == target_ids_asc:
        return True, "order ok: insertion-asc"

    final_if_display_order = list(reversed(add_ids_asc)) + current_after_remove
    if final_if_display_order == target_ids_desc:
        return True, "order ok: display-desc"

    return False, "order drift: rebuild required"


def _with_artist_aliases(
    track: dict[str, Any],
    qq: QQClient,
    alias_client: MusicBrainzClient,
) -> dict[str, Any]:
    names = _artist_strings(track)
    if not names:
        return track

    expanded: list[str] = []
    seen: set[str] = set()
    isrc = track.get("isrc") or ""
    for name in names:
        for alias in _qq_artist_aliases(name, qq):
            key = normalize_artist(alias)
            if not key or key in seen:
                continue
            expanded.append(alias)
            seen.add(key)
        try:
            aliases = alias_client.get_aliases_for_isrc(str(isrc), name)
        except Exception as exc:  # pragma: no cover — alias lookup is best-effort
            _log(f"    ↻ 歌手别名查询失败 {name!r}: {exc}")
            aliases = [name]
        for alias in aliases:
            key = normalize_artist(alias)
            if not key or key in seen:
                continue
            expanded.append(alias)
            seen.add(key)

    if len(expanded) <= len(names):
        return track
    out = dict(track)
    out["artists"] = expanded
    return out


def _qq_artist_aliases(name: str, qq: QQClient) -> list[str]:
    try:
        singers = qq.search_singers(name, num=5)
    except Exception as exc:  # pragma: no cover — QQ singer search is best-effort
        _log(f"    ↻ QQ 歌手别名搜索失败 {name!r}: {exc}")
        return []

    aliases: list[str] = []
    for singer in singers:
        for field in ("name", "title"):
            aliases.extend(_split_artist_aliases(str(singer.get(field) or "")))
    return aliases


def _split_artist_aliases(value: str) -> list[str]:
    value = value.strip()
    if not value:
        return []

    out = [value]
    for inner in re.findall(r"[\(（]([^()（）]+)[\)）]", value):
        out.append(inner.strip())
    stripped = re.sub(r"\s*[\(（][^()（）]+[\)）]\s*", " ", value).strip()
    if stripped and stripped != value:
        out.append(stripped)
    return out


def _fallback_title_queries(track: dict[str, Any]) -> list[str]:
    title = (track.get("title") or track.get("name") or "").strip()
    queries: list[str] = []

    def add(value: str) -> None:
        value = value.strip()
        if value and value not in queries:
            queries.append(value)

    add(title)
    normalized = normalize_title(title)
    add(normalized)
    for sep in (" - ", " – ", " — "):
        if sep in title:
            add(title.split(sep, 1)[0])
    return queries


def _track_with_title(track: dict[str, Any], title: str) -> dict[str, Any]:
    if (track.get("title") or track.get("name") or "") == title:
        return track
    out = dict(track)
    out["title"] = title
    return out


def _best_scored_candidate(
    track: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, float, str]:
    best_cand: dict[str, Any] | None = None
    best_score = 0.0
    best_method = "none"
    for cand in candidates or []:
        score, method = score_candidate(track, cand)
        if best_cand is None or score > best_score:
            best_cand = cand
            best_score = score
            best_method = method
    return best_cand, best_score, best_method


def _match_title_only_fallback(
    track: dict[str, Any],
    primary_candidates: list[dict[str, Any]],
    qq: QQClient,
    alias_client: MusicBrainzClient,
) -> tuple[dict[str, Any] | None, float, str]:
    """Try `title+artist` first; on weak match retry by title, but still require artist."""
    best, best_score, best_method = pick_best(track, primary_candidates, threshold=0.8)
    if best is not None and best_score >= 0.8:
        return best, best_score, best_method

    overall_cand, overall_score, overall_method = _best_scored_candidate(
        track, primary_candidates
    )
    if overall_score < best_score:
        overall_score = best_score
        overall_method = best_method

    queries = _fallback_title_queries(track)
    if not queries:
        return None, overall_score, overall_method

    _log(f"    ↻ 主搜索弱（最高 {int(overall_score * 100)}%），回退仅搜标题")
    alt_scored: list[tuple[dict[str, Any], dict[str, Any]]] = []
    best_score_track = track
    for query in queries:
        _log(f"    ↻ 搜索词: {query!r}")
        try:
            alt_cands = qq.search_song(query, num=10)
        except Exception as exc:  # pragma: no cover — defensive
            _log(f"    ↻ title-only 搜索失败: {exc}")
            continue

        score_track = _track_with_title(track, query)
        for cand in alt_cands:
            alt_scored.append((score_track, cand))
            score, method = score_candidate(score_track, cand)
            if overall_cand is None or score > overall_score:
                overall_score = score
                overall_method = f"title-only|{method}"
                overall_cand = cand
                best_score_track = score_track
                if score >= 1.0:
                    return overall_cand, overall_score, overall_method

    if overall_cand is not None and is_confident_match(
        best_score_track, overall_cand, overall_score
    ):
        return overall_cand, overall_score, overall_method

    if overall_cand is not None and overall_score >= 0.8:
        alias_cache: dict[int, dict[str, Any]] = {}

        def aliased(score_track: dict[str, Any]) -> dict[str, Any]:
            key = id(score_track)
            if key not in alias_cache:
                alias_cache[key] = _with_artist_aliases(score_track, qq, alias_client)
            return alias_cache[key]

        alias_track = aliased(best_score_track)
        if alias_track is not best_score_track:
            _log("    ↻ 标题/时长吻合但歌手不同，使用歌手别名再校验")
            for score_track, cand in [(track, cand) for cand in primary_candidates] + alt_scored:
                alias_score_track = aliased(score_track)
                score, method = score_candidate(alias_score_track, cand)
                if score > overall_score:
                    prefix = "title-only|" if score_track is not track else ""
                    overall_score = score
                    overall_method = f"{prefix}{method}"
                    overall_cand = cand
                    alias_track = alias_score_track
                    if score >= 1.0:
                        break
            if overall_cand is not None and is_confident_match(
                alias_track, overall_cand, overall_score
            ):
                return overall_cand, overall_score, overall_method

    return None, overall_score, overall_method


def _match_qq_to_spotify(
    song: dict[str, Any],
    spotify: SpotifyClient,
    qq: QQClient,
    alias_client: MusicBrainzClient,
) -> tuple[dict[str, Any] | None, float, str]:
    """Find a confident Spotify track for a QQ song.

    The matcher is deliberately shared with the Spotify -> QQ direction: the
    normalized QQ song is the source track and Spotify search results are the
    candidates.  Title-only retries still require artist evidence (including
    cached cross-language aliases) before a write is allowed.
    """
    source = dict(song)
    title = (song.get("title") or song.get("name") or "").strip()
    artists = song.get("artists") or song.get("artist") or []
    artist = _primary_artist({"artists": artists})
    if not title:
        return None, 0.0, "none"

    try:
        primary = spotify.search_track(title, artist, limit=10)
    except Exception as exc:  # pragma: no cover - defensive network guard
        _log(f"    ↻ Spotify 搜索失败 {title!r}: {exc}")
        primary = []
    if not isinstance(primary, list):
        primary = []
    all_candidates = list(primary)

    best, best_score, best_method = pick_best(source, primary, threshold=0.8)
    if best is not None:
        return best, best_score, best_method

    overall_cand, overall_score, overall_method = _best_scored_candidate(
        source, primary
    )
    for query in _fallback_title_queries(source):
        if query == title:
            continue
        try:
            candidates = spotify.search_track(query, limit=10)
        except Exception as exc:  # pragma: no cover - defensive network guard
            _log(f"    ↻ Spotify 标题搜索失败 {query!r}: {exc}")
            continue
        if not isinstance(candidates, list):
            continue
        all_candidates.extend(candidates)
        score_source = _track_with_title(source, query)
        for candidate in candidates:
            score, method = score_candidate(score_source, candidate)
            if overall_cand is None or score > overall_score:
                overall_cand = candidate
                overall_score = score
                overall_method = f"title-only|{method}"

    if overall_cand is not None and is_confident_match(
        source, overall_cand, overall_score
    ):
        return overall_cand, overall_score, overall_method

    if overall_cand is not None and overall_score >= 0.8:
        alias_source = _with_artist_aliases(source, qq, alias_client)
        if alias_source is not source:
            for candidate in all_candidates:
                score, method = score_candidate(alias_source, candidate)
                if score > overall_score:
                    overall_cand = candidate
                    overall_score = score
                    overall_method = method
            if overall_cand is not None and is_confident_match(
                alias_source, overall_cand, overall_score
            ):
                return overall_cand, overall_score, overall_method

    return None, overall_score, overall_method


def run_sync(cfg: Config, dry_run: bool = False, full: bool = False) -> int:
    """Execute one sync run. Returns 0 on success, non-zero on failure.

    `full=True` bypasses the incremental snapshot so every Spotify track is
    re-searched (useful after cache drift or manual QQ edits).  When
    ``cfg.sync_direction`` is ``bidirectional``, QQ additions/deletions are
    reconciled back to Spotify after the forward matching pass.
    """
    os.makedirs(os.path.dirname(os.path.abspath(cfg.db_path)) or ".", exist_ok=True)

    conn = dbm.connect(cfg.db_path)
    dbm.init_schema(conn)
    run_id = dbm.insert_run(conn, {"status": "running"})

    added_count = 0
    removed_count = 0
    skipped_count = 0
    failed_count = 0
    notes_parts: list[str] = []
    status = "failed"

    t_start = time.time()
    try:
        mode = "full" if full else "incremental"
        bidirectional = cfg.sync_direction == "bidirectional"
        notes_parts.append(f"direction={cfg.sync_direction}")
        _log(f"[1/9] fetching Spotify playlist {cfg.spotify_playlist_name!r} (mode={mode})...")
        sp = SpotifyClient(
            cfg.spotify_client_id,
            cfg.spotify_client_secret,
            cfg.spotify_refresh_token,
        )
        sp_playlist = sp.find_playlist_by_name(cfg.spotify_playlist_name)
        if not sp_playlist:
            raise RuntimeError(
                f"Spotify playlist not found: {cfg.spotify_playlist_name!r}"
            )
        spotify_playlist_id = str(sp_playlist["id"])
        sp_tracks = sp.get_playlist_tracks(spotify_playlist_id)
        # Sort ascending by added_at so newly-added tracks land last in QQ —
        # i.e. show up at the top of QQ's "added time desc" view, matching
        # the order the user sees on Spotify.
        sp_tracks = sorted(sp_tracks, key=_added_at_sort_key)
        _log(f"      -> {len(sp_tracks)} tracks")
        notes_parts.append(f"spotify_count={len(sp_tracks)}")
        notes_parts.append(f"mode={mode}")
        _persist_spotify_refresh_token_if_rotated(sp, cfg, notes_parts)

        _log("[2/9] refreshing QQ credential...")
        credential = load_credential(cfg.qq_credential_json)
        credential, rotated = ensure_fresh(credential)
        if rotated:
            _log("      -> rotated, persisting new musickey")
            new_blob = dump_credential(credential)
            ok, msg = _push_qq_secret_if_possible(new_blob, cfg)
            notes_parts.append(
                f"qq_credential_rotated={'pushed' if ok else 'local-only'} ({msg})"
            )
        else:
            _log("      -> still valid")
        qq = QQClient(credential)
        alias_client = MusicBrainzClient(cfg.musicbrainz_user_agent, conn)

        _log(f"[3/9] resolving QQ playlist {cfg.qq_playlist_name!r}...")
        target = qq.find_or_create_playlist(cfg.qq_playlist_name)
        dirid = int(target["dirid"])
        qq_current = qq.get_playlist_songs(dirid)
        _log(f"      -> dirid={dirid}, current {len(qq_current)} songs")
        notes_parts.append(f"qq_count={len(qq_current)}")

        _log(f"[4/9] building incremental plan (full={full})...")
        prior_snapshot = dbm.snapshot_get(conn, spotify_playlist_id)
        plan = incremental.build_plan(
            conn, spotify_playlist_id, sp_tracks, dirid, full
        )
        _log(
            f"      -> to_search {len(plan.to_search)}, reused {len(plan.reused_matched)}, "
            f"skipped-unmatched {len(plan.skipped_unmatched)}, "
            f"snapshot-removed {len(plan.to_remove_from_qq)}"
        )
        notes_parts.append(
            f"incremental: search={len(plan.to_search)} reused={len(plan.reused_matched)} "
            f"skipped_unmatched={len(plan.skipped_unmatched)} "
            f"snap_del={len(plan.to_remove_from_qq)}"
        )

        _log(f"[5/9] matching {len(plan.to_search)} track(s) against QQ...")
        matched: list[tuple[dict[str, Any], tuple[int, int]]] = list(plan.reused_matched)
        unmatched_rows: list[dict[str, Any]] = []
        total = len(plan.to_search)
        searched = 0

        for idx, track in enumerate(plan.to_search, 1):
            sp_id = track.get("id")
            sp_title = track.get("title", "")
            sp_artist = _primary_artist(track)
            if not sp_id:
                skipped_count += 1
                _log(f"[{idx}/{total}] ⚠ 跳过（没 Spotify id）")
                _log(f"    Spotify: {sp_artist} 《{sp_title}》")
                continue

            query = _search_query(track)
            searched += 1
            try:
                candidates = qq.search_song(query, num=10)
            except Exception as exc:  # pragma: no cover — defensive
                failed_count += 1
                _log(f"[{idx}/{total}] ✗ QQ 搜索出错")
                _log(f"    Spotify: {sp_artist} 《{sp_title}》")
                _log(f"    错误:    {exc}")
                unmatched_rows.append(
                    {
                        "spotify_track_id": sp_id,
                        "title": sp_title,
                        "artist": sp_artist,
                        "album": track.get("album", ""),
                        "reason": f"search error: {exc}",
                    }
                )
                continue

            best, score, method = _match_title_only_fallback(
                track, candidates, qq, alias_client
            )
            pct = int(round(score * 100))
            reason_cn = explain_method(method)

            if best is None:
                _log(f"[{idx}/{total}] ✗ 找不到")
                _log(f"    Spotify: {sp_artist} 《{sp_title}》")
                _log(f"    得分:    {pct}%（{reason_cn}）")
                unmatched_rows.append(
                    {
                        "spotify_track_id": sp_id,
                        "title": sp_title,
                        "artist": sp_artist,
                        "album": track.get("album", ""),
                        "reason": f"最佳候选 {pct}% ({reason_cn})",
                    }
                )
                continue

            song_type = best.get("type")
            song_id = best.get("id")
            if song_id is None or song_type is None:
                _log(f"[{idx}/{total}] ✗ 候选缺 id/type")
                _log(f"    Spotify: {sp_artist} 《{sp_title}》")
                unmatched_rows.append(
                    {
                        "spotify_track_id": sp_id,
                        "title": sp_title,
                        "artist": sp_artist,
                        "album": track.get("album", ""),
                        "reason": "候选缺 id/type",
                    }
                )
                continue

            dbm.cache_put(conn, _cache_row_for(track, best, score, method))
            matched.append((track, (int(song_id), int(song_type))))
            # 成功不打 per-track log，只周期性打进度。
            if idx % 25 == 0 or idx == total:
                _log(
                    f"—— 进度 {idx}/{total} —— 复用 {len(plan.reused_matched)} / "
                    f"搜索 {searched} / 匹配 {len(matched) - len(plan.reused_matched)} / "
                    f"未匹配 {len(unmatched_rows)}"
                )

        _log(
            f"      小结: 共匹配 {len(matched)} 首 (复用缓存 {len(plan.reused_matched)} + "
            f"本次搜到 {searched - len(unmatched_rows) - failed_count}), 未匹配 {len(unmatched_rows)}"
        )

        spotify_ids_before_reverse = {
            str(track.get("id"))
            for track in sp_tracks
            if track.get("id") is not None
        }
        spotify_ids_to_add: list[str] = []
        spotify_ids_to_remove: list[str] = []
        bidirectional_unmatched: list[dict[str, Any]] = []
        preserved_qq_pairs: list[tuple[int, int]] = []

        if bidirectional:
            _log("[6/9] checking QQ changes for reverse sync...")
            prior_spotify_ids = set((prior_snapshot or {}).get("spotify_track_ids") or [])
            current_qq_ids = set(_qq_current_ids(qq_current))
            deleted_spotify_ids = prior_spotify_ids - spotify_ids_before_reverse
            prior_cache_rows = dbm.cache_get_many(conn, prior_spotify_ids)
            prior_qq_to_spotify = {
                pair[0]: str(row["spotify_track_id"])
                for row in prior_cache_rows.values()
                if (pair := _qq_pair_from_cache(row)) is not None
            }

            # A cached mapping is evidence that this track was shipped by a
            # previous successful run.  If QQ removed it, propagate that
            # deletion to Spotify.  The prior snapshot guard prevents a first
            # sync from interpreting an empty QQ playlist as mass deletion.
            for track in sp_tracks:
                sp_id = track.get("id")
                if sp_id is None or str(sp_id) not in prior_spotify_ids:
                    continue
                row = prior_cache_rows.get(str(sp_id))
                pair = _qq_pair_from_cache(row)
                if pair is not None and pair[0] not in current_qq_ids:
                    spotify_ids_to_remove.append(str(sp_id))

            mapped_qq_ids = {
                pair[0]
                for _track, pair in matched
                if pair[0] in current_qq_ids
            }
            seen_reverse_spotify_ids = set(spotify_ids_before_reverse)
            for song in qq_current:
                pair = _qq_pair_from_song(song)
                if pair is None or pair[0] in mapped_qq_ids:
                    continue

                # If Spotify removed this mapped song since the last snapshot,
                # keep the deletion one-way for this conflict and let the
                # existing QQ diff remove it below.
                cached_sp_id = prior_qq_to_spotify.get(pair[0])
                if cached_sp_id in deleted_spotify_ids:
                    continue

                candidate, score, method = _match_qq_to_spotify(
                    song, sp, qq, alias_client
                )
                if candidate is None or candidate.get("id") is None:
                    bidirectional_unmatched.append(
                        {
                            "title": song.get("title", ""),
                            "artist": _primary_artist(song),
                            "qq_song_id": pair[0],
                            "reason": f"Spotify reverse match {int(round(score * 100))}% ({method})",
                        }
                    )
                    preserved_qq_pairs.append(pair)
                    continue

                sp_id = str(candidate["id"])
                if sp_id in seen_reverse_spotify_ids:
                    preserved_qq_pairs.append(pair)
                    continue

                candidate = dict(candidate)
                candidate["added_at"] = None
                dbm.cache_put(conn, _cache_row_for(candidate, song, score, method))
                matched.append((candidate, pair))
                mapped_qq_ids.add(pair[0])
                seen_reverse_spotify_ids.add(sp_id)
                if sp_id not in spotify_ids_before_reverse:
                    spotify_ids_to_add.append(sp_id)
                    sp_tracks.append(candidate)

            # The QQ side is allowed to retain songs that cannot be mapped
            # safely.  This is the key difference from the legacy mirror mode:
            # a failed reverse lookup never causes a destructive QQ delete.
            notes_parts.append(
                f"reverse: add_spotify={len(spotify_ids_to_add)} "
                f"remove_spotify={len(spotify_ids_to_remove)} "
                f"unmatched_qq={len(bidirectional_unmatched)}"
            )
            if bidirectional_unmatched:
                _log(
                    f"      -> preserved {len(bidirectional_unmatched)} QQ song(s) "
                    "without a confident Spotify match"
                )
            spotify_ids_to_add = list(dict.fromkeys(spotify_ids_to_add))
            spotify_ids_to_remove = list(dict.fromkeys(spotify_ids_to_remove))
        else:
            _log("[6/9] computing mirror diff...")

        spotify_ids_to_remove_set = set(spotify_ids_to_remove)
        # Keep the Spotify playlist order while deduplicating the filtered
        # mapping.  Reverse additions are appended to ``sp_tracks`` above.
        ordered_target_pairs = _ordered_unique_pairs(
            [
                track
                for track in sp_tracks
                if str(track.get("id")) not in spotify_ids_to_remove_set
            ],
            [
                (track, pair)
                for track, pair in matched
                if str(track.get("id")) not in spotify_ids_to_remove_set
            ],
        )
        if bidirectional:
            seen_target_ids = {pair[0] for pair in ordered_target_pairs}
            for pair in preserved_qq_pairs:
                if pair[0] not in seen_target_ids:
                    ordered_target_pairs.append(pair)
                    seen_target_ids.add(pair[0])
            for row in bidirectional_unmatched:
                unmatched_rows.append(
                    {
                        "spotify_track_id": f"qq:{row['qq_song_id']}",
                        "title": row["title"],
                        "artist": row["artist"],
                        "album": "",
                        "reason": row["reason"],
                    }
                )

        target_qq_ids = {pair[0] for pair in ordered_target_pairs}
        current_order_ids = _qq_current_ids(qq_current)
        current_qq_ids = set(current_order_ids)
        diff = compute_mirror_diff(target_qq_ids, current_qq_ids)
        safe, diff_safety_msg = safety_check(diff, len(current_qq_ids), cfg.mirror_delete_threshold)
        reverse_safety_msg = ""
        if bidirectional and spotify_ids_to_remove:
            prior_count = len((prior_snapshot or {}).get("spotify_track_ids") or [])
            reverse_ratio = len(spotify_ids_to_remove) / max(1, prior_count)
            if reverse_ratio > cfg.mirror_delete_threshold:
                safe = False
                reverse_safety_msg = (
                    f"reverse threshold exceeded: would remove "
                    f"{len(spotify_ids_to_remove)}/{prior_count} "
                    f"({reverse_ratio:.2%} > {cfg.mirror_delete_threshold:.2%})"
                )
            else:
                reverse_safety_msg = (
                    f"reverse remove {len(spotify_ids_to_remove)}/{prior_count} "
                    f"({reverse_ratio:.2%} <= {cfg.mirror_delete_threshold:.2%})"
                )

        to_add_pairs = [pair for pair in ordered_target_pairs if pair[0] in diff["to_add"]]
        # For removal we fuse snapshot-driven removals with mirror-diff removals.
        qq_id_to_type = {
            int(s["id"]): int(s.get("type") or 0)
            for s in qq_current
            if s.get("id") is not None
        }
        to_remove_pairs: list[tuple[int, int]] = [
            (qid, qq_id_to_type.get(qid, 0)) for qid in diff["to_remove"]
        ]
        # Snapshot-driven removals land here too — dedupe by qq_song_id.
        seen_rm_ids = {pair[0] for pair in to_remove_pairs}
        for pair in plan.to_remove_from_qq:
            if pair[0] in seen_rm_ids:
                continue
            to_remove_pairs.append(pair)
            seen_rm_ids.add(pair[0])

        target_ids_asc = [pair[0] for pair in ordered_target_pairs]
        add_ids_asc = [pair[0] for pair in to_add_pairs]
        remove_ids = {pair[0] for pair in to_remove_pairs}
        order_ok, order_msg = _incremental_would_match_order(
            current_order_ids, target_ids_asc, add_ids_asc, remove_ids
        )
        write_mode = "incremental" if order_ok else "rebuild"

        if not order_ok and qq_current:
            rebuild_loss = max(0, len(qq_current) - len(ordered_target_pairs))
            rebuild_ratio = rebuild_loss / max(1, len(qq_current))
            rebuild_safe = rebuild_ratio <= cfg.mirror_delete_threshold
            rebuild_safety_msg = (
                f"rebuild loss {rebuild_loss}/{len(qq_current)} "
                f"({rebuild_ratio:.2%} <= {cfg.mirror_delete_threshold:.2%})"
                if rebuild_safe
                else (
                    f"rebuild threshold exceeded: would lose "
                    f"{rebuild_loss}/{len(qq_current)} "
                    f"({rebuild_ratio:.2%} > {cfg.mirror_delete_threshold:.2%})"
                )
            )
            safe = safe and rebuild_safe
        elif not order_ok:
            rebuild_safety_msg = "first-sync rebuild bypass"
        else:
            rebuild_safety_msg = order_msg

        _log(
            f"      -> add {len(to_add_pairs)}, remove {len(to_remove_pairs)} "
            f"(mode: {write_mode}; safety: {diff_safety_msg}; {rebuild_safety_msg}"
            f"{'; ' + reverse_safety_msg if reverse_safety_msg else ''})"
        )
        notes_parts.append(
            f"write_mode={write_mode}; safety={diff_safety_msg}; {rebuild_safety_msg}; "
            f"spotify_add={len(spotify_ids_to_add)} spotify_remove={len(spotify_ids_to_remove)}"
            f"{'; ' + reverse_safety_msg if reverse_safety_msg else ''}"
        )

        skipped_count += len(unmatched_rows)
        skipped_count += len(plan.skipped_unmatched)

        if dry_run:
            _log("[7/9] DRY RUN — skipping writes")
            if write_mode == "rebuild":
                notes_parts.append(
                    f"dry_run: would rebuild delete={len(qq_current)} "
                    f"add={len(ordered_target_pairs)}"
                )
                added_count = len(ordered_target_pairs)
                removed_count = len(qq_current)
            else:
                notes_parts.append(
                    f"dry_run: would delete={len(to_remove_pairs)} "
                    f"add={len(to_add_pairs)}"
                )
                added_count = len(to_add_pairs)
                removed_count = len(to_remove_pairs)
            status = "dry-run"
        elif not safe:
            _log("[7/9] ABORT — safety threshold exceeded")
            notes_parts.append("aborted: safety threshold exceeded")
            status = "aborted"
        else:
            if write_mode == "rebuild":
                current_pairs = [
                    pair
                    for song in qq_current
                    if (pair := _qq_pair_from_song(song)) is not None
                ]
                if current_pairs:
                    _log(
                        f"[7/9] order drift detected: deleting "
                        f"{len(current_pairs)} current QQ song(s)..."
                    )
                    ok = qq.del_songs(dirid, current_pairs)
                    if ok:
                        removed_count = len(current_pairs)
                        _log(f"      -> deleted {removed_count}")
                    else:
                        failed_count += len(current_pairs)
                        _log("      -> FAILED: del_songs returned non-success")
                        notes_parts.append("del_songs returned non-success")
                if not failed_count and ordered_target_pairs:
                    _log(
                        f"      adding {len(ordered_target_pairs)} song(s) "
                        f"in Spotify added_at order..."
                    )
                    ok = qq.add_songs(dirid, ordered_target_pairs)
                    if ok:
                        added_count = len(ordered_target_pairs)
                        _log(f"      -> added {added_count}")
                    else:
                        failed_count += len(ordered_target_pairs)
                        _log("      -> FAILED: add_songs returned non-success")
                        notes_parts.append("add_songs returned non-success")
                notes_parts.append(
                    f"rebuild: deleted={removed_count} added={added_count} "
                    f"target_order={len(ordered_target_pairs)}"
                )
            else:
                if to_remove_pairs:
                    _log(
                        f"[7/9] applying incremental delete: "
                        f"{len(to_remove_pairs)} song(s)..."
                    )
                    ok = qq.del_songs(dirid, to_remove_pairs)
                    if ok:
                        removed_count = len(to_remove_pairs)
                        _log(f"      -> deleted {removed_count}")
                    else:
                        failed_count += len(to_remove_pairs)
                        _log("      -> FAILED: del_songs returned non-success")
                        notes_parts.append("del_songs returned non-success")
                if not failed_count and to_add_pairs:
                    _log(
                        f"[7/9] applying incremental add: "
                        f"{len(to_add_pairs)} song(s) in Spotify added_at order..."
                    )
                    ok = qq.add_songs(dirid, to_add_pairs)
                    if ok:
                        added_count = len(to_add_pairs)
                        _log(f"      -> added {added_count}")
                    else:
                        failed_count += len(to_add_pairs)
                        _log("      -> FAILED: add_songs returned non-success")
                        notes_parts.append("add_songs returned non-success")
                if not to_remove_pairs and not to_add_pairs:
                    _log("[7/9] no QQ writes needed")
                notes_parts.append(
                    f"incremental_apply: deleted={removed_count} "
                    f"added={added_count}"
                )

            if bidirectional:
                if spotify_ids_to_remove:
                    _log(
                        f"[7/9] removing {len(spotify_ids_to_remove)} track(s) from Spotify..."
                    )
                    try:
                        sp.remove_tracks(spotify_playlist_id, spotify_ids_to_remove)
                        _log("      -> Spotify removals applied")
                    except Exception as exc:  # pragma: no cover - network guard
                        failed_count += len(spotify_ids_to_remove)
                        notes_parts.append(f"spotify remove failed: {exc}")
                        _log(f"      -> FAILED: Spotify remove_tracks: {exc}")
                if spotify_ids_to_add:
                    _log(
                        f"[7/9] adding {len(spotify_ids_to_add)} track(s) to Spotify..."
                    )
                    try:
                        sp.add_tracks(spotify_playlist_id, spotify_ids_to_add)
                        _log("      -> Spotify additions applied")
                    except Exception as exc:  # pragma: no cover - network guard
                        failed_count += len(spotify_ids_to_add)
                        notes_parts.append(f"spotify add failed: {exc}")
                        _log(f"      -> FAILED: Spotify add_tracks: {exc}")
                # The list is used as the next snapshot only after all writes
                # succeed.  Filtering removals here also keeps a successful
                # reverse delete from being resurrected by the next run.
                if not failed_count:
                    sp_tracks = [
                        track
                        for track in sp_tracks
                        if str(track.get("id")) not in spotify_ids_to_remove_set
                    ]
            _persist_spotify_refresh_token_if_rotated(sp, cfg, notes_parts)
            status = "failed" if failed_count else "success"

            if status == "success":
                # Persist the current Spotify id set only on a clean apply so
                # the next incremental run diffs against what actually shipped.
                try:
                    incremental.commit_snapshot(
                        conn, spotify_playlist_id, dirid, sp_tracks
                    )
                except Exception as exc:  # pragma: no cover — cache is best-effort
                    _log(f"      WARN: snapshot commit failed: {exc}")
                    notes_parts.append(f"snapshot_commit_error: {exc}")

        _log(f"[8/9] writing unmatched.txt ({len(unmatched_rows)} rows) + log...")
        dbm.insert_unmatched(conn, unmatched_rows)
        report.write_unmatched_txt(cfg.unmatched_path, unmatched_rows)

        notes = "; ".join(notes_parts)
        dbm.finalize_run(
            conn,
            run_id,
            status=status,
            added_count=added_count,
            removed_count=removed_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            notes=notes,
        )
        summary = {
            "run_id": run_id,
            "status": status,
            "added_count": added_count,
            "removed_count": removed_count,
            "skipped_count": skipped_count,
            "failed_count": failed_count,
            "notes": notes,
        }
        report.append_sync_log(cfg.log_path, summary)
        _log(f"[9/9] done in {time.time() - t_start:.1f}s")
        report.print_summary(summary)

        if status in ("success", "dry-run"):
            return 0
        if status == "aborted":
            return 2
        return 1

    except Exception as exc:
        tb = traceback.format_exc()
        notes_parts.append(f"exception: {exc}")
        notes = "; ".join(notes_parts)
        dbm.finalize_run(
            conn,
            run_id,
            status="failed",
            added_count=added_count,
            removed_count=removed_count,
            skipped_count=skipped_count,
            failed_count=failed_count + 1,
            notes=notes,
        )
        summary = {
            "run_id": run_id,
            "status": "failed",
            "added_count": added_count,
            "removed_count": removed_count,
            "skipped_count": skipped_count,
            "failed_count": failed_count + 1,
            "notes": notes,
        }
        report.append_sync_log(cfg.log_path, summary)
        report.print_summary(summary)
        print(tb)
        return 1
    finally:
        conn.close()


__all__ = ["run_sync", "normalize_title", "normalize_artist"]
