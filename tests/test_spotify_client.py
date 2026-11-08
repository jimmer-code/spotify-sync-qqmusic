"""Unit tests for SpotifyClient."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.spotify_client import SpotifyClient, _normalize_track  # noqa: E402


def _mock_response(status: int = 200, json_body: dict | None = None, headers: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.json.return_value = json_body or {}
    if status >= 400:
        from requests import HTTPError

        resp.raise_for_status.side_effect = HTTPError(f"{status}")
    else:
        resp.raise_for_status.return_value = None
    return resp


@patch("src.spotify_client.requests.post")
def test_refresh_access_token_sets_bearer(mock_post):
    mock_post.return_value = _mock_response(
        200, {"access_token": "AT123", "expires_in": 3600}
    )
    c = SpotifyClient("cid", "csec", "rt")
    token = c._refresh_access_token()

    assert token == "AT123"
    assert c._access_token == "AT123"
    # POSTed to the right URL with refresh_token grant
    args, kwargs = mock_post.call_args
    assert args[0] == "https://accounts.spotify.com/api/token"
    assert kwargs["data"]["grant_type"] == "refresh_token"
    assert kwargs["data"]["refresh_token"] == "rt"
    assert kwargs["auth"] == ("cid", "csec")


@patch("src.spotify_client.requests.post")
def test_refresh_access_token_records_rotated_refresh_token(mock_post):
    mock_post.return_value = _mock_response(
        200,
        {
            "access_token": "AT123",
            "refresh_token": "RT_NEW",
            "expires_in": 3600,
        },
    )
    c = SpotifyClient("cid", "csec", "RT_OLD")

    c._refresh_access_token()

    assert c.refresh_token == "RT_NEW"
    assert c.refresh_token_rotated is True


@patch("src.spotify_client.requests.get")
@patch("src.spotify_client.requests.post")
def test_find_playlist_by_name_paginates(mock_post, mock_get):
    mock_post.return_value = _mock_response(
        200, {"access_token": "AT", "expires_in": 3600}
    )
    page1 = {
        "items": [{"id": "p1", "name": "Other"}],
        "next": "https://api.spotify.com/v1/me/playlists?offset=50&limit=50",
    }
    page2 = {"items": [{"id": "p2", "name": "Target"}], "next": None}
    mock_get.side_effect = [_mock_response(200, page1), _mock_response(200, page2)]

    c = SpotifyClient("cid", "csec", "rt")
    pl = c.find_playlist_by_name("Target")

    assert pl is not None
    assert pl["id"] == "p2"
    assert mock_get.call_count == 2


@patch("src.spotify_client.requests.get")
@patch("src.spotify_client.requests.post")
def test_find_playlist_by_name_missing_returns_none(mock_post, mock_get):
    mock_post.return_value = _mock_response(
        200, {"access_token": "AT", "expires_in": 3600}
    )
    mock_get.return_value = _mock_response(
        200, {"items": [{"id": "p1", "name": "X"}], "next": None}
    )
    c = SpotifyClient("cid", "csec", "rt")
    assert c.find_playlist_by_name("Nope") is None


@patch("src.spotify_client.requests.get")
@patch("src.spotify_client.requests.post")
def test_get_playlist_tracks_skips_none_and_normalizes(mock_post, mock_get):
    mock_post.return_value = _mock_response(
        200, {"access_token": "AT", "expires_in": 3600}
    )
    body = {
        "items": [
            {"track": None},  # local file — skip
            {
                "added_at": "2024-01-15T10:30:00Z",
                "track": {
                    "id": "t1",
                    "name": "Song A",
                    "duration_ms": 200000,
                    "artists": [{"name": "Alice"}, {"name": "Bob"}],
                    "album": {"name": "Alb"},
                    "external_ids": {"isrc": "USAA1234567"},
                },
            },
            {
                "added_at": "2024-02-20T12:00:00Z",
                "track": {
                    "id": "t2",
                    "name": "Song B",
                    "duration_ms": 180000,
                    "artists": [{"name": "Solo"}],
                    "album": {"name": "AlbB"},
                    "external_ids": {},
                },
            },
        ],
        "next": None,
    }
    mock_get.return_value = _mock_response(200, body)

    c = SpotifyClient("cid", "csec", "rt")
    tracks = c.get_playlist_tracks("PID")

    assert len(tracks) == 2
    assert tracks[0] == {
        "id": "t1",
        "title": "Song A",
        "artists": ["Alice", "Bob"],
        "album": "Alb",
        "duration_ms": 200000,
        "isrc": "USAA1234567",
        "added_at": "2024-01-15T10:30:00Z",
    }
    assert tracks[1]["isrc"] is None
    assert tracks[1]["added_at"] == "2024-02-20T12:00:00Z"
    # Correct fields param on first request
    _, kwargs = mock_get.call_args_list[0]
    assert "external_ids(isrc)" in kwargs["params"]["fields"]
    assert "items(" in kwargs["params"]["fields"]
    assert "added_at" in kwargs["params"]["fields"]


@patch("src.spotify_client.time.sleep")
@patch("src.spotify_client.requests.get")
@patch("src.spotify_client.requests.post")
def test_get_handles_429_with_retry_after(mock_post, mock_get, mock_sleep):
    mock_post.return_value = _mock_response(
        200, {"access_token": "AT", "expires_in": 3600}
    )
    throttled = _mock_response(429, {}, headers={"Retry-After": "2"})
    ok = _mock_response(200, {"items": [], "next": None})
    mock_get.side_effect = [throttled, throttled, ok]

    c = SpotifyClient("cid", "csec", "rt")
    result = c.find_playlist_by_name("Any")

    assert result is None
    assert mock_get.call_count == 3
    # Backoff uses Retry-After * 2**attempt
    waits = [call.args[0] for call in mock_sleep.call_args_list]
    assert waits == [2, 4]


@patch("src.spotify_client.time.sleep")
@patch("src.spotify_client.requests.get")
@patch("src.spotify_client.requests.post")
def test_get_429_gives_up_after_max_retries(mock_post, mock_get, mock_sleep):
    mock_post.return_value = _mock_response(
        200, {"access_token": "AT", "expires_in": 3600}
    )
    mock_get.return_value = _mock_response(429, {}, headers={"Retry-After": "1"})

    c = SpotifyClient("cid", "csec", "rt")
    with pytest.raises(Exception):
        c.find_playlist_by_name("Any")

    # 1 initial + 3 retries = 4 attempts
    assert mock_get.call_count == 4


def test_normalize_track_handles_missing_fields():
    out = _normalize_track(
        {"id": "x", "name": "T", "artists": [], "album": None, "external_ids": None}
    )
    assert out["artists"] == []
    assert out["album"] == ""
    assert out["isrc"] is None
    assert out["duration_ms"] == 0
    assert out["added_at"] is None


def test_normalize_track_carries_added_at():
    out = _normalize_track(
        {"id": "x", "name": "T", "artists": [], "album": None, "external_ids": None},
        added_at="2024-05-07T01:02:03Z",
    )
    assert out["added_at"] == "2024-05-07T01:02:03Z"


@patch("src.spotify_client.requests.request")
@patch("src.spotify_client.requests.post")
def test_remove_tracks_uses_items_endpoint(mock_post, mock_request):
    mock_post.return_value = _mock_response(
        200, {"access_token": "AT", "expires_in": 3600}
    )
    mock_request.return_value = _mock_response(200, {"snapshot_id": "snap"})

    c = SpotifyClient("cid", "csec", "rt")
    c.remove_tracks("PID", ["t1", "t2"])

    args, kwargs = mock_request.call_args
    assert args[0] == "DELETE"
    assert args[1] == "https://api.spotify.com/v1/playlists/PID/items"
    assert kwargs["json"] == {
        "items": [
            {"uri": "spotify:track:t1"},
            {"uri": "spotify:track:t2"},
        ]
    }
