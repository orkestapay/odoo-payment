#!/usr/bin/env bash
# Install payment_orkestapay on a new database, run its tests and uninstall it.
# Runs inside the odoo:19.0 image. Used by .github/workflows/ci.yml; it can also be run locally:
#   docker run --rm --network <compose network> -v "$PWD":/src -w /src -e DB_HOST=db \
#     --entrypoint bash odoo:19.0 .github/scripts/odoo-ci.sh website_sale,payment_orkestapay
#
# $1: modules to install (comma-separated), e.g. `payment_orkestapay` or
#     `website_sale,payment_orkestapay` (the sales order breakdown tests need `sale`).
set -euo pipefail

MODULES="${1:?modules to install}"
DB_NAME="${DB_NAME:-ci}"
ADDONS_PATH="/usr/lib/python3/dist-packages/odoo/addons,${ADDONS_DIR:-$PWD}"
ODOO_ARGS=(
    --db_host="${DB_HOST:-localhost}" --db_port="${DB_PORT:-5432}"
    --db_user="${DB_USER:-odoo}" --db_password="${DB_PASSWORD:-odoo}"
    --addons-path="$ADDONS_PATH" -d "$DB_NAME" --no-http
)
LOG_LINE='^[0-9-]+ [0-9:,]+ [0-9]+ (ERROR|CRITICAL) '

fail() {
    echo "::error::$1"
    exit 1
}

echo "::group::Install ${MODULES} and run the tests"
odoo "${ODOO_ARGS[@]}" -i "$MODULES" --test-tags /payment_orkestapay --stop-after-init \
    2>&1 | tee install.log
echo "::endgroup::"
grep -E 'odoo.tests.result: .* tests when loading' install.log || fail "No test result in the log."
grep -qE ' 0 failed, 0 error\(s\) of [1-9][0-9]* tests' install.log || fail "Tests failed."
if grep -qE "$LOG_LINE" install.log; then
    grep -E "$LOG_LINE" install.log
    fail "Errors in the install/test log."
fi

echo "::group::Uninstall payment_orkestapay"
odoo shell "${ODOO_ARGS[@]}" 2>&1 <<'EOF' | tee uninstall.log
env['ir.module.module'].search([('name', '=', 'payment_orkestapay')]).button_immediate_uninstall()
env.cr.commit()
env.cr.execute("SELECT state FROM ir_module_module WHERE name = 'payment_orkestapay'")
print('MODULE STATE:', env.cr.fetchone()[0])
EOF
echo "::endgroup::"
grep -q 'MODULE STATE: uninstalled' uninstall.log || fail "The module was not uninstalled."
if grep -qE "$LOG_LINE" uninstall.log; then
    grep -E "$LOG_LINE" uninstall.log
    fail "Errors in the uninstall log."
fi
echo "Install, tests and uninstall OK (${MODULES})."
