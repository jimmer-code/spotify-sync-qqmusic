"""Environment-driven configuration with fail-fast validation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


_REQUIRED = (
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
    "SPOTIFY_REFRESH_TOKEN",
    "SPOTIFY_PLAYLIST_NAME",
    "QQ_PLAYLIST_NAME",
    "QQ_CREDENTIAL_JSON",
)


_DEFAULT_MB_USER_AGENT = "spotify-qq-sync/0.2 (CarfagnoArcino@gmail.com)"


class ConfigError(RuntimeError):
    """Friendly configuration error shown to the user without a traceback."""


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _anchor(path: str) -> str:
    """Make a path absolute by anchoring it to the project root if relative."""
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str(_project_root() / p)


@dataclass(frozen=True)
class Config:
    spotify_client_id: str
    spotify_client_secret: str
    spotify_refresh_token: str
    spotify_playlist_name: str
    qq_playlist_name: str
    qq_credential_json: str
    sync_direction: str = "spotify-to-qq"
    sync_playlist_names: tuple[str, ...] = ()
    apple_music_media_user_token: str | None = None
    gh_pat_secrets_write: str | None = None
    mirror_delete_threshold: float = 0.2
    db_path: str = "data/sync.db"
    log_path: str = "data/sync.log"
    unmatched_path: str = "data/unmatched.txt"
    musicbrainz_user_agent: str = _DEFAULT_MB_USER_AGENT


def _load_dotenv_if_present() -> None:
    """Load `.env` from project root, not cwd. Silently skip if absent."""
    env_path = _project_root() / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        return
    load_dotenv(str(env_path), override=False)


def load_config() -> Config:
    _load_dotenv_if_present()

    missing = [name for name in _REQUIRED if not os.environ.get(name)]
    if missing:
        env_path = _project_root() / ".env"
        msg_lines = [
            "缺少必填的配置项 / Missing required config:",
            *[f"  - {name}" for name in missing],
            "",
            f"检查 {env_path} 是否写好，或在环境变量里设置上述项。",
            "Check that the file above has all of the keys set, or export them as env vars.",
            "",
            "第一次用请看 README 的 Setup 段：",
            "  https://github.com/jiaweizhang1995/spotify-sync-qqmusic#快速开始--setup",
        ]
        raise ConfigError("\n".join(msg_lines))

    threshold_raw = os.environ.get("MIRROR_DELETE_THRESHOLD", "0.2")
    try:
        threshold = float(threshold_raw)
    except ValueError as exc:
        raise ConfigError(
            f"MIRROR_DELETE_THRESHOLD 必须是小数，现在是 {threshold_raw!r}"
        ) from exc

    sync_direction = (os.environ.get("SYNC_DIRECTION") or "spotify-to-qq").strip().lower()
    if sync_direction not in {"spotify-to-qq", "bidirectional"}:
        raise ConfigError(
            "SYNC_DIRECTION 必须是 spotify-to-qq 或 bidirectional，"
            f"现在是 {sync_direction!r}"
        )

    sync_playlist_names = tuple(
        dict.fromkeys(
            name.strip()
            for name in os.environ.get("SPOTIFY_PLAYLIST_NAMES", "").split(",")
            if name.strip()
        )
    ) or (os.environ["SPOTIFY_PLAYLIST_NAME"],)

    return Config(
        spotify_client_id=os.environ["SPOTIFY_CLIENT_ID"],
        spotify_client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        spotify_refresh_token=os.environ["SPOTIFY_REFRESH_TOKEN"],
        spotify_playlist_name=os.environ["SPOTIFY_PLAYLIST_NAME"],
        qq_playlist_name=os.environ["QQ_PLAYLIST_NAME"],
        qq_credential_json=os.environ["QQ_CREDENTIAL_JSON"],
        sync_direction=sync_direction,
        sync_playlist_names=sync_playlist_names,
        apple_music_media_user_token=(
            os.environ.get("APPLE_MUSIC_MEDIA_USER_TOKEN") or None
        ),
        gh_pat_secrets_write=os.environ.get("GH_PAT_SECRETS_WRITE") or None,
        mirror_delete_threshold=threshold,
        db_path=_anchor(os.environ.get("DB_PATH", "data/sync.db")),
        log_path=_anchor(os.environ.get("LOG_PATH", "data/sync.log")),
        unmatched_path=_anchor(os.environ.get("UNMATCHED_PATH", "data/unmatched.txt")),
        musicbrainz_user_agent=(
            os.environ.get("MUSICBRAINZ_USER_AGENT") or _DEFAULT_MB_USER_AGENT
        ),
    )
