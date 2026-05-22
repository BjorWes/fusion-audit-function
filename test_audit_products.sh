#!/bin/bash
# Test which product name works for suppliers and payments VOs
# Uses OAuth client_credentials flow to obtain a bearer token.
#
# Usage: ./test_audit_products.sh <client_id> <client_secret>

CLIENT_ID="${1:?Usage: $0 <client_id> <client_secret>}"
CLIENT_SECRET="${2:?Usage: $0 <client_id> <client_secret>}"

HOST="https://<your-pod>.fa.ocs.oraclecloud.com"
URL="$HOST/fscmRestApi/fndAuditRESTService/audittrail/getaudithistory"

# --- OAuth token acquisition --------------------------------------------------
# Verify that IDCS_URL and SCOPE match your environment.
IDCS_URL="https://<your_idcs_url>/oauth2/v1/token"
SCOPE="urn:opc:resource:consumer::all"

echo "==> Acquiring OAuth token from ${IDCS_URL} ..."
TOKEN_RESPONSE="$(curl -s -X POST "$IDCS_URL" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -u "$CLIENT_ID:$CLIENT_SECRET" \
  -d "grant_type=client_credentials&scope=${SCOPE}")"

TOKEN="$(echo "$TOKEN_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")"
if [[ -z "$TOKEN" ]]; then
  echo "ERROR: Failed to obtain access token. Response:"
  echo "$TOKEN_RESPONSE"
  exit 1
fi
echo "==> Token acquired successfully."

# --- Test calls ---------------------------------------------------------------

echo ""
echo "=== Testing product names for Supplier VOs ==="
for prod in customer Payments Procurement Supplier FinancialCommon Payables Purchasing; do
  printf "%-20s: " "$prod"
  curl -s -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"product\":\"$prod\",\"businessObjectType\":\"oracle.apps.prc.poz.suppliers.protectedModel.core.view.AuditSupplierVO\",\"fromDate\":\"2026-05-01\",\"toDate\":\"2026-05-19\"}" \
    "$URL" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status'), d.get('error',{}).get('title','ok')[:60])"
done

echo ""
echo "=== Testing product names for Payments VOs ==="
for prod in Payments FinancialCommon Payables Disbursement CashManagement; do
  printf "%-20s: " "$prod"
  curl -s -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"product\":\"$prod\",\"businessObjectType\":\"oracle.apps.financials.payments.shared.bankAccounts.bankAccountService.view.ExternalBankAccountVO\",\"fromDate\":\"2026-05-01\",\"toDate\":\"2026-05-19\"}" \
    "$URL" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status'), d.get('error',{}).get('title','ok')[:60])"
done

echo ""
echo "=== Testing product names for Location/PartySite VOs ==="
for prod in customer TradeCommunity CDM; do
  printf "%-20s: " "$prod"
  curl -s -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"product\":\"$prod\",\"businessObjectType\":\"oracle.apps.cdm.foundation.parties.locationService.view.LocationVO\",\"fromDate\":\"2026-05-01\",\"toDate\":\"2026-05-19\"}" \
    "$URL" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status'), d.get('error',{}).get('title','ok')[:60])"
done

echo ""
echo "=== Testing product names for DataSecurity VO ==="
for prod in FinancialCommon Ledger FND; do
  printf "%-20s: " "$prod"
  curl -s -X POST -u "$USER:$PASS" \
    -H "Content-Type: application/json" \
    -d "{\"product\":\"$prod\",\"businessObjectType\":\"oracle.apps.financials.commonModules.shared.dataSecurity.uiModel.view.UserRoleDataAsgnmntsVO\",\"fromDate\":\"2026-05-01\",\"toDate\":\"2026-05-19\"}" \
    "$URL" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status'), d.get('error',{}).get('title','ok')[:60])"
done
