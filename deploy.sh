#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Deploy fusion-audit-collector to OCI Functions
#
# Prerequisites:
#   - OCI CLI configured (oci session authenticate / API key)
#   - Fn CLI installed (https://github.com/fnproject/cli)
#   - Docker running (for building the function image)
#   - OCIR auth token (generate in OCI Console → User Settings → Auth Tokens)
#
# Usage:
#   ./deploy.sh d <OCI_PROFILE>  # deploys to dev using a named OCI profile
#   ./deploy.sh d                # deploys to dev using DEFAULT profile
# -----------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Arguments ----------------------------------------------------------------
ENV="${1:-d}"
OCI_PROFILE="${2:-DEFAULT}"
OCI_OPTS=(--profile "$OCI_PROFILE")

case "$ENV" in
  d)
    APP_NAME="fusionaudit-application-dev"
    COMPARTMENT_OCID="ocid1.compartment.oc1..<DEV_COMPARTMENT_OCID>"
    ;;
  t)
    APP_NAME="fusionaudit-application-test"
    COMPARTMENT_OCID="ocid1.compartment.oc1..<TEST_COMPARTMENT_OCID>"
    ;;
  *)
    echo "ERROR: Unknown environment '$ENV'. Valid: d, t"
    exit 1
    ;;
esac

# --- OCI configuration -------------------------------------------------------
REGION="${OCI_REGION:-<OCI_REGION>}"
OCIR_DOMAIN="ocir.${REGION}.oci.oraclecloud.com"
TENANCY_NAMESPACE="${OCI_TENANCY_NAMESPACE:-<TENANCY_NAMESPACE>}"

# --- Set Fn context -----------------------------------------------------------
echo "==> Configuring Fn context for $ENV ($APP_NAME)"
fn use context "$REGION" 2>/dev/null || fn create context "$REGION" --provider oracle && fn use context "$REGION"
fn update context oracle.profile "$OCI_PROFILE"
fn update context oracle.compartment-id "$COMPARTMENT_OCID"
fn update context api-url "https://functions.${REGION}.oci.oraclecloud.com"
fn update context registry "${OCIR_DOMAIN}/${TENANCY_NAMESPACE}/functions"

# --- Docker login to OCIR -----------------------------------------------------
echo "==> Logging in to OCIR (${OCIR_DOMAIN})"

# Resolve current user's email from the OCI profile
if [[ -z "${OCI_USER_EMAIL:-}" ]]; then
  USER_OCID="$(grep -A10 "^\[${OCI_PROFILE}\]" ~/.oci/config | grep "^user" | head -1 | cut -d= -f2 | tr -d ' ')"
  OCI_USER_EMAIL="$(oci iam user get "${OCI_OPTS[@]}" --user-id "$USER_OCID" --query 'data.email' --raw-output)"
fi
echo "    User: ${TENANCY_NAMESPACE}/${OCI_USER_EMAIL}"

read -rsp "Enter OCIR auth token: " AUTH_TOKEN
echo

docker login "${OCIR_DOMAIN}" \
  -u "${TENANCY_NAMESPACE}/${OCI_USER_EMAIL}" \
  --password-stdin <<< "$AUTH_TOKEN"

unset AUTH_TOKEN

# --- Deploy -------------------------------------------------------------------
echo "==> Deploying function to app: $APP_NAME"
cd "$SCRIPT_DIR"
fn deploy --app "$APP_NAME"

echo "==> Done. Function deployed to $APP_NAME ($ENV)"
