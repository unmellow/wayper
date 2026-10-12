from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from wayper.wallhaven_web import WallhavenWeb, _SessionStatus


class WallhavenWebTest(unittest.TestCase):
    def test_login_preserves_cached_session_when_verification_is_unavailable(self) -> None:
        client = WallhavenWeb("user", "pass")
        try:
            with (
                patch.object(client, "_load_cookies", return_value=True),
                patch.object(
                    client,
                    "_verify_session",
                    return_value=_SessionStatus.UNAVAILABLE,
                ),
                patch.object(client, "_clear_cookies") as clear_cookies,
                patch.object(client, "_load_browser_cookies") as load_browser_cookies,
                patch.object(client, "_nodriver_login") as nodriver_login,
            ):
                logged_in = client._login()
        finally:
            client.close()

        self.assertFalse(logged_in)
        clear_cookies.assert_not_called()
        load_browser_cookies.assert_not_called()
        nodriver_login.assert_not_called()

    def test_background_login_never_opens_browser_for_cloudflare(self) -> None:
        response = httpx.Response(
            403,
            text="challenge-platform cloudflare",
            request=httpx.Request("GET", "https://wallhaven.cc/login"),
        )
        client = WallhavenWeb("user", "pass")
        try:
            with (
                patch.object(client, "_load_cookies", return_value=False),
                patch.object(
                    client,
                    "_load_browser_cookies",
                    return_value=_SessionStatus.UNAUTHENTICATED,
                ),
                patch.object(client._client, "get", return_value=response),
                patch.object(client, "_is_cf_challenge", return_value=True),
                patch.object(client, "_nodriver_login") as nodriver_login,
            ):
                logged_in = client._login()
        finally:
            client.close()

        self.assertFalse(logged_in)
        nodriver_login.assert_not_called()

    def test_explicit_login_allows_browser_for_cloudflare(self) -> None:
        response = httpx.Response(
            403,
            text="challenge-platform cloudflare",
            request=httpx.Request("GET", "https://wallhaven.cc/login"),
        )
        client = WallhavenWeb("user", "pass")
        try:
            with (
                patch.object(client, "_load_cookies", return_value=False),
                patch.object(
                    client,
                    "_load_browser_cookies",
                    return_value=_SessionStatus.UNAUTHENTICATED,
                ),
                patch.object(client._client, "get", return_value=response),
                patch.object(client, "_is_cf_challenge", return_value=True),
                patch.object(client, "_nodriver_login", return_value=True) as nodriver_login,
            ):
                logged_in = client._login(allow_browser=True, force=True)
        finally:
            client.close()

        self.assertTrue(logged_in)
        nodriver_login.assert_called_once_with()

    def test_parse_fav_button_add_state_with_nested_add_link(self) -> None:
        html = """
        <meta name="csrf-token" content="token">
        <section id="fav-button" class="button add-button">
          <a class="item add-fav" href="/wallpaper/favorite/abc123">Add</a>
        </section>
        """

        client = WallhavenWeb("user", "pass")
        try:
            url, is_faved = client._parse_fav_button(html)
        finally:
            client.close()

        self.assertEqual(url, "https://wallhaven.cc/wallpaper/favorite/abc123")
        self.assertIs(is_faved, False)

    def test_parse_fav_button_favorited_state_with_direct_link(self) -> None:
        html = """
        <a class="button" href="https://wallhaven.cc/wallpaper/favorite/abc123"
           id="fav-button">
          Favorite
        </a>
        """

        client = WallhavenWeb("user", "pass")
        try:
            url, is_faved = client._parse_fav_button(html)
        finally:
            client.close()

        self.assertEqual(url, "https://wallhaven.cc/wallpaper/favorite/abc123")
        self.assertIs(is_faved, True)

    def test_parse_fav_button_ignores_favorites_count_overlay(self) -> None:
        html = """
        <dd>
          <a class="overlay-anchor" data-href="https://wallhaven.cc/wallpaper/fav/abc123">
            8
          </a>
        </dd>
        """

        client = WallhavenWeb("user", "pass")
        try:
            url, is_faved = client._parse_fav_button(html)
        finally:
            client.close()

        self.assertIsNone(url)
        self.assertIsNone(is_faved)


if __name__ == "__main__":
    unittest.main()
