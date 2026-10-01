"""Tax rates and shipping rules: configuration, and how checkout resolves them.

These are the two tables an operator fills in before anyone can buy
anything, and both follow the same rule: **absence stops the sale**. A
country with no tax rate is not a country with no tax, and a destination
with no shipping rule is not free delivery. Either would silently cost the
merchant money, so both are configuration gaps an operator is shown rather
than defaults the system invents.

Resolution itself is asserted here directly, rather than only through
checkout, because the precedence rules - region beats country, priority
beats band width - are where a quiet mistake would be expensive.
"""

import uuid
from decimal import Decimal

import pytest

from app.core.exceptions import BusinessRuleError
from app.modules.shipping import service as shipping_service
from app.modules.tax import service as tax_service
from tests.order_helpers import (
    SHIPPING_RULES,
    TAX_RATES,
    create_shipping_rule,
    create_tax_rate,
    error_code,
)


@pytest.fixture()
def tax_admin(authorize):
    return authorize("tax.manage")


@pytest.fixture()
def shipping_admin(authorize):
    return authorize("shipping.manage")


# --------------------------------------------------------------------------
# Tax rates
# --------------------------------------------------------------------------


class TestTaxRateConfiguration:
    def test_a_rate_is_created(self, client, tax_admin):
        response = create_tax_rate(client, tax_admin)

        assert response.status_code == 201
        body = response.json()
        assert body["rate"] == "0.2000"
        assert body["country_code"] == "GB"
        assert body["is_active"] is True

    def test_a_rate_above_one_is_refused(self, client, tax_admin):
        response = create_tax_rate(client, tax_admin, rate="1.5000")

        assert response.status_code == 422

    def test_a_negative_rate_is_refused(self, client, tax_admin):
        response = create_tax_rate(client, tax_admin, rate="-0.1000")

        assert response.status_code == 422

    def test_a_zero_rate_is_allowed_and_meaningful(self, client, tax_admin):
        """A deliberate 0% is how "no tax applies here" is expressed."""
        response = create_tax_rate(client, tax_admin, rate="0.0000", country_code="JE")

        assert response.status_code == 201
        assert response.json()["rate"] == "0.0000"

    def test_two_active_rates_for_one_jurisdiction_are_refused(self, client, tax_admin):
        create_tax_rate(client, tax_admin)

        response = create_tax_rate(client, tax_admin, name="UK VAT again")

        assert response.status_code == 409
        assert error_code(response) == "TAX_RATE_ALREADY_EXISTS"

    def test_a_retired_rate_frees_the_jurisdiction(self, client, tax_admin):
        first = create_tax_rate(client, tax_admin).json()["id"]
        client.patch(f"{TAX_RATES}/{first}", json={"is_active": False}, headers=tax_admin)

        response = create_tax_rate(client, tax_admin, rate="0.1750")

        assert response.status_code == 201

    def test_a_country_rate_and_a_region_rate_may_coexist(self, client, tax_admin):
        create_tax_rate(client, tax_admin)

        response = create_tax_rate(client, tax_admin, name="Scotland", region="Scotland")

        assert response.status_code == 201

    def test_rates_are_listed(self, client, tax_admin):
        create_tax_rate(client, tax_admin)

        listed = client.get(TAX_RATES, headers=tax_admin).json()

        assert [row["country_code"] for row in listed] == ["GB"]

    def test_an_unknown_rate_reports_not_found(self, client, tax_admin):
        response = client.patch(
            f"{TAX_RATES}/{uuid.uuid4()}", json={"is_active": False}, headers=tax_admin
        )

        assert response.status_code == 404
        assert error_code(response) == "TAX_RATE_NOT_FOUND"


class TestTaxRateAuthorization:
    def test_a_plain_customer_is_refused(self, client, authorize):
        response = create_tax_rate(client, authorize())

        assert response.status_code == 403
        assert error_code(response) == "PERMISSION_DENIED"

    def test_shipping_manage_does_not_grant_tax_manage(self, client, shipping_admin):
        assert create_tax_rate(client, shipping_admin).status_code == 403

    def test_inventory_adjust_does_not_grant_tax_manage(self, client, authorize):
        assert create_tax_rate(client, authorize("inventory.adjust")).status_code == 403

    def test_authentication_is_required(self, client):
        assert client.get(TAX_RATES).status_code == 401


class TestTaxResolution:
    def test_the_country_rate_is_found(self, client, tax_admin, db_session):
        create_tax_rate(client, tax_admin)

        resolved = tax_service.resolve_rate(db_session, country_code="GB", region=None)

        assert resolved.rate == Decimal("0.2000")

    def test_a_region_rate_beats_the_country_rate(self, client, tax_admin, db_session):
        create_tax_rate(client, tax_admin)
        create_tax_rate(client, tax_admin, name="Scotland", region="Scotland", rate="0.1000")

        resolved = tax_service.resolve_rate(db_session, country_code="GB", region="Scotland")

        assert resolved.rate == Decimal("0.1000")

    def test_an_unmatched_region_falls_back_to_the_country_rate(
        self, client, tax_admin, db_session
    ):
        create_tax_rate(client, tax_admin)
        create_tax_rate(client, tax_admin, name="Scotland", region="Scotland", rate="0.1000")

        resolved = tax_service.resolve_rate(db_session, country_code="GB", region="Wales")

        assert resolved.rate == Decimal("0.2000")

    def test_an_inactive_rate_is_never_resolved(self, client, tax_admin, db_session):
        rate = create_tax_rate(client, tax_admin).json()["id"]
        client.patch(f"{TAX_RATES}/{rate}", json={"is_active": False}, headers=tax_admin)

        with pytest.raises(BusinessRuleError) as raised:
            tax_service.resolve_rate(db_session, country_code="GB", region=None)
        assert raised.value.code == "NO_TAX_RATE"

    def test_an_unconfigured_country_is_rejected_not_zero_rated(self, db_session):
        with pytest.raises(BusinessRuleError) as raised:
            tax_service.resolve_rate(db_session, country_code="ZZ", region=None)

        assert raised.value.code == "NO_TAX_RATE"
        assert raised.value.status_code == 422


# --------------------------------------------------------------------------
# Shipping rules
# --------------------------------------------------------------------------


class TestShippingRuleConfiguration:
    def test_a_rule_is_created(self, client, shipping_admin):
        response = create_shipping_rule(client, shipping_admin)

        assert response.status_code == 201
        assert response.json()["cost"] == "4.99"

    def test_a_negative_cost_is_refused(self, client, shipping_admin):
        response = create_shipping_rule(client, shipping_admin, cost="-1.00")

        assert response.status_code == 422

    def test_a_zero_cost_rule_is_how_free_shipping_is_expressed(
        self, client, shipping_admin
    ):
        response = create_shipping_rule(
            client, shipping_admin, name="Free over 50", min_order_subtotal="50.00", cost="0.00"
        )

        assert response.status_code == 201

    def test_an_inverted_band_is_refused(self, client, shipping_admin):
        response = create_shipping_rule(
            client,
            shipping_admin,
            min_order_subtotal="50.00",
            max_order_subtotal="10.00",
        )

        assert response.status_code == 422
        assert error_code(response) == "INVALID_SHIPPING_RULE"

    def test_rules_are_listed(self, client, shipping_admin):
        create_shipping_rule(client, shipping_admin)

        listed = client.get(SHIPPING_RULES, headers=shipping_admin).json()

        assert len(listed) == 1

    def test_an_unknown_rule_reports_not_found(self, client, shipping_admin):
        response = client.patch(
            f"{SHIPPING_RULES}/{uuid.uuid4()}",
            json={"is_active": False},
            headers=shipping_admin,
        )

        assert response.status_code == 404
        assert error_code(response) == "SHIPPING_RULE_NOT_FOUND"


class TestShippingRuleAuthorization:
    def test_a_plain_customer_is_refused(self, client, authorize):
        assert create_shipping_rule(client, authorize()).status_code == 403

    def test_tax_manage_does_not_grant_shipping_manage(self, client, tax_admin):
        assert create_shipping_rule(client, tax_admin).status_code == 403

    def test_authentication_is_required(self, client):
        assert client.get(SHIPPING_RULES).status_code == 401


class TestShippingResolution:
    def test_the_matching_rule_is_found(self, client, shipping_admin, db_session):
        create_shipping_rule(client, shipping_admin)

        rule = shipping_service.resolve_rule(
            db_session, country_code="GB", subtotal=Decimal("20.00"), currency="GBP"
        )

        assert rule.cost == Decimal("4.99")

    def test_a_band_that_excludes_the_subtotal_does_not_match(
        self, client, shipping_admin, db_session
    ):
        create_shipping_rule(
            client, shipping_admin, min_order_subtotal="50.00", max_order_subtotal="100.00"
        )

        with pytest.raises(BusinessRuleError) as raised:
            shipping_service.resolve_rule(
                db_session, country_code="GB", subtotal=Decimal("20.00"), currency="GBP"
            )
        assert raised.value.code == "NO_SHIPPING_RULE"

    def test_free_shipping_above_a_threshold_wins_on_a_narrower_band(
        self, client, shipping_admin, db_session
    ):
        create_shipping_rule(client, shipping_admin, max_order_subtotal="1000.00")
        create_shipping_rule(
            client,
            shipping_admin,
            name="Free over 50",
            min_order_subtotal="50.00",
            max_order_subtotal="1000.00",
            cost="0.00",
        )

        rule = shipping_service.resolve_rule(
            db_session, country_code="GB", subtotal=Decimal("60.00"), currency="GBP"
        )

        assert rule.cost == Decimal("0.00")

    def test_priority_beats_band_width(self, client, shipping_admin, db_session):
        create_shipping_rule(
            client, shipping_admin, name="Narrow", max_order_subtotal="10.00", cost="9.99"
        )
        create_shipping_rule(
            client, shipping_admin, name="Promotion", priority=5, cost="1.00"
        )

        rule = shipping_service.resolve_rule(
            db_session, country_code="GB", subtotal=Decimal("5.00"), currency="GBP"
        )

        assert rule.cost == Decimal("1.00")

    def test_an_open_ended_top_band_matches_any_larger_subtotal(
        self, client, shipping_admin, db_session
    ):
        create_shipping_rule(client, shipping_admin, min_order_subtotal="0.00")

        rule = shipping_service.resolve_rule(
            db_session, country_code="GB", subtotal=Decimal("99999.00"), currency="GBP"
        )

        assert rule.cost == Decimal("4.99")

    def test_an_inactive_rule_is_never_resolved(self, client, shipping_admin, db_session):
        rule = create_shipping_rule(client, shipping_admin).json()["id"]
        client.patch(
            f"{SHIPPING_RULES}/{rule}", json={"is_active": False}, headers=shipping_admin
        )

        with pytest.raises(BusinessRuleError) as raised:
            shipping_service.resolve_rule(
                db_session, country_code="GB", subtotal=Decimal("20.00"), currency="GBP"
            )
        assert raised.value.code == "NO_SHIPPING_RULE"

    def test_an_unconfigured_country_is_rejected_not_shipped_free(self, db_session):
        with pytest.raises(BusinessRuleError) as raised:
            shipping_service.resolve_rule(
                db_session, country_code="ZZ", subtotal=Decimal("20.00"), currency="GBP"
            )

        assert raised.value.code == "NO_SHIPPING_RULE"

    def test_a_rule_in_another_currency_is_refused_not_converted(
        self, client, shipping_admin, db_session
    ):
        create_shipping_rule(client, shipping_admin, currency="USD")

        with pytest.raises(BusinessRuleError) as raised:
            shipping_service.resolve_rule(
                db_session, country_code="GB", subtotal=Decimal("20.00"), currency="GBP"
            )

        assert raised.value.code == "SHIPPING_RULE_CURRENCY_MISMATCH"
