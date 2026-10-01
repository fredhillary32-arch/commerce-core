"""Product options and their values, and the scoping the schema gives them."""

import uuid

from sqlalchemy import text

from tests.catalog_helpers import (
    PRODUCTS,
    create_option,
    create_option_value,
    product_id,
)


class TestOptions:
    def test_an_option_is_created_against_its_product(self, client, catalog_admin, test_engine):
        product = product_id(client, catalog_admin)

        response = create_option(client, catalog_admin, product, name="Size")

        assert response.status_code == 201
        assert response.json()["name"] == "Size"
        assert response.json()["values"] == []
        with test_engine.connect() as connection:
            owner = connection.execute(
                text("SELECT product_id FROM product_options WHERE id = :o"),
                {"o": response.json()["id"]},
            ).scalar_one()
        assert str(owner) == product

    def test_options_appear_on_the_product(self, client, catalog_admin):
        product = product_id(client, catalog_admin)
        create_option(client, catalog_admin, product, name="Size")
        create_option(client, catalog_admin, product, name="Colour")

        body = client.get(f"{PRODUCTS}/{product}", headers=catalog_admin).json()

        assert [option["name"] for option in body["options"]] == ["Size", "Colour"]

    def test_an_option_cannot_be_created_on_a_product_that_does_not_exist(
        self, client, catalog_admin
    ):
        response = create_option(client, catalog_admin, uuid.uuid4())

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PRODUCT_NOT_FOUND"


class TestOptionValues:
    def test_a_value_is_created_against_its_option(self, client, catalog_admin):
        product = product_id(client, catalog_admin)
        option = create_option(client, catalog_admin, product).json()["id"]

        response = create_option_value(client, catalog_admin, product, option, "Large")

        assert response.status_code == 201
        assert response.json()["value"] == "Large"
        assert response.json()["option_id"] == option

    def test_values_are_grouped_under_their_option_on_the_product(self, client, catalog_admin):
        product = product_id(client, catalog_admin)
        size = create_option(client, catalog_admin, product, name="Size").json()["id"]
        colour = create_option(client, catalog_admin, product, name="Colour").json()["id"]
        create_option_value(client, catalog_admin, product, size, "Small")
        create_option_value(client, catalog_admin, product, size, "Large")
        create_option_value(client, catalog_admin, product, colour, "Brass")

        body = client.get(f"{PRODUCTS}/{product}", headers=catalog_admin).json()

        grouped = {option["name"]: [value["value"] for value in option["values"]] for option in body["options"]}
        assert grouped == {"Size": ["Small", "Large"], "Colour": ["Brass"]}

    def test_an_option_belonging_to_another_product_is_not_reachable(
        self, client, catalog_admin
    ):
        """Scoping comes from the schema; the service only refuses to cross it."""
        first = product_id(client, catalog_admin, name="First Lamp")
        second = product_id(client, catalog_admin, name="Second Lamp")
        foreign_option = create_option(client, catalog_admin, first).json()["id"]

        response = create_option_value(client, catalog_admin, second, foreign_option, "Large")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "OPTION_NOT_FOUND"

    def test_an_unknown_option_is_reported(self, client, catalog_admin):
        product = product_id(client, catalog_admin)

        response = create_option_value(client, catalog_admin, product, uuid.uuid4(), "Large")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "OPTION_NOT_FOUND"

    def test_a_value_transitively_belongs_to_one_product(
        self, client, catalog_admin, test_engine
    ):
        product = product_id(client, catalog_admin)
        option = create_option(client, catalog_admin, product).json()["id"]
        value = create_option_value(client, catalog_admin, product, option).json()["id"]

        with test_engine.connect() as connection:
            owner = connection.execute(
                text(
                    "SELECT o.product_id FROM product_option_values v "
                    "JOIN product_options o ON o.id = v.option_id WHERE v.id = :v"
                ),
                {"v": value},
            ).scalar_one()

        assert str(owner) == product
