"""PMO asset download URL allowlist (docs/14 §11)."""

import pytest

from devcake.domain.asset_fetch import AssetUrlError, assert_downloadable_asset_url


def test_linear_uploads_host_allowed():
    url = "https://uploads.linear.app/abc/file.png"
    assert assert_downloadable_asset_url(
        url, allowed_hosts={"uploads.linear.app"}) == url


def test_evil_host_rejected():
    with pytest.raises(AssetUrlError, match="not in allowlist"):
        assert_downloadable_asset_url(
            "https://evil.example/steal",
            allowed_hosts={"uploads.linear.app"})


def test_metadata_and_file_schemes_rejected():
    with pytest.raises(AssetUrlError):
        assert_downloadable_asset_url(
            "http://169.254.169.254/latest/meta-data/",
            allowed_hosts={"169.254.169.254"})
    with pytest.raises(AssetUrlError):
        assert_downloadable_asset_url(
            "file:///etc/passwd",
            allowed_hosts={"uploads.linear.app"})


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
