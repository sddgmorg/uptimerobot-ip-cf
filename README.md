# uptimerobot-ip-cf

Simple GitHub Actions job that keeps a Cloudflare Account IP List synced with UptimeRobot checker IPs.

No `npm`, no local install, and no server needed.

It uses UptimeRobot's JSON metadata endpoint first:

```txt
https://api.uptimerobot.com/meta/ips
```

If that fails, it falls back to:

```txt
https://cdn.uptimerobot.com/api/IPv4andIPv6.txt
```

## What It Does

- Creates or finds a Cloudflare account IP list named `uptimerobot_ips`.
- Adds new UptimeRobot IPv4 and IPv6 addresses.
- Removes stale addresses that are no longer published by UptimeRobot.
- Runs every 6 hours using GitHub Actions.
- Can also be run manually from the GitHub Actions page.

Use this list from a Cloudflare WAF custom rule instead of hardcoding IPs into the rule expression.

## Cloudflare API Token

Create a Cloudflare API token with this permission:

```txt
Account Rules Lists: Edit
```

Scope it to only the Cloudflare account that owns the IP list.

## Setup

Push this repo to GitHub.

In your GitHub repo, go to:

```
Settings -> Secrets and variables -> Actions -> New repository secret
```

Add these two secrets:

```txt
CF_ACCOUNT_ID
CF_API_TOKEN
```

Then go to:

```txt
Actions -> Sync UptimeRobot IPs to Cloudflare -> Run workflow
```

That runs it immediately. After that, GitHub will run it automatically every 6 hours.

## Where To Find Cloudflare Account ID

Cloudflare dashboard:

```
Right sidebar -> Account ID
```

Make sure this is the account ID, not the zone ID.

## GitHub Workflow

The scheduled workflow is here:

```txt
.github/workflows/sync-uptimerobot-ips.yml
```

It runs this Python script:

```txt
scripts/sync_uptimerobot_ips.py
```

The script uses only Python's standard library, so there are no packages to install.

## WAF Rule

After the first successful run, create a Cloudflare WAF custom rule that references the account list:

```txt
ip.src in $uptimerobot_ips
```

Safer scoped example:

```txt
ip.src in $uptimerobot_ips
and http.host eq "example.com"
and http.request.uri.path in {"/" "/health" "/status"}
```

Recommended action: use `Skip` for the protections that interfere with monitoring, rather than globally allowing all traffic.

Typical skip targets:

```txt
Managed WAF
Browser Integrity Check
Rate Limiting
```

## Notes

- The first run will create the `uptimerobot_ips` Cloudflare list automatically.
- Use a dedicated IP list for this automation.
- Do not add your own manual IPs into this same list because the script removes stale entries.

## Optional Configuration

The workflow sets this default list name:

```txt
CF_LIST_NAME=uptimerobot_ips
```

If you already have a Cloudflare IP list and want to use its ID directly, add another GitHub repository secret:

```txt
CF_LIST_ID
```
