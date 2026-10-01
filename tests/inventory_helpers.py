"""Small request helpers shared by the inventory tests.

Not a test module itself - it only removes the repeated URL building that
would otherwise bury what each test is actually asserting.
"""

INVENTORY = "/api/v1/inventory"


def provision(client, headers, variant):
    return client.post(INVENTORY, json={"variant_id": str(variant)}, headers=headers)


def read_inventory(client, headers, variant):
    return client.get(f"{INVENTORY}/{variant}", headers=headers)


def adjust(client, headers, variant, **body):
    body.setdefault("reason", "Warehouse stock count")
    return client.post(f"{INVENTORY}/{variant}/adjustments", json=body, headers=headers)


def restock(client, headers, variant, quantity, **body):
    return adjust(
        client,
        headers,
        variant,
        transaction_type="RESTOCK",
        quantity=quantity,
        to_bucket="ON_HAND",
        **body,
    )


def list_transactions(client, headers, variant):
    return client.get(f"{INVENTORY}/{variant}/transactions", headers=headers)


def list_reservations(client, headers, variant):
    return client.get(f"{INVENTORY}/{variant}/reservations", headers=headers)


def stocked(client, admin_headers, variant, quantity):
    """Provision a variant and put `quantity` units of good stock on hand."""
    provision(client, admin_headers, variant)
    restock(client, admin_headers, variant, quantity)
    return variant
