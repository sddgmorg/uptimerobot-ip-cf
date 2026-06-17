import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
UPTIMEROBOT_META_URL = "https://api.uptimerobot.com/meta/ips"
UPTIMEROBOT_TEXT_URL = "https://cdn.uptimerobot.com/api/IPv4andIPv6.txt"
DEFAULT_LIST_NAME = "uptimerobot_ips"
MANAGED_COMMENT = "Managed by uptimerobot-ip-cf"
BULK_OPERATION_TIMEOUT_SECONDS = 90


def main():
    account_id = required_env("CF_ACCOUNT_ID")
    api_token = required_env("CF_API_TOKEN")
    list_name = os.getenv("CF_LIST_NAME", DEFAULT_LIST_NAME)
    list_id = os.getenv("CF_LIST_ID")

    desired_ips = fetch_uptimerobot_ips()
    target_list = get_or_create_ip_list(account_id, api_token, list_name, list_id)
    current_items = get_all_list_items(account_id, api_token, target_list["id"])

    current_ips = {normalize_prefix(item.get("ip")) for item in current_items}
    current_ips.discard(None)
    desired_ip_set = set(desired_ips)

    ips_to_add = [ip for ip in desired_ips if ip not in current_ips]
    items_to_remove = [item for item in current_items if normalize_prefix(item.get("ip")) not in desired_ip_set]

    add_list_items(account_id, api_token, target_list["id"], ips_to_add)
    delete_list_items(account_id, api_token, target_list["id"], items_to_remove)

    print(
        json.dumps(
            {
                "ok": True,
                "list_name": target_list["name"],
                "list_id": target_list["id"],
                "desired_count": len(desired_ips),
                "existing_count": len(current_items),
                "added_count": len(ips_to_add),
                "removed_count": len(items_to_remove),
            },
            indent=2,
        )
    )


def required_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def fetch_uptimerobot_ips():
    try:
        return fetch_uptimerobot_meta_ips()
    except Exception as error:
        print(f"Meta IP source failed, using text fallback: {error}", file=sys.stderr)
        return fetch_uptimerobot_text_ips()


def fetch_uptimerobot_meta_ips():
    body = http_json("GET", UPTIMEROBOT_META_URL, headers={"Accept": "application/json"})
    prefixes = body.get("prefixes")
    if not isinstance(prefixes, list):
        raise RuntimeError("UptimeRobot meta response did not contain prefixes")

    ips = []
    for prefix in prefixes:
        ips.append(normalize_prefix(prefix.get("ip_prefix")))
        ips.append(normalize_prefix(prefix.get("ipv6_prefix")))

    return sort_and_dedupe(ip for ip in ips if ip)


def fetch_uptimerobot_text_ips():
    text = http_text("GET", UPTIMEROBOT_TEXT_URL, headers={"Accept": "text/plain"})
    ips = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            ips.append(normalize_prefix(line))

    result = sort_and_dedupe(ip for ip in ips if ip)
    if not result:
        raise RuntimeError("UptimeRobot text response did not contain any valid IPs")
    return result


def get_or_create_ip_list(account_id, api_token, list_name, list_id=None):
    if list_id:
        return {"id": list_id, "name": list_name}

    response = cloudflare_api(api_token, "GET", f"/accounts/{account_id}/rules/lists")
    for item in response["result"]:
        if item["name"] == list_name:
            if item["kind"] != "ip":
                raise RuntimeError(f"Cloudflare list {list_name} exists but is {item['kind']}, expected ip")
            return item

    response = cloudflare_api(
        api_token,
        "POST",
        f"/accounts/{account_id}/rules/lists",
        {
            "kind": "ip",
            "name": list_name,
            "description": "UptimeRobot checker IPs synced by GitHub Actions",
        },
    )
    return response["result"]


def get_all_list_items(account_id, api_token, list_id):
    items = []
    cursor = None
    seen_cursors = set()

    while True:
        path = f"/accounts/{account_id}/rules/lists/{list_id}/items"
        if cursor:
            path = f"{path}?{urllib.parse.urlencode({'cursor': cursor})}"

        response = cloudflare_api(api_token, "GET", path)
        items.extend(response["result"])

        cursor = response.get("result_info", {}).get("cursors", {}).get("after")
        if not cursor:
            return items
        if cursor in seen_cursors:
            raise RuntimeError("Cloudflare returned a repeated list-items cursor")
        seen_cursors.add(cursor)


def add_list_items(account_id, api_token, list_id, ips):
    for chunk in chunks(ips, 1000):
        response = cloudflare_api(
            api_token,
            "POST",
            f"/accounts/{account_id}/rules/lists/{list_id}/items",
            [{"ip": ip, "comment": MANAGED_COMMENT} for ip in chunk],
        )
        wait_for_bulk_operation(account_id, api_token, response["result"]["operation_id"])


def delete_list_items(account_id, api_token, list_id, items):
    for chunk in chunks(items, 1000):
        response = cloudflare_api(
            api_token,
            "DELETE",
            f"/accounts/{account_id}/rules/lists/{list_id}/items",
            {"items": [{"id": item["id"]} for item in chunk]},
        )
        wait_for_bulk_operation(account_id, api_token, response["result"]["operation_id"])


def wait_for_bulk_operation(account_id, api_token, operation_id):
    deadline = time.time() + BULK_OPERATION_TIMEOUT_SECONDS

    while time.time() < deadline:
        response = cloudflare_api(
            api_token,
            "GET",
            f"/accounts/{account_id}/rules/lists/bulk_operations/{operation_id}",
        )
        status = response["result"]["status"]

        if status == "completed":
            return
        if status == "failed":
            raise RuntimeError(f"Cloudflare bulk operation failed: {response['result'].get('error', operation_id)}")

        time.sleep(1)

    raise RuntimeError(f"Timed out waiting for Cloudflare bulk operation {operation_id}")


def cloudflare_api(api_token, method, path, data=None):
    return http_json(
        method,
        f"{CLOUDFLARE_API_BASE}{path}",
        data=data,
        headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
    )


def http_json(method, url, data=None, headers=None):
    text = http_text(method, url, data=data, headers=headers)
    body = json.loads(text)
    if body.get("success") is False:
        messages = "; ".join(error.get("message", str(error)) for error in body.get("errors", []))
        raise RuntimeError(f"API request failed: {messages or text}")
    return body


def http_text(method, url, data=None, headers=None):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code} from {url}: {error_body}") from error


def normalize_prefix(value):
    if not isinstance(value, str) or not value.strip():
        return None

    try:
        return str(ipaddress.ip_network(value.strip(), strict=False))
    except ValueError:
        return None


def sort_and_dedupe(values):
    return sorted(set(values), key=network_sort_key)


def network_sort_key(value):
    network = ipaddress.ip_network(value, strict=False)
    return (network.version, int(network.network_address), network.prefixlen)


def chunks(values, size):
    for index in range(0, len(values), size):
        yield values[index : index + size]


if __name__ == "__main__":
    main()
