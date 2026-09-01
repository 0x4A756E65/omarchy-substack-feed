import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


BACKEND_PATH = Path(__file__).resolve().parents[1] / "substack_backend.py"
PANEL_PATH = BACKEND_PATH.with_name("Panel.qml")
SERVICE_PATH = BACKEND_PATH.with_name("Service.qml")
SPEC = importlib.util.spec_from_file_location("substack_backend", BACKEND_PATH)
backend = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(backend)


RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Small Hours</title>
    <item>
      <title>A useful post &amp; its title</title>
      <link>https://smallhours.substack.com/p/useful-post</link>
      <guid>post-42</guid>
      <dc:creator>Robin Writer</dc:creator>
      <pubDate>Fri, 28 Aug 2026 13:03:10 GMT</pubDate>
      <description><![CDATA[<p>A clean <strong>description</strong>.</p>]]></description>
      <enclosure url="https://substackcdn.com/image.jpg" type="image/jpeg" />
    </item>
  </channel>
</rss>"""


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        backend.STATE_ROOT = root
        backend.STATE_FILE = root / "state.json"
        backend.CONFIG_FILE = root / "config.json"
        backend.STATE_LOCK = root / "state.lock"
        backend.CONFIG_LOCK = root / "config.lock"
        backend.DAEMON_LOCK = root / "daemon.lock"
        backend.AUTH_LOCK = root / "auth.lock"
        backend.REFRESH_REQUEST = root / "refresh.request"
        backend.ensure_dirs()
        backend.atomic_json(backend.STATE_FILE, backend.default_state())

    def tearDown(self):
        self.temporary.cleanup()

    def publication(self):
        return {
            "id": "smallhours",
            "name": "Small Hours",
            "author": "Robin Writer",
            "feed_url": "https://smallhours.substack.com/feed",
        }

    def test_subscription_response_uses_authenticated_custom_domain_feed(self):
        recognized, publications = backend.parse_publications(
            {
                "subscriptions": [{"publication_id": 42, "membership_state": "free_signup"}],
                "publications": [
                    {
                        "id": 42,
                        "name": "Small Hours",
                        "subdomain": "smallhours",
                        "custom_domain": "smallhours.example",
                        "author_name": "Robin Writer",
                        "logo_url": "https://substackcdn.com/small-hours.png",
                    }
                ],
            }
        )
        self.assertTrue(recognized)
        self.assertEqual(publications[0]["url"], "https://smallhours.example")
        self.assertEqual(publications[0]["custom_domain"], "smallhours.example")
        self.assertEqual(publications[0]["feed_url"], "https://smallhours.example/feed")
        self.assertEqual(publications[0]["logo_url"], "https://substackcdn.com/small-hours.png")

    def test_page_v2_marks_admin_publications_as_owned(self):
        recognized, publications = backend.parse_publications(
            {
                "subscriptions": [
                    {"publication_id": 42, "membership_state": "subscribed"},
                    {"publication_id": 99, "membership_state": "free_signup"},
                ],
                "publications": [
                    {"id": 42, "name": "My Essays", "subdomain": "my-essays"},
                    {"id": 99, "name": "Someone Else", "subdomain": "someone-else"},
                ],
                "publicationUsers": [
                    {"publication_id": 42, "role": "admin", "is_primary": True},
                ],
            }
        )
        self.assertTrue(recognized)
        self.assertEqual(
            [(publication["id"], publication["owned"]) for publication in publications],
            [("my-essays", True), ("someone-else", False)],
        )

    def test_wrapped_subscription_shape_and_publication_map_are_supported(self):
        recognized, publications = backend.parse_publications(
            {
                "result": {
                    "subscriptions": [{"publication_id": "42", "membership_state": "paid"}],
                    "publicationMap": {
                        "42": {
                            "id": "42",
                            "name": "Small Hours",
                            "subdomain": "smallhours",
                            "logo_url": "https://substackcdn.com/logo.png",
                        }
                    },
                }
            }
        )
        self.assertTrue(recognized)
        self.assertEqual(publications[0]["id"], "smallhours")
        self.assertEqual(publications[0]["membership"], "paid")
        self.assertEqual(publications[0]["custom_domain"], "")
        self.assertEqual(publications[0]["feed_url"], "https://smallhours.substack.com/feed")

    def test_malformed_custom_domain_metadata_falls_back_to_canonical_feed(self):
        recognized, publications = backend.parse_publications(
            {
                "subscriptions": [{"publication_id": 42}],
                "publications": [
                    {
                        "id": 42,
                        "name": "Small Hours",
                        "subdomain": "smallhours",
                        "custom_domain": "letters.example/unexpected-path",
                    }
                ],
            }
        )
        self.assertTrue(recognized)
        self.assertEqual(publications[0]["custom_domain"], "")
        self.assertEqual(publications[0]["feed_url"], "https://smallhours.substack.com/feed")

    def test_rss_metadata_is_safely_normalized(self):
        articles = backend.parse_feed(RSS, self.publication())
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["title"], "A useful post & its title")
        self.assertEqual(articles[0]["excerpt"], "A clean description.")
        self.assertEqual(articles[0]["publication"], "Small Hours")
        self.assertRegex(articles[0]["id"], r"^[0-9a-f]{24}$")

    def test_html_cleanup_ignores_active_content_with_spaced_end_tags(self):
        value = "<p>Keep this</p><script>alert('drop')</script ><style>drop too</style >"
        self.assertEqual(backend.clean_text(value), "Keep this")

    def test_first_sync_seeds_and_second_sync_marks_only_new_article_unread(self):
        publication = self.publication()

        def seed(state):
            state["subscriptions"] = [
                {
                    **publication,
                    "seen_ids": [],
                    "last_checked": 0,
                    "next_poll": 0,
                }
            ]

        backend.mutate_state(seed)
        initial = backend.parse_feed(RSS, publication)
        self.assertEqual(backend.merge_feed("smallhours", initial, 100), [])
        self.assertEqual(backend.load_state()["unread_count"], 0)

        newer = dict(initial[0])
        newer["id"] = "a" * 24
        newer["title"] = "A genuinely new post"
        newer["published_ts"] += 100
        notified = backend.merge_feed("smallhours", [newer, initial[0]], 200)
        state = backend.load_state()
        self.assertEqual([item["title"] for item in notified], ["A genuinely new post"])
        self.assertEqual(state["unread_count"], 1)
        self.assertEqual(state["articles"][0]["title"], "A genuinely new post")

    def test_magic_link_is_restricted_to_substack_sign_in(self):
        self.assertTrue(backend.magic_link_allowed("https://substack.com/sign-in?token=header.payload.signature"))
        self.assertTrue(backend.magic_link_allowed("https://www.substack.com/sign-in?token=abc"))
        self.assertFalse(backend.magic_link_allowed("http://substack.com/sign-in?token=abc"))
        self.assertFalse(backend.magic_link_allowed("https://substack.example/sign-in?token=abc"))
        self.assertFalse(backend.magic_link_allowed("https://substack.com/library?token=abc"))
        self.assertFalse(backend.magic_link_allowed("https://substack.com/sign-in"))
        self.assertFalse(backend.magic_link_allowed("https://substack.com:8443/sign-in?token=abc"))

    def test_auth_navigation_is_restricted_to_substack_and_cloudflare_challenge(self):
        self.assertTrue(backend.auth_navigation_allowed("https://substack.com/sign-in"))
        self.assertTrue(backend.auth_navigation_allowed("https://www.substack.com/library"))
        self.assertTrue(backend.auth_navigation_allowed("https://challenges.cloudflare.com/turnstile"))
        self.assertFalse(backend.auth_navigation_allowed("https://example.com/sign-in"))
        self.assertFalse(backend.auth_navigation_allowed("http://substack.com/sign-in"))
        self.assertFalse(backend.auth_navigation_allowed("https://substack.com:8443/sign-in"))

    def test_requests_fail_closed_outside_the_exact_https_origin(self):
        backend.validate_request_target(
            "https://substack.com/api/v1/user/profile/self", {"substack.com"}, {"connect.sid": "ok"}
        )
        with self.assertRaises(backend.BackendError):
            backend.validate_request_target("http://substack.com/api", {"substack.com"}, None)
        with self.assertRaises(backend.BackendError):
            backend.validate_request_target("https://www.substack.com/api", {"substack.com"}, {"connect.sid": "ok"})
        with self.assertRaises(backend.BackendError):
            backend.validate_request_target(
                "https://newsletter.substack.com:8443/feed", {"newsletter.substack.com"}, None
            )
        with self.assertRaises(backend.BackendError):
            backend.RejectRedirects().redirect_request(None, None, 302, "Found", {}, "https://example.com/")

    def test_session_cookie_values_cannot_inject_headers(self):
        self.assertEqual(
            backend.normalize_session_cookies({"connect.sid": "safe.value"}), {"connect.sid": "safe.value"}
        )
        self.assertEqual(backend.normalize_session_cookies({"connect.sid": "bad\r\nHeader: injected"}), {})
        self.assertEqual(backend.normalize_session_cookies({"other": "ignored"}), {})

    def test_external_urls_reject_local_targets_and_untrusted_artwork(self):
        self.assertEqual(backend.safe_article_url("https://127.0.0.1/post"), "")
        self.assertEqual(backend.safe_article_url("https://localhost/post"), "")
        self.assertEqual(backend.safe_article_url("https://intranet/post"), "")
        self.assertEqual(backend.safe_article_url("https://example.com:8443/post"), "")
        self.assertEqual(backend.safe_article_url("https://example.com/post"), "https://example.com/post")
        self.assertEqual(backend.safe_image_url("https://tracker.example/pixel.png"), "")
        self.assertEqual(
            backend.safe_image_url("https://substack-post-media.s3.amazonaws.com/public/logo.png"),
            "https://substack-post-media.s3.amazonaws.com/public/logo.png",
        )

    def test_custom_feed_requires_authenticated_domain_substack_dns_and_response_identity(self):
        publication = {
            **self.publication(),
            "subdomain": "smallhours",
            "custom_domain": "letters.example",
            "feed_url": "https://letters.example/feed",
        }
        resolved = [
            (
                backend.socket.AF_INET,
                backend.socket.SOCK_STREAM,
                6,
                "target.substack-custom-domains.com",
                ("104.18.36.24", 443),
            )
        ]
        with mock.patch.object(backend.socket, "getaddrinfo", return_value=resolved):
            self.assertEqual(
                backend.publication_feed_target(publication),
                ("https://letters.example/feed", "letters.example", True),
            )
        backend.validate_custom_feed_response(
            publication,
            {"x-sub": "smallhours", "X-Served-By": "Substack"},
        )
        with self.assertRaises(backend.BackendError):
            backend.validate_custom_feed_response(
                publication,
                {"X-Sub": "someone-else", "X-Served-By": "Substack"},
            )

    def test_custom_feed_rejects_unapproved_hosts_private_dns_and_non_substack_dns(self):
        publication = {
            **self.publication(),
            "subdomain": "smallhours",
            "custom_domain": "letters.example",
            "feed_url": "https://other.example/feed",
        }
        with self.assertRaises(backend.BackendError):
            backend.publication_feed_target(publication)

        publication["feed_url"] = "https://letters.example/feed"
        private_dns = [
            (
                backend.socket.AF_INET,
                backend.socket.SOCK_STREAM,
                6,
                "target.substack-custom-domains.com",
                ("127.0.0.1", 443),
            )
        ]
        with (
            mock.patch.object(backend.socket, "getaddrinfo", return_value=private_dns),
            self.assertRaises(backend.BackendError),
        ):
            backend.publication_feed_target(publication)

        unrelated_dns = [
            (
                backend.socket.AF_INET,
                backend.socket.SOCK_STREAM,
                6,
                "hosting.example",
                ("93.184.216.34", 443),
            )
        ]
        with (
            mock.patch.object(backend.socket, "getaddrinfo", return_value=unrelated_dns),
            self.assertRaises(backend.BackendError),
        ):
            backend.publication_feed_target(publication)

    def test_doctype_and_entities_are_rejected_before_xml_parsing(self):
        dangerous = b'<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><rss><channel/></rss>'
        with self.assertRaises(backend.BackendError):
            backend.parse_feed(dangerous, self.publication())

    def test_one_empty_subscription_sync_preserves_last_good_feed(self):
        publication = {**self.publication(), "seen_ids": [], "last_checked": 1, "next_poll": 100}
        article = {
            "id": "a" * 24,
            "publication_id": "smallhours",
            "title": "Keep me",
            "link": "https://smallhours.substack.com/p/keep-me",
            "unread": False,
        }

        def seed(state):
            state["subscriptions"] = [publication]
            state["articles"] = [article]

        backend.mutate_state(seed)
        with (
            mock.patch.object(backend, "fetch_subscriptions", return_value=[]),
            mock.patch.object(backend, "fetch_profile", return_value={"name": "Reader"}),
            mock.patch.object(backend, "now_ts", return_value=1000),
        ):
            backend.sync_publications({"connect.sid": "safe.value"})
            first = backend.load_state()
            self.assertEqual(len(first["subscriptions"]), 1)
            self.assertEqual(len(first["articles"]), 1)
            self.assertEqual(first["empty_subscription_confirmations"], 1)
            self.assertEqual(first["subscription_sync_due"], 1000 + backend.EMPTY_SUBSCRIPTION_RECHECK_SECONDS)

            backend.sync_publications({"connect.sid": "safe.value"})
            second = backend.load_state()
            self.assertEqual(second["subscriptions"], [])
            self.assertEqual(second["articles"], [])
            self.assertEqual(second["empty_subscription_confirmations"], 0)

    def test_qml_security_and_singleton_ipc_guards_remain_present(self):
        panel = PANEL_PATH.read_text(encoding="utf-8")
        service = SERVICE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("IpcHandler {", panel)
        self.assertEqual(service.count("IpcHandler {"), 1)
        self.assertIn('target: "0x4a756e65.omarchy-substack"', service)
        self.assertIn("textFormat: Text.PlainText", panel)
        self.assertIn("Blocked navigation outside Substack", BACKEND_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
