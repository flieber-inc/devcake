"""PMO asset download URL allowlist (docs/14 §11)."""

import pytest

from devcake.domain.asset_fetch import (
    AssetUrlError,
    assert_downloadable_asset_url,
    assert_fetch_netloc,
    assert_path_prefix,
    enforce_download_byte_cap,
)


def test_linear_uploads_host_allowed():
    url = "https://uploads.linear.app/abc/file.png"
    assert assert_downloadable_asset_url(
        url, allowed_hosts={"uploads.linear.app"}) == url


def test_evil_host_rejected():
    with pytest.raises(AssetUrlError, match="not in allowlist"):
        assert_downloadable_asset_url(
            "https://evil.example/steal",
            allowed_hosts={"uploads.linear.app"})


def test_file_and_unknown_schemes_rejected():
    with pytest.raises(AssetUrlError):
        assert_downloadable_asset_url(
            "file:///etc/passwd",
            allowed_hosts={"uploads.linear.app"})
    with pytest.raises(AssetUrlError, match="https"):
        assert_downloadable_asset_url(
            "ftp://uploads.linear.app/x",
            allowed_hosts={"uploads.linear.app"})


def test_metadata_ip_refused_when_not_allowlisted():
    """Host policy (not scheme alone): link-local IP outside allowlist fails."""
    with pytest.raises(AssetUrlError, match="not in allowlist"):
        assert_downloadable_asset_url(
            "http://169.254.169.254/latest/meta-data/",
            allowed_hosts={"uploads.linear.app"},
            allow_http=True,
        )


def test_allowlisted_http_host_accepted_when_flagged():
    """No special link-local denylist — allowlist is the control (docs/14)."""
    url = "http://169.254.169.254/latest/meta-data/"
    assert assert_downloadable_asset_url(
        url, allowed_hosts={"169.254.169.254"}, allow_http=True,
    ) == url


def test_userinfo_rejected():
    with pytest.raises(AssetUrlError, match="userinfo"):
        assert_downloadable_asset_url(
            "https://user:pass@uploads.linear.app/x",
            allowed_hosts={"uploads.linear.app"})


def test_http_allowed_only_when_flagged():
    with pytest.raises(AssetUrlError, match="https"):
        assert_downloadable_asset_url(
            "http://gitea:3000/a/b",
            allowed_hosts={"gitea"})
    assert assert_downloadable_asset_url(
        "http://gitea:3000/a/b",
        allowed_hosts={"gitea"},
        allow_http=True,
    ).startswith("http://gitea")


def test_empty_url_rejected():
    with pytest.raises(AssetUrlError, match="empty"):
        assert_downloadable_asset_url("", allowed_hosts={"x"})


def test_resolve_redirect_location_joins_relative_and_absolute():
    from devcake.domain.asset_fetch import resolve_redirect_location
    assert resolve_redirect_location(
        "https://uploads.linear.app/a/start", "/a/final"
    ) == "https://uploads.linear.app/a/final"
    assert resolve_redirect_location(
        "https://uploads.linear.app/a/start",
        "https://uploads.linear.app/a/other",
    ) == "https://uploads.linear.app/a/other"
    # uppercase scheme still treated as absolute via urljoin
    assert resolve_redirect_location(
        "https://uploads.linear.app/a/start",
        "HTTPS://uploads.linear.app/a/U",
    ).lower().startswith("https://uploads.linear.app/")


def test_host_case_normalized():
    url = "https://Uploads.Linear.App/file"
    assert assert_downloadable_asset_url(
        url, allowed_hosts={"uploads.linear.app"}) == url


def test_assert_fetch_netloc_and_path_prefix():
    origin = "http://gitea:3000"
    ok = "http://gitea:3000/attachments/abc"
    assert assert_fetch_netloc(ok, origin) == ok
    assert assert_path_prefix(ok, "/attachments/") == ok
    with pytest.raises(AssetUrlError, match="netloc"):
        assert_fetch_netloc("http://gitea:9/attachments/x", origin)
    with pytest.raises(AssetUrlError, match="path"):
        assert_path_prefix("http://gitea:3000/api/v1/user", "/attachments/")


def test_enforce_download_byte_cap():
    assert enforce_download_byte_cap(
        b"hi", content_length="2", max_bytes=10) == b"hi"
    with pytest.raises(AssetUrlError, match="Content-Length"):
        enforce_download_byte_cap(
            b"x", content_length="100", max_bytes=10)
    with pytest.raises(AssetUrlError, match="body"):
        enforce_download_byte_cap(
            b"x" * 20, content_length=None, max_bytes=10)
