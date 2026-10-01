"""Small request helpers shared by the catalogue tests.

Not a test module itself - it only removes the repeated URL building that
would otherwise bury what each test is actually asserting.
"""

PRODUCTS = "/api/v1/products"
VARIANTS = "/api/v1/variants"
CATEGORIES = "/api/v1/categories"
PUBLIC = "/api/v1/catalog"


def create_product(client, headers, **body):
    body.setdefault("name", "Aurora Desk Lamp")
    return client.post(PRODUCTS, json=body, headers=headers)


def product_id(client, headers, **body) -> str:
    return create_product(client, headers, **body).json()["id"]


def patch_product(client, headers, product, **body):
    return client.patch(f"{PRODUCTS}/{product}", json=body, headers=headers)


def create_option(client, headers, product, name="Size"):
    return client.post(f"{PRODUCTS}/{product}/options", json={"name": name}, headers=headers)


def create_option_value(client, headers, product, option, value="Large"):
    return client.post(
        f"{PRODUCTS}/{product}/options/{option}/values", json={"value": value}, headers=headers
    )


def create_variant(client, headers, product, **body):
    body.setdefault("sku", "LAMP-001")
    return client.post(f"{PRODUCTS}/{product}/variants", json=body, headers=headers)


def patch_variant(client, headers, variant, **body):
    return client.patch(f"{VARIANTS}/{variant}", json=body, headers=headers)


def assign_option_value(client, headers, variant, option, option_value):
    # str() so a test may pass a raw uuid.UUID for the "no such id" cases.
    return client.post(
        f"{VARIANTS}/{variant}/option-values",
        json={"option_id": str(option), "option_value_id": str(option_value)},
        headers=headers,
    )


def create_category(client, headers, **body):
    body.setdefault("name", "Lighting")
    return client.post(CATEGORIES, json=body, headers=headers)


def patch_category(client, headers, category, **body):
    return client.patch(f"{CATEGORIES}/{category}", json=body, headers=headers)


def assign_category(client, headers, product, category):
    return client.post(
        f"{PRODUCTS}/{product}/categories", json={"category_id": str(category)}, headers=headers
    )


def activate_product_with_variant(client, headers, *, name="Aurora Desk Lamp", sku="LAMP-001"):
    """Create a product that satisfies every rule for being published."""
    product = product_id(client, headers, name=name)
    variant = create_variant(
        client, headers, product, sku=sku, price="24.99", currency="GBP"
    ).json()["id"]
    patch_variant(client, headers, variant, status="ACTIVE")
    patch_product(client, headers, product, status="ACTIVE")
    return product, variant


def upload_url(client, headers, product):
    return client.post(f"{PRODUCTS}/{product}/images/upload-url", headers=headers)


def confirm_image(client, headers, product, storage_path, **body):
    return client.post(
        f"{PRODUCTS}/{product}/images/confirm",
        json={"storage_path": storage_path, **body},
        headers=headers,
    )


def delete_image(client, headers, product, image):
    return client.delete(f"{PRODUCTS}/{product}/images/{image}", headers=headers)
