"""Salesforce CLI auth and REST helpers."""

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from .actions import DEFAULT_API_VERSION, DEFAULT_TARGET_ORG
from .config import is_org_allowed, org_allowlist


class SalesforceError(Exception):
    pass


def _clean_version(api_version):
    version = str(api_version or DEFAULT_API_VERSION).strip()
    return version[1:] if version.startswith("v") else version


def resolve_target_org(target_org=None):
    is_default = not target_org
    org = target_org or os.environ.get("SALESFORCE_TARGET_ORG") or os.environ.get("SF_TARGET_ORG") or DEFAULT_TARGET_ORG
    if not org:
        raise SalesforceError(
            "No Salesforce target org specified. Pass target_org or set SALESFORCE_TARGET_ORG/SF_TARGET_ORG."
        )
    allowlist = org_allowlist()
    if allowlist and not is_org_allowed(org, allowlist, is_default=is_default):
        raise SalesforceError(
            "Target org '%s' is not in the allowlist (%s). Use --orgs / REVENUECLOUD_ORGS to permit it (DEFAULT_TARGET_ORG / ALLOW_ALL_ORGS tokens supported)." % (org, ", ".join(allowlist))
        )
    return org


def get_connection(target_org=None):
    env_token = os.environ.get("SF_ACCESS_TOKEN") or os.environ.get("SALESFORCE_ACCESS_TOKEN")
    env_instance = os.environ.get("SF_INSTANCE_URL") or os.environ.get("SALESFORCE_INSTANCE_URL")
    if env_token and env_instance:
        org = (
            target_org
            or os.environ.get("SALESFORCE_TARGET_ORG")
            or os.environ.get("SF_TARGET_ORG")
            or os.environ.get("SF_USERNAME")
            or "env-token"
        )
        return {
            "targetOrg": org,
            "username": os.environ.get("SF_USERNAME", org),
            "instanceUrl": env_instance.rstrip("/"),
            "accessToken": env_token,
        }

    org = resolve_target_org(target_org)
    cli_env = dict(os.environ, NO_COLOR="1", FORCE_COLOR="0", SF_NO_COLOR="1")
    cmd = ["sf", "org", "display", "--target-org", org, "--json"]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=cli_env)
    if proc.returncode != 0:
        raise SalesforceError("Salesforce CLI auth failed for %s: %s%s" % (org, proc.stderr, proc.stdout))
    try:
        result = json.loads(proc.stdout)["result"]
    except Exception as exc:
        raise SalesforceError("Unable to parse Salesforce CLI org display output: %s" % exc)

    access_token = result.get("accessToken") or ""
    if not access_token or access_token.startswith("[REDACTED"):
        token_cmd = ["sf", "org", "auth", "show-access-token", "--target-org", org, "--json"]
        token_proc = subprocess.run(token_cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=cli_env)
        if token_proc.returncode != 0:
            raise SalesforceError(
                "Salesforce CLI access-token retrieval failed for %s: %s%s" % (org, token_proc.stderr, token_proc.stdout)
            )
        try:
            access_token = json.loads(token_proc.stdout)["result"]["accessToken"]
        except Exception as exc:
            raise SalesforceError("Unable to parse Salesforce CLI access-token output: %s" % exc)

    return {
        "targetOrg": org,
        "username": result.get("username"),
        "instanceUrl": result["instanceUrl"].rstrip("/"),
        "accessToken": access_token,
        "apiVersion": result.get("apiVersion"),
        "connectedStatus": result.get("connectedStatus"),
        "alias": result.get("alias"),
    }


def rest_request(method, path, body=None, query=None, target_org=None, api_version=None):
    conn = get_connection(target_org)
    version = _clean_version(api_version or conn.get("apiVersion") or DEFAULT_API_VERSION)
    if not path.startswith("/"):
        path = "/services/data/v%s/%s" % (version, path)
    if not path.startswith("/services/data/"):
        raise SalesforceError("Only Salesforce /services/data REST paths are allowed.")

    url = conn["instanceUrl"] + path
    if query:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)

    data = None
    headers = {
        "Authorization": "Bearer " + conn["accessToken"],
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return {
                "ok": 200 <= resp.status < 300,
                "status": resp.status,
                "url": path,
                "targetOrg": conn["targetOrg"],
                "username": conn.get("username"),
                "body": json.loads(raw) if raw else None,
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw
        return {
            "ok": False,
            "status": exc.code,
            "url": path,
            "targetOrg": conn["targetOrg"],
            "username": conn.get("username"),
            "body": parsed,
        }
    except urllib.error.URLError as exc:
        raise SalesforceError("Salesforce REST request failed: %s" % exc)


def action_path(action_name, api_version=None):
    if api_version:
        return "/services/data/v%s/actions/standard/%s" % (_clean_version(api_version), action_name)
    return "actions/standard/%s" % action_name


def describe_action(action_name, target_org=None, api_version=None):
    return rest_request("GET", action_path(action_name, api_version), target_org=target_org, api_version=api_version)


def invoke_action(action_name, inputs, target_org=None, api_version=None):
    return rest_request(
        "POST",
        action_path(action_name, api_version),
        body={"inputs": inputs},
        target_org=target_org,
        api_version=api_version,
    )


def query_soql(query, target_org=None, api_version=None, tooling=False):
    endpoint = "tooling/query" if tooling else "query"
    return rest_request("GET", endpoint, query={"q": query}, target_org=target_org, api_version=api_version)
