"""Small request helpers shared by the cart tests.

Not a test module itself - it only removes the repeated URL building that
would otherwise bury what each test is actually asserting.
"""

CART = "/api/v1/cart"
CART_ITEMS = "/api/v1/cart/items"


def get_cart(client, headers):
    return client.get(CART, headers=headers)


def add_item(client, headers, variant, quantity=1):
    return client.post(
        CART_ITEMS, json={"variant_id": str(variant), "quantity": quantity}, headers=headers
    )


def update_item(client, headers, item, quantity):
    return client.patch(f"{CART_ITEMS}/{item}", json={"quantity": quantity}, headers=headers)


def remove_item(client, headers, item):
    return client.delete(f"{CART_ITEMS}/{item}", headers=headers)


def clear_cart(client, headers):
    return client.delete(CART, headers=headers)


def items_of(response) -> list[dict]:
    return response.json()["items"]


def only_item(response) -> dict:
    items = items_of(response)
    assert len(items) == 1, items
    return items[0]
