"""Small request helpers shared by the Phase 5 tests.

Not a test module itself - it only removes the repeated URL and payload
building that would otherwise bury what each test is actually asserting.
"""

ADDRESSES = "/api/v1/addresses"
CHECKOUT = "/api/v1/checkout"
ORDERS = "/api/v1/orders"
ADMIN_ORDERS = "/api/v1/admin/orders"
TAX_RATES = "/api/v1/tax-rates"
SHIPPING_RULES = "/api/v1/shipping-rules"

# One destination, used by almost every test, so that a rule created with
# these defaults always matches an address created with them.
COUNTRY = "GB"
REGION = "England"


# --------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------


def address_payload(**overrides) -> dict:
    payload = {
        "label": "Home",
        "recipient_name": "Ada Lovelace",
        "line1": "12 Analytical Way",
        "city": "London",
        "region": REGION,
        "postal_code": "W1A 1AA",
        "country_code": COUNTRY,
    }
    payload.update(overrides)
    return payload


def create_address(client, headers, **overrides):
    return client.post(ADDRESSES, json=address_payload(**overrides), headers=headers)


def address_id(client, headers, **overrides) -> str:
    return create_address(client, headers, **overrides).json()["id"]


def list_addresses(client, headers):
    return client.get(ADDRESSES, headers=headers)


def patch_address(client, headers, address, **body):
    return client.patch(f"{ADDRESSES}/{address}", json=body, headers=headers)


def delete_address(client, headers, address):
    return client.delete(f"{ADDRESSES}/{address}", headers=headers)


# --------------------------------------------------------------------------
# Tax rates and shipping rules
# --------------------------------------------------------------------------


def create_tax_rate(client, headers, **overrides):
    body = {"name": "UK VAT", "rate": "0.2000", "country_code": COUNTRY}
    body.update(overrides)
    return client.post(TAX_RATES, json=body, headers=headers)


def create_shipping_rule(client, headers, **overrides):
    body = {
        "name": "UK standard",
        "country_code": COUNTRY,
        "min_order_subtotal": "0.00",
        "cost": "4.99",
        "currency": "GBP",
    }
    body.update(overrides)
    return client.post(SHIPPING_RULES, json=body, headers=headers)


# --------------------------------------------------------------------------
# Checkout and orders
# --------------------------------------------------------------------------


def checkout(client, headers, shipping_address, billing_address=None):
    body = {"shipping_address_id": str(shipping_address)}
    if billing_address is not None:
        body["billing_address_id"] = str(billing_address)
    return client.post(CHECKOUT, json=body, headers=headers)


def get_order(client, headers, order):
    return client.get(f"{ORDERS}/{order}", headers=headers)


def list_orders(client, headers, **params):
    return client.get(ORDERS, headers=headers, params=params)


def cancel_order(client, headers, order):
    return client.post(f"{ORDERS}/{order}/cancel", headers=headers)


def error_code(response) -> str:
    return response.json()["error"]["code"]


def only_line(response) -> dict:
    items = response.json()["items"]
    assert len(items) == 1, items
    return items[0]
