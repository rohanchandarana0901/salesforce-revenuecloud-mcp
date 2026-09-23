"""Minimal stdio MCP server for Salesforce Revenue Cloud."""

import json
import sys
import traceback

from .actions import ACTION_REGISTRY, CONTROL_FIELDS, TARGET_ORG_DESCRIPTION, action_input_schema, action_metadata, action_names
from .config import load_config, resolve_enabled_tools, set_runtime_config
from .salesforce import SalesforceError, describe_action, invoke_action, query_soql, rest_request


SERVER_NAME = "revenuecloud-mcp"
SERVER_VERSION = "0.1.0"


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def text_content(value):
    if isinstance(value, str):
        text = value
    else:
        text = json_text(value)
    return [{"type": "text", "text": text}]


def tool(name, description, input_schema):
    return {"name": name, "description": description, "inputSchema": input_schema}


def base_salesforce_schema(extra):
    props = {
        "target_org": {
            "type": "string",
            "description": TARGET_ORG_DESCRIPTION,
        },
        "api_version": {"type": "string", "description": "Salesforce API version without leading v, or latest (default)."},
    }
    props.update(extra)
    return {"type": "object", "properties": props, "additionalProperties": False}


def tools_list(config=None):
    config = config or {}
    all_tools = _all_tools()
    action_domains = {n: action_metadata(n).get("domain") for n in action_names()}
    enabled = resolve_enabled_tools(
        config,
        all_tool_names=[t["name"] for t in all_tools],
        action_tool_names=action_names(),
        action_domains=action_domains,
    )
    return [t for t in all_tools if t["name"] in enabled]


def _all_tools():
    tools = [
        tool(
            "server_info",
            "Show server defaults and implementation notes.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
        tool(
            "list_revenue_cloud_actions",
            "List Revenue Cloud actions implemented by this MCP server.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
        tool(
            "describe_revenue_cloud_action",
            "Describe a Revenue Cloud action from the local registry and optionally Salesforce.",
            base_salesforce_schema(
                {
                    "action_name": {"type": "string", "description": "MCP action name or Salesforce action API name."},
                    "live": {
                        "type": "boolean",
                        "description": "When true, call GET /actions/standard/{action} in Salesforce.",
                    },
                }
            ),
        ),
        tool(
            "invoke_revenue_cloud_action",
            "Invoke any Salesforce standard action with a raw inputs array.",
            base_salesforce_schema(
                {
                    "action_name": {"type": "string"},
                    "input": {"type": "object", "description": "Single action input object."},
                    "inputs": {"type": "array", "items": {"type": "object"}, "description": "Raw Salesforce inputs array."},
                    "validate_only": {
                        "type": "boolean",
                        "description": "When true, describe the action instead of executing POST.",
                    },
                }
            ),
        ),
        tool(
            "validate_revenue_cloud_actions",
            "GET-describe every registered action endpoint against Salesforce without mutating data.",
            base_salesforce_schema({}),
        ),
        tool(
            "list_available_standard_actions",
            "List standard invocable actions exposed by the target Salesforce org.",
            base_salesforce_schema(
                {
                    "filter": {
                        "type": "string",
                        "description": "Optional case-insensitive substring filter applied to action name, label, or type.",
                    }
                }
            ),
        ),
        tool(
            "revenue_cloud_org_readiness",
            "Diagnose Revenue Cloud MCP readiness for the target org without mutating data.",
            base_salesforce_schema({}),
        ),
        tool(
            "salesforce_rest_request",
            "Call a Salesforce /services/data REST API path for APIs in the Revenue Cloud guide.",
            base_salesforce_schema(
                {
                    "method": {"type": "string", "enum": ["GET", "POST", "PATCH", "DELETE"]},
                    "path": {"type": "string", "description": "Relative path such as query or /services/data/latest/query."},
                    "body": {"type": "object"},
                    "query": {"type": "object", "additionalProperties": True},
                }
            ),
        ),
        tool(
            "soql_query",
            "Run a SOQL query through Salesforce REST API.",
            base_salesforce_schema(
                {
                    "query": {"type": "string", "description": "SOQL query."},
                    "tooling": {"type": "boolean", "description": "Use Tooling API query endpoint."},
                }
            ),
        ),
        tool(
            "hydrate_pricing_context",
            "Hydrate a Revenue Cloud pricing context via /connect/core-pricing/pricing. The endpoint requires four ID-based parameters (NOT names): contextDefinitionId, contextMappingId, pricingProcedureId, and jsonDataString. Returns the hydrated contextInstanceId usable by runSalesforcePricing.",
            base_salesforce_schema(
                {
                    "context_definition_id": {"type": "string", "description": "ContextDefinition Id (not name). Query: SELECT Id, DeveloperName FROM ContextDefinition."},
                    "context_mapping_id": {"type": "string", "description": "ContextMapping Id (not name). Query: SELECT Id FROM ContextMapping WHERE ContextDefinitionId = '...'."},
                    "pricing_procedure_id": {"type": "string", "description": "PricingProcedure Id (not name). Query: SELECT Id FROM PricingProcedure."},
                    "json_data_string": {"type": "string", "description": "Pricing payload as a JSON string. Typical shape: '{\"recordId\":\"<Quote/Order Id>\"}'."},
                    "additional_body": {"type": "object", "description": "Extra top-level fields merged into the POST body verbatim (advanced)."},
                }
            ),
        ),
        tool(
            "place_sales_transaction",
            "Place a Revenue Cloud sales transaction via /connect/rev/sales-transaction/actions/place. The endpoint accepts ONLY a contextDetails object at the top level. Requires a hydrated contextId obtained from hydrate_pricing_context (or from any prior call that returned one).",
            base_salesforce_schema(
                {
                    "context_id": {"type": "string", "description": "Hydrated context Id. Required."},
                    "additional_context_details": {"type": "object", "description": "Extra fields merged into contextDetails (advanced)."},
                    "body": {"type": "object", "description": "Escape hatch: full request body. If provided, overrides the structured fields above."},
                }
            ),
        ),
        tool(
            "assign_revenue_cloud_usage",
            "Mark a record (typically an Order) as Revenue Lifecycle Management by inserting an AppUsageAssignment.",
            base_salesforce_schema(
                {
                    "record_id": {"type": "string", "description": "Id of the record to mark."},
                    "app_usage_type": {
                        "type": "string",
                        "description": "AppUsageType value. Defaults to 'RevenueLifecycleManagement'.",
                    },
                }
            ),
        ),
        tool(
            "cpq_configure",
            "Start a configurator session via /connect/cpq/configurator/actions/configure. Requires a transaction or transaction-line context (transactionId / transactionLineId / recordId).",
            base_salesforce_schema(
                {
                    "transaction_id": {"type": "string", "description": "Sales transaction Id (Quote, Order, or RC SalesTransaction)."},
                    "transaction_line_id": {"type": "string", "description": "Specific transaction line Id when configuring a single line."},
                    "record_id": {"type": "string", "description": "Generic record Id alternative when transaction_id/transaction_line_id are not applicable."},
                    "correlation_id": {"type": "string", "description": "Optional correlation id for tracing."},
                    "body": {"type": "object", "description": "Escape hatch: full request body. If provided, overrides the structured fields above."},
                }
            ),
        ),
        tool(
            "cpq_load_instance",
            "Load a configurator instance via /connect/cpq/configurator/actions/load-instance. Same context fields as cpq_configure.",
            base_salesforce_schema(
                {
                    "transaction_id": {"type": "string"},
                    "transaction_line_id": {"type": "string"},
                    "record_id": {"type": "string"},
                    "correlation_id": {"type": "string"},
                    "body": {"type": "object", "description": "Escape hatch full body."},
                }
            ),
        ),
        tool(
            "cpq_save_instance",
            "Save the current configurator instance via /connect/cpq/configurator/actions/save-instance.",
            base_salesforce_schema(
                {
                    "transaction_id": {"type": "string"},
                    "transaction_line_id": {"type": "string"},
                    "record_id": {"type": "string"},
                    "correlation_id": {"type": "string"},
                    "body": {"type": "object", "description": "Escape hatch full body."},
                }
            ),
        ),
        tool(
            "cpq_set_product_quantity",
            "Set product quantity on the configurator instance via /connect/cpq/configurator/actions/set-product-quantity.",
            base_salesforce_schema(
                {
                    "transaction_id": {"type": "string"},
                    "transaction_line_id": {"type": "string"},
                    "record_id": {"type": "string"},
                    "correlation_id": {"type": "string"},
                    "body": {"type": "object", "description": "Escape hatch full body. Pass the full payload (e.g. with productId, quantity, attributes) here."},
                }
            ),
        ),
        tool(
            "cpq_add_nodes",
            "Add nodes (line items) on the configurator instance via /connect/cpq/configurator/actions/add-nodes.",
            base_salesforce_schema(
                {
                    "transaction_id": {"type": "string"},
                    "transaction_line_id": {"type": "string"},
                    "record_id": {"type": "string"},
                    "correlation_id": {"type": "string"},
                    "body": {"type": "object", "description": "Escape hatch full body with the nodes payload."},
                }
            ),
        ),
        tool(
            "cpq_update_nodes",
            "Update nodes on the configurator instance via /connect/cpq/configurator/actions/update-nodes.",
            base_salesforce_schema(
                {
                    "transaction_id": {"type": "string"},
                    "transaction_line_id": {"type": "string"},
                    "record_id": {"type": "string"},
                    "correlation_id": {"type": "string"},
                    "body": {"type": "object", "description": "Escape hatch full body with the updated nodes payload."},
                }
            ),
        ),
        tool(
            "cpq_delete_nodes",
            "Delete nodes from the configurator instance via /connect/cpq/configurator/actions/delete-nodes.",
            base_salesforce_schema(
                {
                    "transaction_id": {"type": "string"},
                    "transaction_line_id": {"type": "string"},
                    "record_id": {"type": "string"},
                    "correlation_id": {"type": "string"},
                    "body": {"type": "object", "description": "Escape hatch full body with the node ids to delete."},
                }
            ),
        ),
    ]
    for name in action_names():
        meta = action_metadata(name)
        tools.append(tool(name, meta["description"], action_input_schema(name)))
    return tools


CPQ_CONFIGURATOR_ENDPOINTS = {
    "cpq_configure": "configure",
    "cpq_load_instance": "load-instance",
    "cpq_save_instance": "save-instance",
    "cpq_set_product_quantity": "set-product-quantity",
    "cpq_add_nodes": "add-nodes",
    "cpq_update_nodes": "update-nodes",
    "cpq_delete_nodes": "delete-nodes",
}


def normalize_action_inputs(args, action_api_name=None):
    if args.get("inputs") is not None:
        if not isinstance(args["inputs"], list):
            raise ValueError("inputs must be an array of objects")
        items = args["inputs"]
    elif args.get("input") is not None:
        if not isinstance(args["input"], dict):
            raise ValueError("input must be an object")
        items = [args["input"]]
    else:
        items = [{k: v for k, v in args.items() if k not in CONTROL_FIELDS}]
    return [_apply_input_aliases(item, action_api_name) for item in items]


def _apply_input_aliases(item, action_api_name):
    if not isinstance(item, dict):
        return item
    if action_api_name == "getMultipleProductDetails":
        if "productDataInputs" in item and "productData" not in item:
            item = dict(item)
            item["productData"] = item.pop("productDataInputs")
    return item


def call_action_tool(name, args):
    meta = action_metadata(name)
    api_name = meta["api_name"]
    target_org = args.get("target_org")
    api_version = args.get("api_version")
    if args.get("validate_only"):
        return describe_action(api_name, target_org=target_org, api_version=api_version)
    return invoke_action(api_name, normalize_action_inputs(args, api_name), target_org=target_org, api_version=api_version)


def call_tool(name, args, config=None):
    args = args or {}
    config = config or {}
    enabled_names = {
        t["name"]
        for t in tools_list(config)
    }
    if enabled_names and name not in enabled_names:
        raise KeyError("Tool '%s' is not enabled by current toolset/tools filter." % name)
    if name == "server_info":
        return {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
            "defaults": {
                "target_org": "SALESFORCE_TARGET_ORG or SF_TARGET_ORG",
                "api_version": "latest (pass e.g. 66.0 to pin)",
            },
            "filter": {
                "toolsets": config.get("toolsets") or [],
                "tools": config.get("tools") or [],
                "orgs_allowlist": config.get("orgs") or [],
            },
            "sources": ["image (2).png", "revenue_lifecycle_management_dev_guide.pdf"],
            "notes": [
                "Specific action tools accept direct fields, input, or inputs.",
                "Use validate_only=true for a non-mutating endpoint check.",
                "salesforce_rest_request covers additional Revenue Cloud REST APIs from the guide.",
            ],
        }
    if name == "list_revenue_cloud_actions":
        return [{"tool": n, **action_metadata(n)} for n in action_names()]
    if name == "describe_revenue_cloud_action":
        action_name = args["action_name"]
        meta = action_metadata(action_name) if action_name in ACTION_REGISTRY else {"api_name": action_name}
        result = {"registry": meta}
        if args.get("live"):
            result["salesforce"] = describe_action(
                meta["api_name"],
                target_org=args.get("target_org"),
                api_version=args.get("api_version"),
            )
        return result
    if name == "invoke_revenue_cloud_action":
        action_name = args["action_name"]
        meta = action_metadata(action_name) if action_name in ACTION_REGISTRY else {"api_name": action_name}
        if args.get("validate_only"):
            return describe_action(meta["api_name"], target_org=args.get("target_org"), api_version=args.get("api_version"))
        return invoke_action(
            meta["api_name"],
            normalize_action_inputs(args, meta["api_name"]),
            target_org=args.get("target_org"),
            api_version=args.get("api_version"),
        )
    if name == "validate_revenue_cloud_actions":
        results = []
        for action_name in action_names():
            meta = action_metadata(action_name)
            result = describe_action(meta["api_name"], target_org=args.get("target_org"), api_version=args.get("api_version"))
            results.append(
                {
                    "tool": action_name,
                    "api_name": meta["api_name"],
                    "ok": result.get("ok"),
                    "status": result.get("status"),
                    "body": result.get("body"),
                }
            )
        return results
    if name == "list_available_standard_actions":
        response = rest_request(
            "GET",
            "actions/standard",
            target_org=args.get("target_org"),
            api_version=args.get("api_version"),
        )
        if not response.get("ok"):
            return response
        actions = (response.get("body") or {}).get("actions") or []
        text_filter = (args.get("filter") or "").lower()
        if text_filter:
            actions = [
                action
                for action in actions
                if text_filter in str(action.get("name", "")).lower()
                or text_filter in str(action.get("label", "")).lower()
                or text_filter in str(action.get("type", "")).lower()
            ]
        return {
            "ok": True,
            "status": response.get("status"),
            "targetOrg": response.get("targetOrg"),
            "username": response.get("username"),
            "count": len(actions),
            "actions": actions,
        }
    if name == "revenue_cloud_org_readiness":
        standard_actions = rest_request(
            "GET",
            "actions/standard",
            target_org=args.get("target_org"),
            api_version=args.get("api_version"),
        )
        if standard_actions.get("ok"):
            available_actions = {
                action.get("name"): action
                for action in (standard_actions.get("body") or {}).get("actions", [])
                if action.get("name")
            }
        else:
            available_actions = {}

        username = (standard_actions.get("username") or "").replace("'", "\\'")
        user_query = "SELECT Id, Username, Profile.Name FROM User WHERE Username = '%s' LIMIT 1" % username
        assigned_permsets_query = (
            "SELECT PermissionSet.Name, PermissionSet.Label FROM PermissionSetAssignment "
            "WHERE Assignee.Username = '%s' AND "
            "(PermissionSet.Name LIKE '%%Revenue%%' OR PermissionSet.Name LIKE '%%Usage%%' OR "
            "PermissionSet.Name LIKE '%%Pricing%%' OR PermissionSet.Name LIKE '%%Product%%' OR "
            "PermissionSet.Name LIKE '%%Configurator%%' OR PermissionSet.Name LIKE '%%Rating%%' OR "
            "PermissionSet.Name LIKE '%%Transaction%%') LIMIT 200"
        ) % username
        psl_query = (
            "SELECT PermissionSetLicense.DeveloperName, PermissionSetLicense.MasterLabel "
            "FROM PermissionSetLicenseAssign WHERE Assignee.Username = '%s' AND "
            "(PermissionSetLicense.DeveloperName LIKE '%%Revenue%%' OR PermissionSetLicense.DeveloperName LIKE '%%Usage%%' OR "
            "PermissionSetLicense.DeveloperName LIKE '%%Pricing%%' OR PermissionSetLicense.DeveloperName LIKE '%%Product%%' OR "
            "PermissionSetLicense.DeveloperName LIKE '%%Configurator%%' OR PermissionSetLicense.DeveloperName LIKE '%%Rating%%' OR "
            "PermissionSetLicense.DeveloperName LIKE '%%DynamicRevenue%%') LIMIT 200"
        ) % username

        action_results = []
        for action_name in action_names():
            meta = action_metadata(action_name)
            available = meta["api_name"] in available_actions
            action_results.append(
                {
                    "tool": action_name,
                    "api_name": meta["api_name"],
                    "domain": meta.get("domain"),
                    "since": meta.get("since"),
                    "available_in_org": available,
                    "org_action": available_actions.get(meta["api_name"]),
                }
            )

        # Segregate v67-only registry entries that require Salesforce Billing
        # license and/or API v67.0+. These are expected to be missing from
        # orgs running v66 or without the relevant permission sets.
        v67_missing = [
            item for item in action_results
            if not item["available_in_org"] and (item.get("since") or "") >= "67.0"
        ]
        truly_missing = [
            item for item in action_results
            if not item["available_in_org"] and (item.get("since") or "") < "67.0"
        ]

        return {
            "ok": standard_actions.get("ok"),
            "status": standard_actions.get("status"),
            "targetOrg": standard_actions.get("targetOrg"),
            "username": standard_actions.get("username"),
            "apiVersion": args.get("api_version") or "latest",
            "summary": {
                "registered_action_tools": len(action_results),
                "available_action_tools": len([item for item in action_results if item["available_in_org"]]),
                "missing_or_disabled_action_tools": len([item for item in action_results if not item["available_in_org"]]),
                "missing_due_to_v67_or_billing_license": len(v67_missing),
                "missing_other_reasons": len(truly_missing),
            },
            "missing_v67_or_billing_actions": [item["tool"] for item in v67_missing],
            "missing_other_actions": [item["tool"] for item in truly_missing],
            "actions": action_results,
            "user": query_soql(user_query, target_org=args.get("target_org"), api_version=args.get("api_version")),
            "assigned_revenue_cloud_permission_sets": query_soql(
                assigned_permsets_query,
                target_org=args.get("target_org"),
                api_version=args.get("api_version"),
            ),
            "assigned_revenue_cloud_permission_set_licenses": query_soql(
                psl_query,
                target_org=args.get("target_org"),
                api_version=args.get("api_version"),
            ),
        }
    if name == "salesforce_rest_request":
        return rest_request(
            args.get("method", "GET"),
            args["path"],
            body=args.get("body"),
            query=args.get("query"),
            target_org=args.get("target_org"),
            api_version=args.get("api_version"),
        )
    if name == "soql_query":
        return query_soql(
            args["query"],
            target_org=args.get("target_org"),
            api_version=args.get("api_version"),
            tooling=args.get("tooling", False),
        )
    if name == "hydrate_pricing_context":
        body = dict(args.get("additional_body") or {})
        if args.get("context_definition_id"):
            body.setdefault("contextDefinitionId", args["context_definition_id"])
        if args.get("context_mapping_id"):
            body.setdefault("contextMappingId", args["context_mapping_id"])
        if args.get("pricing_procedure_id"):
            body.setdefault("pricingProcedureId", args["pricing_procedure_id"])
        if args.get("json_data_string") is not None:
            body.setdefault("jsonDataString", args["json_data_string"])
        return rest_request(
            "POST",
            "connect/core-pricing/pricing",
            body=body,
            target_org=args.get("target_org"),
            api_version=args.get("api_version"),
        )
    if name == "place_sales_transaction":
        explicit_body = args.get("body")
        if explicit_body:
            body = explicit_body
        else:
            ctx = dict(args.get("additional_context_details") or {})
            if args.get("context_id"):
                ctx.setdefault("contextId", args["context_id"])
            body = {"contextDetails": ctx}
        return rest_request(
            "POST",
            "connect/rev/sales-transaction/actions/place",
            body=body,
            target_org=args.get("target_org"),
            api_version=args.get("api_version"),
        )
    if name == "assign_revenue_cloud_usage":
        return rest_request(
            "POST",
            "sobjects/AppUsageAssignment",
            body={
                "RecordId": args["record_id"],
                "AppUsageType": args.get("app_usage_type") or "RevenueLifecycleManagement",
            },
            target_org=args.get("target_org"),
            api_version=args.get("api_version"),
        )
    if name in CPQ_CONFIGURATOR_ENDPOINTS:
        explicit_body = args.get("body")
        if explicit_body:
            body = explicit_body
        else:
            body = {}
            if args.get("transaction_id"):
                body["transactionId"] = args["transaction_id"]
            if args.get("transaction_line_id"):
                body["transactionLineId"] = args["transaction_line_id"]
            if args.get("record_id"):
                body["recordId"] = args["record_id"]
            if args.get("correlation_id"):
                body["correlationId"] = args["correlation_id"]
        return rest_request(
            "POST",
            "connect/cpq/configurator/actions/" + CPQ_CONFIGURATOR_ENDPOINTS[name],
            body=body,
            target_org=args.get("target_org"),
            api_version=args.get("api_version"),
        )
    if name in ACTION_REGISTRY:
        return call_action_tool(name, args)
    raise KeyError("Unknown tool: %s" % name)


def handle(request, config=None):
    method = request.get("method")
    req_id = request.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools_list(config)}}
    if method == "tools/call":
        params = request.get("params") or {}
        result = call_tool(params["name"], params.get("arguments") or {}, config=config)
        return {"jsonrpc": "2.0", "id": req_id, "result": {"content": text_content(result)}}
    if method and method.startswith("notifications/"):
        return None
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}}


def error_response(req_id, exc):
    message = str(exc)
    data = None
    if not isinstance(exc, (KeyError, ValueError, SalesforceError)):
        data = traceback.format_exc()
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": message, "data": data}}


def read_requests(raw):
    buffer = b""
    while True:
        chunk = raw.read1(4096) if hasattr(raw, "read1") else raw.read(4096)
        if not chunk:
            break
        buffer += chunk
        while buffer:
            stripped = buffer.lstrip()
            leading_ws = len(buffer) - len(stripped)
            if stripped.startswith(b"Content-Length:") or b"\r\n\r\n" in stripped:
                if leading_ws:
                    buffer = stripped
                header_end = buffer.find(b"\r\n\r\n")
                if header_end == -1:
                    break
                header = buffer[:header_end].decode("ascii", "replace")
                content_length = None
                for line in header.split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        content_length = int(line.split(":", 1)[1].strip())
                        break
                if content_length is None:
                    raise ValueError("Missing Content-Length header")
                body_start = header_end + 4
                body_end = body_start + content_length
                if len(buffer) < body_end:
                    break
                payload = buffer[body_start:body_end]
                buffer = buffer[body_end:]
                yield json.loads(payload.decode("utf-8")), "framed"
                continue

            newline = buffer.find(b"\n")
            if newline == -1:
                break
            line = buffer[:newline].strip()
            buffer = buffer[newline + 1 :]
            if line:
                yield json.loads(line.decode("utf-8")), "line"


def write_response(response, mode):
    payload = json.dumps(response, ensure_ascii=False).encode("utf-8")
    if mode == "framed":
        sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(payload))
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        return
    sys.stdout.write(payload.decode("utf-8") + "\n")
    sys.stdout.flush()


def main(argv=None):
    config = load_config(argv if argv is not None else sys.argv[1:])
    set_runtime_config(config)
    for request, mode in read_requests(sys.stdin.buffer):
        req_id = None
        try:
            req_id = request.get("id")
            response = handle(request, config=config)
        except Exception as exc:
            response = error_response(req_id, exc)
        if response is not None:
            write_response(response, mode)


if __name__ == "__main__":
    main()
