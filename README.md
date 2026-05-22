# fusion-audit-collector

OCI Function that collects audit logs from Oracle Fusion Cloud Applications via
`fndAuditRESTService` and publishes them to OCI Streaming.


## Architecture

```text
┌───────────────┐   POST    ┌─────────────────────┐
│  OCI Function │ ────────► │  Fusion Cloud       │
│  (Python 3.11)│ ◄──────── │  fndAuditRESTService│
└──────┬────────┘           └─────────────────────┘
       │
       ├──► OCI Streaming (audit events)
       ├──► OCI Object Storage (state + config)
       └──► OCI Vault (Fusion credentials)
```

## Prerequisites

### OCI Infrastructure

The following OCI resources must be created:

- **Functions Application** – `fn-<project>-<env>-fusionaudit`
- **OCI Streaming** – stream for audit events
- **Object Storage bucket** – for state (`last_run.json`) and configuration (`audit_config_<env>.json`)
- **OCI Vault secret** – Fusion credentials in JSON format
- **Dynamic Group + Policies** – granting the function access to Vault, Streaming, and Object Storage

### Deploy Tools

> **Note:** The install commands below use `brew` (macOS). Adapt to your platform's package manager.

| Tool          | Installation                                                             |
|---------------|--------------------------------------------------------------------------|
| OCI CLI       | `brew install oci-cli`                                                   |
| Fn CLI        | `brew install fn`                                                        |
| Docker        | Docker Desktop or Colima (`brew install colima && colima start`)         |
| docker-buildx | `brew install docker-buildx` (for cross-platform builds on Apple Silicon) |

## Configuration

### Function Application Config

The following environment variables are configured on the Functions Application:

| Variable                | Description                                                                   |
|-------------------------|-------------------------------------------------------------------------------|
| `FUSION_POD_URL`        | Fusion pod URL, e.g. `https://<your-pod>.fa.ocs.oraclecloud.com`              |
| `FUSION_AUDIT_ENDPOINT` | REST endpoint: `/fscmRestApi/fndAuditRESTService/audittrail/getaudithistory`  |
| `FUSION_PAGE_LIMIT`     | Max records per API call (default: `1000`)                                    |
| `VAULT_SECRET_OCID`     | OCID of the Vault secret containing Fusion credentials                        |
| `STREAM_OCID`           | OCID of the OCI Streaming stream                                              |
| `STREAM_BATCH_SIZE`     | Messages per batch to Streaming (default: `100`)                              |
| `STATE_BUCKET`          | Object Storage bucket for state and config                                    |
| `STATE_OBJECT`          | State file name (default: `last_run.json`)                                    |
| `AUDIT_CONFIG_OBJECT`   | Audit config file name (e.g. `audit_config_dev.json`)                         |
| `STATE_NAMESPACE`       | Object Storage namespace (auto-detected if empty)                             |

### Vault Secret Format

The OCI Vault secret must contain JSON. For OAuth (recommended):

```json
{"client_id": "...", "client_secret": "...", "token_url": "https://<your_idcs_url>/oauth2/v1/token", "scope": "urn:opc:resource:consumer::all"}
```

See [Configure 2-Legged OAuth Using Oracle IDCS/IAM](https://docs.oracle.com/en/cloud/saas/sales/faaps/Configure_OAuth_Using_Oracle_IDCS_or_IAM.html)
for setting up client credentials with Fusion.

### Audit Configuration (Object Storage)

The file `audit_config_<env>.json` in the state bucket defines which Fusion audit objects
to collect. Format:

```json
[
  {"product": "Ledger", "businessObjectType": "oracle.apps.financials.generalLedger.calendars.accounting.uiModel.view.PeriodStatusAuditVO"},
  {"product": "Receivables", "businessObjectType": "oracle.apps.financials.receivables.customerSetup.customerProfiles.model.view.CustomerProfileAuditVO"},
  {"product": "Procurement", "businessObjectType": "oracle.apps.prc.poz.suppliers.protectedModel.core.view.AuditSupplierVO"}
]
```

See `audit_config_dev.json` for a complete example with 50 VOs from an example/Dev environment.

Upload to Object Storage:

```bash
oci os object put \
  --bucket-name <STATE_BUCKET> \
  --file audit_config_dev.json \
  --name audit_config_dev.json \
  --profile <OCI_PROFILE>
```

## Fusion Cloud – Access

### Required Privileges

The user configured in the Vault secret requires the following privileges in Fusion Cloud:

| Privilege                        | Description                                                         |
|----------------------------------|---------------------------------------------------------------------|
| `FND_VIEW_AUDIT_HISTORY_PRIV`    | View Audit History                                                  |
| `FND_MANAGE_AUDIT_POLICIES_PRIV` | Manage Audit Policies (optional, for modifying audit policies)      |

Assign via **Security Console** in Fusion:
Navigator → Tools → Security Console → Users → find the user → assign role/privilege.

### Enabling Audit in Fusion

Audit must be enabled for the desired products/objects in Fusion:

Navigator → Setup and Maintenance → search **"Manage Audit Policies"** → Select products and objects → enable.

### Finding What Is Enabled (SQL)

Run these queries against the Fusion database (via OTBI, BI Publisher, or SQL Access):

**Count of enabled audit attributes:**

```sql
SELECT COUNT(DISTINCT VIEW_OBJECT) AS total_audit_objects
FROM FND_AUDIT_ATTRIBUTES
WHERE ENABLED_FLAG = 'Y' AND AUDIT_SWITCH = 'ON';
```

**List of all enabled View Objects:**

```sql
SELECT DISTINCT VIEW_OBJECT
FROM FND_AUDIT_ATTRIBUTES
WHERE ENABLED_FLAG = 'Y' AND AUDIT_SWITCH = 'ON'
ORDER BY VIEW_OBJECT;
```

**Find product name (WEBAPP) for each View Object:**

```sql
SELECT DISTINCT wam.WEBAPP AS product, a.VIEW_OBJECT AS businessObjectType
FROM FND_AUDIT_ATTRIBUTES a
JOIN FND_AUDIT_WEBAPP_AM wam
  ON LOWER(a.VIEW_OBJECT) LIKE '%' || LOWER(wam.WEBAPP) || '%'
WHERE a.ENABLED_FLAG = 'Y' AND a.AUDIT_SWITCH = 'ON'
ORDER BY 1, 2;
```

The output from this query maps directly to the entries in `audit_config_<env>.json` –
each row becomes a `{"product": "...", "businessObjectType": "..."}` entry.

**Note:** Some View Objects do not match a WEBAPP by package name. In practice the `product` field
in the API is not strictly validated – you can use a logical product name. Test with `test_audit_products.sh`.

### Audit Status Overview

```sql
SELECT DISTINCT ENABLED_FLAG, AUDIT_SWITCH, COUNT(*) AS cnt
FROM FND_AUDIT_ATTRIBUTES
GROUP BY ENABLED_FLAG, AUDIT_SWITCH;
```

## Fusion REST API

The function calls:

```http
POST /fscmRestApi/fndAuditRESTService/audittrail/getaudithistory
Content-Type: application/json

{
  "product": "Ledger",
  "businessObjectType": "oracle.apps.financials.generalLedger.calendars.accounting.uiModel.view.PeriodStatusAuditVO",
  "fromDate": "2026-05-01",
  "toDate": "2026-05-19",
  "pageSize": "1000",
  "pageNumber": "1"
}
```

**Required parameters:** `product`, `businessObjectType`, `fromDate`, `toDate`

**Optional parameters:** `pageSize`, `pageNumber`, `eventType`, `user`, `timeZone`,
`includeAttributes`, `includeChildObjects`, `includeExtendedObjectIdentiferColumns`,
`includeImpersonator`, `attributeDetailMode`

**Reference:** [Oracle Docs – fndAuditRESTService](https://docs.oracle.com/en/cloud/saas/applications-common/26b/farca/op-fscmrestapi-fndauditrestservice-audittrail-getaudithistory-post.html)

## Deploy

```bash
./deploy.sh d <OCI_PROFILE>
```

Arguments:
- `d` – environment (`d`, `t`, `yt`, `p`)
- `<OCI_PROFILE>` – OCI CLI profile

### Manual Test

```bash
fn invoke function fn-<project>-<env>-fusionaudit fusion-audit-collector
```

## Testing the Audit Endpoint

Verify the API works directly:

```bash

curl -s -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"product":"Ledger","businessObjectType":"oracle.apps.financials.generalLedger.calendars.accounting.uiModel.view.PeriodStatusAuditVO","fromDate":"2026-05-01","toDate":"2026-05-19"}' \
  "https://<your-pod>.fa.ocs.oraclecloud.com/fscmRestApi/fndAuditRESTService/audittrail/getaudithistory"
```

Expected response: `{"actionName": "getAuditHistory", "status": "SUCCESS", ...}`

### Testing Unknown Product Names

Use `test_audit_products.sh` to test which product names work:

```bash
./test_audit_products.sh <client_id> <client_secret>
```

## Files

| File                     | Description                                        |
|--------------------------|----------------------------------------------------|
| `func.py`                | Function logic                                     |
| `func.yaml`              | Fn definition (runtime, memory, timeout)           |
| `requirements.txt`       | Python dependencies                                |
| `deploy.sh`              | Deploy script for OCI Functions                    |
| `audit_config_dev.json`  | Audit configuration for the dev environment (50 VOs) |
| `test_audit_products.sh` | Helper script to test product/VO combinations      |

## IAM: Dynamic Groups and Policies

The function runs with Resource Principal and requires two Dynamic Groups with
associated policies.

### Dynamic Group: fusion-audit-collectors-dg

Includes all functions in all active environment compartments:

```text
Any {{resource.type = 'fnfunc', resource.compartment.id = '<D_COMPARTMENT_OCID>'}, {resource.type = 'fnfunc', resource.compartment.id = '<T_COMPARTMENT_OCID>'}}
```

Policy (`fusion-audit-function-policy`):

```text
Allow dynamic-group fusion-audit-collectors-dg to read secret-bundles in compartment <env-compartment>
    where target.vault.id = '<VAULT_OCID>'
Allow dynamic-group fusion-audit-collectors-dg to read vaults in compartment <env-compartment>
    where target.vault.id = '<VAULT_OCID>'
Allow dynamic-group fusion-audit-collectors-dg to use keys in compartment <env-compartment>
    where target.key.id = '<KEY_OCID>'
Allow dynamic-group fusion-audit-collectors-dg to read streams in compartment <networking-compartment>
Allow dynamic-group fusion-audit-collectors-dg to manage objects in compartment <env-compartment>
    where target.bucket.name = '<STATE_BUCKET>'
Allow dynamic-group fusion-audit-collectors-dg to read objectstorage-namespaces in compartment <env-compartment>
```

The function also requires `stream-push` via policy
`allow-log-connector-to-write-stream`:

```text
Allow dynamic-group fusion-audit-collectors-dg to use stream-push in compartment <networking-compartment>
```

### Dynamic Group: resource-scheduler-fusionaudit-dg

For scheduling via OCI Resource Scheduler:

```text
All {resource.type = 'resourceschedule', resource.id = '<SCHEDULE_OCID>'}
```

Policy (`resource-scheduler-fusionaudit-policy`):

```text
Allow dynamic-group resource-scheduler-fusionaudit-dg to manage functions-family in compartment <env-compartment>
```


