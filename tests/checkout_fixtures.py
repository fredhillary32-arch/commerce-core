"""The scaffolding a checkout needs: a sellable variant, stock, and rules.

Shared by every Phase 5 behavioural test. Kept out of ``conftest.py`` so it
stays obvious that these are Phase 5 helpers rather than project-wide
fixtures, and imported explicitly by the modules that use it.
"""

from dataclasses import dataclass

import pytest

from tests.cart_helpers import add_item
from tests.catalog_helpers import activate_product_with_variant
from tests.inventory_helpers import provision, restock
from tests.order_helpers import address_id, create_shipping_rule, create_tax_rate


@dataclass
class Shop:
    """One configured shop and one customer with a basket ready to buy."""

    customer: dict
    address: str
    product: str
    variant: str
    catalog: dict
    inventory: dict


@pytest.fixture()
def shop_admin(authorize):
    """One operator holding every Phase 5 administrative permission."""
    return authorize(
        "catalog.read",
        "catalog.write",
        "inventory.read",
        "inventory.adjust",
        "tax.manage",
        "shipping.manage",
        email="operator@example.com",
    )


def sellable(client, admin, *, name="Aurora Desk Lamp", sku="LAMP-001", price="24.99",
             quantity=10, currency="GBP"):
    """A published product with one priced, ACTIVE, stocked variant."""
    product, variant = activate_product_with_variant(client, admin, name=name, sku=sku)
    client.patch(
        f"/api/v1/variants/{variant}",
        json={"price": price, "currency": currency},
        headers=admin,
    )
    provision(client, admin, variant)
    if quantity:
        restock(client, admin, variant, quantity)
    return product, variant


def configure_rules(client, admin, *, tax_rate="0.2000", shipping_cost="4.99", **overrides):
    create_tax_rate(client, admin, rate=tax_rate)
    create_shipping_rule(client, admin, cost=shipping_cost, **overrides)


@pytest.fixture()
def shop(client, shop_admin, authorize) -> Shop:
    """Everything a happy-path checkout needs, and nothing it does not."""
    product, variant = sellable(client, shop_admin)
    configure_rules(client, shop_admin)

    customer = authorize(email="buyer@example.com")
    add_item(client, customer, variant, quantity=2)
    address = address_id(client, customer)

    return Shop(
        customer=customer,
        address=address,
        product=product,
        variant=variant,
        catalog=shop_admin,
        inventory=shop_admin,
    )
