import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from scripts import sync_uptimerobot_ips as sync


class SourceTests(unittest.TestCase):
    def test_text_feeds_normalize_sort_and_deduplicate(self):
        for fetch in (sync.fetch_hetrixtools_ips, sync.fetch_uptimerobot_text_ips):
            with self.subTest(provider=fetch.__name__), patch.object(
                sync, "http_text", return_value=(
                    "# monitoring IPs\n\n2001:db8::1\n192.0.2.2\n"
                    " 192.0.2.1/32 \n192.0.2.2/32\n"
                )
            ):
                self.assertEqual(fetch(), [
                    "192.0.2.1/32", "192.0.2.2/32", "2001:db8::1/128"
                ])

    def test_text_feeds_reject_empty_or_partially_invalid_responses(self):
        for fetch in (sync.fetch_hetrixtools_ips, sync.fetch_uptimerobot_text_ips):
            for body in ("", "# no addresses\n", "<html>unavailable</html>",
                         "192.0.2.1\ninvalid-ip", "192.0.2.1\n999.0.0.1"):
                with self.subTest(provider=fetch.__name__, body=body), patch.object(
                    sync, "http_text", return_value=body
                ), self.assertRaises(RuntimeError):
                    fetch()

    def test_meta_parses_both_address_families(self):
        with patch.object(sync, "http_json", return_value={"prefixes": [
            {"ip_prefix": "192.0.2.1/32"},
            {"ipv6_prefix": "2001:db8::1/128"},
            {"ip_prefix": "192.0.2.1"},
        ]}):
            self.assertEqual(sync.fetch_uptimerobot_meta_ips(), [
                "192.0.2.1/32", "2001:db8::1/128"
            ])

    def test_invalid_meta_uses_text_fallback(self):
        for body in ({}, {"prefixes": []}, {"prefixes": [{}]},
                     {"prefixes": ["invalid"]}, {"prefixes": [
                         {"ip_prefix": "192.0.2.1"}, {"ipv6_prefix": "invalid"}
                     ]}):
            with self.subTest(body=body), patch.object(
                sync, "http_json", return_value=body
            ), patch.object(sync, "http_text", return_value="192.0.2.2"), \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(sync.fetch_uptimerobot_ips(), ["192.0.2.2/32"])


@patch.dict(sync.os.environ, {"CF_ACCOUNT_ID": "account", "CF_API_TOKEN": "token"}, clear=True)
class SyncTests(unittest.TestCase):
    def test_union_keeps_either_provider_and_removes_only_stale_ips(self):
        items = [
            {"id": "uptime", "ip": "192.0.2.1"},
            {"id": "hetrix", "ip": "192.0.2.2"},
            {"id": "shared", "ip": "192.0.2.3"},
            {"id": "stale", "ip": "192.0.2.99"},
        ]
        calls = []

        def api(token, method, path, data=None):
            calls.append((method, path, data))
            if path.endswith("/lists"):
                return {"result": [{"id": "list", "name": "uptimerobot_ips", "kind": "ip"}]}
            if method == "GET" and path.endswith("/items"):
                return {"result": items}
            if "/bulk_operations/" in path:
                return {"result": {"status": "completed"}}
            return {"result": {"operation_id": "operation"}}

        output = io.StringIO()
        with patch.object(sync, "fetch_uptimerobot_ips", return_value=[
            "192.0.2.1/32", "192.0.2.3/32"
        ]), patch.object(sync, "fetch_hetrixtools_ips", return_value=[
            "192.0.2.2/32", "192.0.2.3/32", "2001:db8::1/128"
        ]), patch.object(sync, "cloudflare_api", side_effect=api), redirect_stdout(output):
            sync.main()

        writes = [call for call in calls if call[0] != "GET"]
        self.assertEqual(writes, [
            ("POST", "/accounts/account/rules/lists/list/items", [
                {"ip": "2001:db8::1/128", "comment": sync.MANAGED_COMMENT}
            ]),
            ("DELETE", "/accounts/account/rules/lists/list/items", {"items": [{"id": "stale"}]}),
        ])
        report = json.loads(output.getvalue())
        self.assertEqual(report["desired_count"], 4)
        self.assertEqual(report["uptimerobot_count"], 2)
        self.assertEqual(report["hetrixtools_count"], 3)
        self.assertEqual(report["added_count"], 1)
        self.assertEqual(report["removed_count"], 1)

    def test_bad_feed_aborts_before_cloudflare_access(self):
        for failed_url in (sync.UPTIMEROBOT_TEXT_URL, sync.HETRIXTOOLS_TEXT_URL):
            for bad_body in ("", "192.0.2.1\ninvalid", OSError("source unavailable")):
                def text(method, url, **kwargs):
                    if url == failed_url:
                        if isinstance(bad_body, Exception):
                            raise bad_body
                        return bad_body
                    return "192.0.2.1"

                with self.subTest(url=failed_url, body=bad_body), patch.object(
                    sync, "http_json", return_value={"prefixes": []}
                ), patch.object(sync, "http_text", side_effect=text), patch.object(
                    sync, "cloudflare_api"
                ) as cloudflare, redirect_stderr(io.StringIO()):
                    with self.assertRaises((RuntimeError, OSError)):
                        sync.main()
                    cloudflare.assert_not_called()

    def test_matching_union_does_not_write(self):
        with patch.object(sync, "fetch_uptimerobot_ips", return_value=["192.0.2.1/32"]), \
                patch.object(sync, "fetch_hetrixtools_ips", return_value=["192.0.2.2/32"]), \
                patch.object(sync, "cloudflare_api", side_effect=[
                    {"result": [{"id": "list", "name": "uptimerobot_ips", "kind": "ip"}]},
                    {"result": [{"id": "1", "ip": "192.0.2.1"}, {"id": "2", "ip": "192.0.2.2"}]},
                ]) as cloudflare, redirect_stdout(io.StringIO()):
            sync.main()
            self.assertTrue(all(call.args[1] == "GET" for call in cloudflare.call_args_list))

    def test_failed_add_does_not_delete_existing_ips(self):
        with patch.object(sync, "fetch_uptimerobot_ips", return_value=["192.0.2.1/32"]), \
                patch.object(sync, "fetch_hetrixtools_ips", return_value=["192.0.2.2/32"]), \
                patch.object(sync, "get_or_create_ip_list", return_value={"id": "list"}), \
                patch.object(sync, "get_all_list_items", return_value=[{"id": "old", "ip": "192.0.2.99"}]), \
                patch.object(sync, "add_list_items", side_effect=RuntimeError("add failed")), \
                patch.object(sync, "delete_list_items") as delete:
            with self.assertRaisesRegex(RuntimeError, "add failed"):
                sync.main()
            delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
