"""One product authored end to end, then withdrawn.

Every other catalogue test isolates a rule; this one checks the rules
compose - that a merchant can take a product from nothing to published and
back out again through the documented endpoints alone.
"""

from sqlalchemy import text

from tests.catalog_helpers import (
    PUBLIC,
    assign_category,
    assign_option_value,
    confirm_image,
    create_category,
    create_option,
    create_option_value,
    create_product,
    create_variant,
    patch_product,
    patch_variant,
    upload_url,
)


def test_a_product_is_authored_published_and_then_withdrawn(
    client, catalog_admin, fake_storage, test_engine
):
    # 1. A product, which starts life invisible.
    product = create_product(
        client,
        catalog_admin,
        name="Aurora Desk Lamp",
        description="A warm brass task lamp.",
    ).json()
    assert product["status"] == "DRAFT"
    assert client.get(f"{PUBLIC}/products").json() == []

    # 2. The dimension it varies along, and the values available.
    size = create_option(client, catalog_admin, product["id"], name="Size").json()["id"]
    small = create_option_value(client, catalog_admin, product["id"], size, "Small").json()["id"]
    large = create_option_value(client, catalog_admin, product["id"], size, "Large").json()["id"]

    # 3. Two variants, each a real configuration with its own SKU.
    small_variant = create_variant(
        client, catalog_admin, product["id"], sku="LAMP-S", price="19.99", currency="GBP"
    ).json()["id"]
    large_variant = create_variant(
        client, catalog_admin, product["id"], sku="LAMP-L", price="24.99", currency="GBP"
    ).json()["id"]
    assert assign_option_value(
        client, catalog_admin, small_variant, size, small
    ).status_code == 201
    assert assign_option_value(
        client, catalog_admin, large_variant, size, large
    ).status_code == 201

    # 4. Activating a variant, then the product it belongs to.
    assert patch_variant(
        client, catalog_admin, large_variant, status="ACTIVE"
    ).json()["status"] == "ACTIVE"
    assert patch_product(
        client, catalog_admin, product["id"], status="ACTIVE"
    ).json()["status"] == "ACTIVE"

    # 5. Merchandising.
    category = create_category(client, catalog_admin, name="Desk Lighting").json()["id"]
    assert assign_category(client, catalog_admin, product["id"], category).status_code == 204

    # 6. An image: reserve a path, "upload" to Supabase, confirm.
    target = upload_url(client, catalog_admin, product["id"]).json()
    assert target["storage_path"].startswith(f"products/{product['id']}/images/")
    fake_storage.simulate_upload(target["storage_path"])
    image = confirm_image(
        client,
        catalog_admin,
        product["id"],
        target["storage_path"],
        alt_text="Aurora lamp on a desk",
        is_primary=True,
    ).json()
    assert image["is_primary"] is True

    # 7. What a customer sees.
    fake_storage.delivery_calls.clear()
    public = client.get(f"{PUBLIC}/products/{product['id']}")
    assert public.status_code == 200
    body = public.json()

    assert body["name"] == "Aurora Desk Lamp"
    assert [category["name"] for category in body["categories"]] == ["Desk Lighting"]
    # Only the ACTIVE variant, priced, with its option value resolved.
    assert len(body["variants"]) == 1
    assert body["variants"][0]["sku"] == "LAMP-L"
    assert body["variants"][0]["price"] == "24.99"
    assert body["variants"][0]["currency"] == "GBP"
    assert body["variants"][0]["option_values"] == [
        {"option_id": size, "option_name": "Size", "option_value_id": large, "value": "Large"}
    ]
    # The delivery URL was signed for this response, from the stored path.
    assert fake_storage.delivery_calls == [target["storage_path"]]
    assert body["images"][0]["url"].startswith("https://storage.test/signed/")
    assert body["images"][0]["alt_text"] == "Aurora lamp on a desk"

    # 8. Withdrawing it hides it from customers without losing anything.
    patch_product(client, catalog_admin, product["id"], status="ARCHIVED")

    assert client.get(f"{PUBLIC}/products").json() == []
    assert client.get(f"{PUBLIC}/products/{product['id']}").status_code == 404

    admin = client.get(f"/api/v1/products/{product['id']}", headers=catalog_admin)
    assert admin.status_code == 200
    retained = admin.json()
    assert retained["status"] == "ARCHIVED"
    assert {variant["sku"] for variant in retained["variants"]} == {"LAMP-S", "LAMP-L"}
    assert len(retained["images"]) == 1
    assert retained["categories"][0]["name"] == "Desk Lighting"

    # 9. The whole journey is on the audit trail.
    with test_engine.connect() as connection:
        events = connection.execute(text("SELECT event_type FROM audit_logs")).scalars().all()
    assert {
        "product.created",
        "variant.created",
        "product.published",
        "category.created",
        "image.uploaded",
        "product.archived",
    } <= set(events)
