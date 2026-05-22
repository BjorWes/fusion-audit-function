"""
OCI Function: Collect Fusion Applications audit logs and publish to OCI Streaming.

Replaces: Compute VM + Management Agent + bash script + Python publisher + systemd timer.

Triggered on schedule via OCI Alarm or Application Integration.
Uses Resource Principal for OCI auth, OCI Vault for Fusion credentials,
and Object Storage for state tracking between invocations.
"""

import io
import json
import base64
import logging
import datetime
from typing import Optional

import fdk.response
from fdk import context as fdk_context

import oci
import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger("fusion-audit-collector")
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Configuration – override via Function config (environment variables)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    # Fusion
    "FUSION_POD_URL": "https://<your-pod>.fa.ocs.oraclecloud.com",
    "FUSION_AUDIT_ENDPOINT": "/fscmRestApi/fndAuditRESTService/audittrail/getaudithistory",
    "FUSION_PAGE_LIMIT": "1000",

    # OCI Vault – secret containing Fusion credentials
    # Secret value must be JSON:
    #   Basic Auth: {"username": "...", "password": "..."}
    #   OAuth:      {"client_id": "...", "client_secret": "...", "token_url": "...", "scope": "..."}
    "VAULT_SECRET_OCID": "ocid1.vaultsecret.oc1...<YOUR_SECRET_OCID>",

    # OCI Streaming
    "STREAM_OCID": "ocid1.stream.oc1...<YOUR_STREAM_OCID>",
    "STREAM_BATCH_SIZE": "100",

    # OCI Object Storage – state bucket & audit config
    "STATE_BUCKET": "fusion-audit-state",
    "STATE_OBJECT": "last_run.json",
    "AUDIT_CONFIG_OBJECT": "audit_config.json",
    "STATE_NAMESPACE": "",  # auto-detected if empty
}


def _cfg(ctx: fdk_context.InvokeContext, key: str) -> str:
    """Read from Function configuration, fall back to defaults."""
    return (ctx.Config().get(key) or DEFAULT_CONFIG.get(key, ""))


# ---------------------------------------------------------------------------
# OCI clients (Resource Principal)
# ---------------------------------------------------------------------------
class OCIClients:
    def __init__(self):
        self.signer = oci.auth.signers.get_resource_principals_signer()
        self.secrets = oci.secrets.SecretsClient(config={}, signer=self.signer)
        self.streaming_admin = oci.streaming.StreamAdminClient(config={}, signer=self.signer)
        self.object_storage = oci.object_storage.ObjectStorageClient(config={}, signer=self.signer)
        self._stream_client: Optional[oci.streaming.StreamClient] = None
        self._stream_endpoint: Optional[str] = None

    def get_stream_client(self, stream_ocid: str) -> oci.streaming.StreamClient:
        if self._stream_client is None:
            stream = self.streaming_admin.get_stream(stream_ocid).data
            self._stream_endpoint = stream.messages_endpoint
            self._stream_client = oci.streaming.StreamClient(
                config={}, signer=self.signer,
                service_endpoint=self._stream_endpoint,
            )
        return self._stream_client


# ---------------------------------------------------------------------------
# Vault – get Fusion credentials
# ---------------------------------------------------------------------------
def get_fusion_credentials(clients: OCIClients, secret_ocid: str) -> dict:
    """Retrieve Fusion credentials from OCI Vault secret.

    Auto-detects format:
      - Basic Auth: {"username": "...", "password": "..."}
      - OAuth:      {"client_id": "...", "client_secret": "...", "token_url": "...", "scope": "..."}

    Returns dict with 'auth_type' key set to 'basic' or 'oauth'.
    """
    bundle = clients.secrets.get_secret_bundle(secret_id=secret_ocid).data
    secret_b64 = bundle.secret_bundle_content.content
    secret_json = base64.b64decode(secret_b64).decode("utf-8")
    creds = json.loads(secret_json)

    if "client_id" in creds:
        creds["auth_type"] = "oauth"
    else:
        creds["auth_type"] = "basic"
    return creds


def get_oauth_token(client_id: str, client_secret: str, token_url: str, scope: str) -> str:
    """Obtain an OAuth2 access token via client_credentials grant."""
    resp = requests.post(
        token_url,
        auth=HTTPBasicAuth(client_id, client_secret),
        data={"grant_type": "client_credentials", "scope": scope},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Token endpoint {resp.status_code}: {resp.text}")
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# State – Object Storage
# ---------------------------------------------------------------------------
def load_state(clients: OCIClients, namespace: str, bucket: str, obj: str) -> dict:
    """Load last-run state from Object Storage. Returns empty dict on first run."""
    try:
        resp = clients.object_storage.get_object(namespace, bucket, obj)
        return json.loads(resp.data.text)
    except oci.exceptions.ServiceError as e:
        if e.status == 404:
            return {}
        raise


def save_state(clients: OCIClients, namespace: str, bucket: str, obj: str, state: dict):
    """Persist state to Object Storage."""
    clients.object_storage.put_object(
        namespace, bucket, obj,
        put_object_body=json.dumps(state).encode("utf-8"),
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# Audit config – Object Storage
# ---------------------------------------------------------------------------
def load_audit_config(clients: OCIClients, namespace: str, bucket: str, obj: str) -> list[dict]:
    """Load audit product/businessObjectType combinations from Object Storage.

    Expected JSON format:
    [
      {"product": "Ledger", "businessObjectType": "oracle.apps.financials...AM"},
      {"product": "customer", "businessObjectType": "oracle.apps.crmCommon...AM"}
    ]
    """
    resp = clients.object_storage.get_object(namespace, bucket, obj)
    return json.loads(resp.data.text)


# ---------------------------------------------------------------------------
# Fusion – collect audit logs
# ---------------------------------------------------------------------------
def collect_fusion_logs(
    pod_url: str,
    audit_endpoint: str,
    audit_targets: list[dict],
    credentials: dict,
    page_limit: int,
    since: Optional[str] = None,
) -> tuple[list[dict], list[dict]]:
    """Call Fusion fndAuditRESTService and return all audit log entries."""
    all_entries = []
    api_responses = []

    if credentials["auth_type"] == "oauth":
        access_token = get_oauth_token(
            credentials["client_id"],
            credentials["client_secret"],
            credentials["token_url"],
            credentials.get("scope", "urn:opc:resource:consumer::all"),
        )
        auth = None
        headers = {"Authorization": f"Bearer {access_token}"}
    else:
        auth = HTTPBasicAuth(credentials["username"], credentials["password"])
        headers = {}
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if since:
        from_date = since[:10]  # normalize to YYYY-MM-DD
    else:
        from_date = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    to_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    url = f"{pod_url}{audit_endpoint}"

    for target in audit_targets:
        product = target["product"]
        bo_type = target.get("businessObjectType", "")
        page_number = 1

        while True:
            payload = {
                "product": product,
                "fromDate": from_date,
                "toDate": to_date,
                "pageSize": str(page_limit),
                "pageNumber": str(page_number),
            }
            if bo_type:
                payload["businessObjectType"] = bo_type

            try:
                resp = requests.post(url, auth=auth, headers=headers, json=payload, timeout=120)
                api_responses.append({
                    "product": product,
                    "businessObjectType": bo_type,
                    "page": page_number,
                    "status_code": resp.status_code,
                })
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                api_responses.append({
                    "product": product,
                    "businessObjectType": bo_type,
                    "page": page_number,
                    "error": str(e),
                })
                logger.exception("Failed to call Fusion audit API for %s/%s", product, bo_type)
                break

            status = data.get("status", "")
            if status == "FAIL":
                api_responses[-1]["api_error"] = data.get("error", {})
                logger.warning("Fusion API returned FAIL for %s/%s: %s", product, bo_type, data.get("error"))
                break

            result_data = data.get("result", data)
            if isinstance(result_data, list):
                items = result_data
            elif isinstance(result_data, dict):
                items = result_data.get("items", result_data.get("auditData", []))
            else:
                items = []

            if items:
                all_entries.append({
                    "timestamp": now,
                    "source": f"{product}/{bo_type}",
                    "data": data,
                })

            page_size = int(data.get("pageSize", 0))
            if not items or len(items) < page_limit or page_size < page_limit:
                break
            page_number += 1

    return all_entries, api_responses


# ---------------------------------------------------------------------------
# Streaming – publish
# ---------------------------------------------------------------------------
def publish_to_stream(
    clients: OCIClients,
    stream_ocid: str,
    entries: list[dict],
    batch_size: int,
) -> tuple[int, int]:
    """Publish entries to OCI Streaming. Returns (success_count, failure_count)."""
    if not entries:
        return 0, 0

    stream_client = clients.get_stream_client(stream_ocid)
    messages = []
    for entry in entries:
        encoded = base64.b64encode(json.dumps(entry, default=str).encode("utf-8")).decode("utf-8")
        messages.append(oci.streaming.models.PutMessagesDetailsEntry(value=encoded))

    total_ok = 0
    total_fail = 0

    for i in range(0, len(messages), batch_size):
        batch = messages[i : i + batch_size]
        try:
            resp = stream_client.put_messages(
                stream_id=stream_ocid,
                put_messages_details=oci.streaming.models.PutMessagesDetails(messages=batch),
            )
            failures = resp.data.failures if resp.data.failures else 0
            total_fail += failures
            total_ok += len(batch) - failures
        except oci.exceptions.ServiceError:
            logger.exception("Failed to publish batch to stream")
            total_fail += len(batch)

    return total_ok, total_fail


# ---------------------------------------------------------------------------
# FDK handler
# ---------------------------------------------------------------------------
def handler(ctx: fdk_context.InvokeContext, data: io.BytesIO = None) -> fdk.response.Response:
    try:
        clients = OCIClients()

        # Config
        pod_url = _cfg(ctx, "FUSION_POD_URL")
        audit_endpoint = _cfg(ctx, "FUSION_AUDIT_ENDPOINT")
        page_limit = int(_cfg(ctx, "FUSION_PAGE_LIMIT"))
        secret_ocid = _cfg(ctx, "VAULT_SECRET_OCID")
        stream_ocid = _cfg(ctx, "STREAM_OCID")
        batch_size = int(_cfg(ctx, "STREAM_BATCH_SIZE"))
        bucket = _cfg(ctx, "STATE_BUCKET")
        state_obj = _cfg(ctx, "STATE_OBJECT")
        audit_config_obj = _cfg(ctx, "AUDIT_CONFIG_OBJECT")
        namespace = _cfg(ctx, "STATE_NAMESPACE")

        if not namespace:
            namespace = clients.object_storage.get_namespace().data

        # 1. Load audit config (product/businessObjectType targets)
        audit_targets = load_audit_config(clients, namespace, bucket, audit_config_obj)
        logger.info("Loaded %d audit targets from %s", len(audit_targets), audit_config_obj)

        # 2. Load state (for future use: filter on timestamp)
        state = load_state(clients, namespace, bucket, state_obj)
        last_run = state.get("last_run")
        logger.info("Last run: %s", last_run or "first run")

        # 3. Get Fusion credentials from Vault
        credentials = get_fusion_credentials(clients, secret_ocid)
        logger.info("Auth type: %s", credentials["auth_type"])

        # 4. Collect audit logs from Fusion
        entries, api_responses = collect_fusion_logs(pod_url, audit_endpoint, audit_targets, credentials, page_limit, since=last_run)
        logger.info("Collected %d log batches from Fusion", len(entries))

        if not entries:
            return fdk.response.Response(
                ctx, response_data=json.dumps({"status": "ok", "collected": 0, "published": 0}),
                headers={"Content-Type": "application/json"},
            )

        # 5. Publish to OCI Streaming
        ok, fail = publish_to_stream(clients, stream_ocid, entries, batch_size)
        logger.info("Published %d messages (%d failures)", ok, fail)

        # 6. Update state
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        save_state(clients, namespace, bucket, state_obj, {"last_run": now})

        result = {"status": "ok", "collected": len(entries), "published": ok, "failures": fail}
        return fdk.response.Response(
            ctx, response_data=json.dumps(result),
            headers={"Content-Type": "application/json"},
        )

    except Exception as e:
        logger.exception("Function failed")
        import traceback
        return fdk.response.Response(
            ctx, response_data=json.dumps({"status": "error", "error": str(e), "traceback": traceback.format_exc()}),
            headers={"Content-Type": "application/json"}, status_code=500,
        )
