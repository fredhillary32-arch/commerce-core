"""Assigning option values to variants, and the signature that keeps
combinations unique."""

import uuid

import pytest
from sqlalchemy import text

from app.modules.catalog.service import compute_signature
from tests.catalog_helpers import (
    assign_option_value,
    create_option,
    create_option_value,
    create_variant,
    product_id,
)


@pytest.fixture()
def configured_product(client, catalog_admin) -> dict:
    """A product with Size (Small/Large) and Colour (Brass/Black)."""
    product = product_id(client, catalog_admin)
    size = create_option(client, catalog_admin, product, name="Size").json()["id"]
    colour = create_option(client, catalog_admin, product, name="Colour").json()["id"]
    return {
        "product": product,
        "size": size,
        "colour": colour,
        "small": create_option_value(client, catalog_admin, product, size, "Small").json()["id"],
        "large": create_option_value(client, catalog_admin, product, size, "Large").json()["id"],
        "brass": create_option_value(client, catalog_admin, product, colour, "Brass").json()["id"],
        "black": create_option_value(client, catalog_admin, product, colour, "Black").json()["id"],
    }


def signature_of(test_engine, variant_id) -> str | None:
    with test_engine.connect() as connection:
        return connection.execute(
            text("SELECT variant_signature FROM product_variants WHERE id = :v"),
            {"v": variant_id},
        ).scalar_one()


class TestAssignment:
    def test_an_assignment_appears_on_the_variant(
        self, client, catalog_admin, configured_product
    ):
        catalogue = configured_product
        variant = create_variant(client, catalog_admin, catalogue["product"]).json()["id"]

        response = assign_option_value(
            client, catalog_admin, variant, catalogue["size"], catalogue["small"]
        )

        assert response.status_code == 201
        assert response.json()["option_values"] == [
            {
                "option_id": catalogue["size"],
                "option_name": "Size",
                "option_value_id": catalogue["small"],
                "value": "Small",
            }
        ]

    def test_a_second_value_for_the_same_option_is_refused(
        self, client, catalog_admin, configured_product
    ):
        catalogue = configured_product
        variant = create_variant(client, catalog_admin, catalogue["product"]).json()["id"]
        assign_option_value(client, catalog_admin, variant, catalogue["size"], catalogue["small"])

        response = assign_option_value(
            client, catalog_admin, variant, catalogue["size"], catalogue["large"]
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "OPTION_ALREADY_ASSIGNED"

    def test_a_value_belonging_to_a_different_option_is_refused(
        self, client, catalog_admin, configured_product
    ):
        catalogue = configured_product
        variant = create_variant(client, catalog_admin, catalogue["product"]).json()["id"]

        response = assign_option_value(
            # Claims to be setting Size, but hands over a Colour's value.
            client, catalog_admin, variant, catalogue["size"], catalogue["brass"]
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "OPTION_VALUE_NOT_FOUND"

    def test_an_option_from_another_product_is_refused(
        self, client, catalog_admin, configured_product
    ):
        catalogue = configured_product
        other_product = product_id(client, catalog_admin, name="Other Lamp")
        other_option = create_option(client, catalog_admin, other_product).json()["id"]
        other_value = create_option_value(
            client, catalog_admin, other_product, other_option, "Huge"
        ).json()["id"]
        variant = create_variant(client, catalog_admin, catalogue["product"]).json()["id"]

        response = assign_option_value(
            client, catalog_admin, variant, other_option, other_value
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "OPTION_NOT_FOUND"

    def test_an_unknown_variant_is_reported(self, client, catalog_admin, configured_product):
        catalogue = configured_product

        response = assign_option_value(
            client, catalog_admin, uuid.uuid4(), catalogue["size"], catalogue["small"]
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "VARIANT_NOT_FOUND"

    def test_an_unknown_value_is_reported(self, client, catalog_admin, configured_product):
        catalogue = configured_product
        variant = create_variant(client, catalog_admin, catalogue["product"]).json()["id"]

        response = assign_option_value(
            client, catalog_admin, variant, catalogue["size"], uuid.uuid4()
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "OPTION_VALUE_NOT_FOUND"


class TestSignature:
    def test_an_unassigned_variant_has_no_signature(
        self, client, catalog_admin, configured_product, test_engine
    ):
        variant = create_variant(
            client, catalog_admin, configured_product["product"]
        ).json()["id"]

        assert signature_of(test_engine, variant) is None

    def test_the_signature_is_written_with_the_first_assignment(
        self, client, catalog_admin, configured_product, test_engine
    ):
        catalogue = configured_product
        variant = create_variant(client, catalog_admin, catalogue["product"]).json()["id"]

        assign_option_value(client, catalog_admin, variant, catalogue["size"], catalogue["small"])

        assert signature_of(test_engine, variant) == f"{catalogue['size']}:{catalogue['small']}"

    def test_the_signature_is_recomputed_on_every_further_assignment(
        self, client, catalog_admin, configured_product, test_engine
    ):
        catalogue = configured_product
        variant = create_variant(client, catalog_admin, catalogue["product"]).json()["id"]
        assign_option_value(client, catalog_admin, variant, catalogue["size"], catalogue["small"])
        assign_option_value(
            client, catalog_admin, variant, catalogue["colour"], catalogue["brass"]
        )

        signature = signature_of(test_engine, variant)

        assert signature.count("|") == 1
        assert f"{catalogue['size']}:{catalogue['small']}" in signature
        assert f"{catalogue['colour']}:{catalogue['brass']}" in signature

    def test_the_signature_does_not_depend_on_assignment_order(
        self, client, catalog_admin, configured_product, test_engine
    ):
        """Sorting by option id is what makes the constraint meaningful."""
        catalogue = configured_product
        first = create_variant(
            client, catalog_admin, catalogue["product"], sku="ORDER-A"
        ).json()["id"]
        assign_option_value(client, catalog_admin, first, catalogue["size"], catalogue["small"])
        assign_option_value(client, catalog_admin, first, catalogue["colour"], catalogue["brass"])

        second = create_variant(
            client, catalog_admin, catalogue["product"], sku="ORDER-B"
        ).json()["id"]
        assign_option_value(client, catalog_admin, second, catalogue["colour"], catalogue["black"])
        assign_option_value(client, catalog_admin, second, catalogue["size"], catalogue["small"])

        # Different orders, different combinations - but both signatures are
        # built the same way, so they are directly comparable.
        assert signature_of(test_engine, first).split("|")[0].split(":")[0] == signature_of(
            test_engine, second
        ).split("|")[0].split(":")[0]

    def test_compute_signature_is_null_for_no_assignments(self):
        assert compute_signature([]) is None


class TestDuplicateCombinations:
    def test_a_duplicate_combination_on_one_product_is_refused(
        self, client, catalog_admin, configured_product, test_engine
    ):
        """Sequential by design, not threaded.

        A duplicate combination is stopped by a single INSERT/UPDATE against
        UNIQUE (product_id, variant_signature) with no read step in between,
        so there is no check-then-act window for timing to exploit - unlike
        Phase 2's refresh-token rotation, which genuinely needed overlapping
        transactions to prove. Committing the first and then attempting the
        second exercises the constraint itself, deterministically.
        """
        catalogue = configured_product
        first = create_variant(
            client, catalog_admin, catalogue["product"], sku="DUP-A"
        ).json()["id"]
        assign_option_value(client, catalog_admin, first, catalogue["size"], catalogue["small"])
        assert (
            assign_option_value(
                client, catalog_admin, first, catalogue["colour"], catalogue["brass"]
            ).status_code
            == 201
        )

        second = create_variant(
            client, catalog_admin, catalogue["product"], sku="DUP-B"
        ).json()["id"]
        assign_option_value(client, catalog_admin, second, catalogue["size"], catalogue["small"])
        response = assign_option_value(
            client, catalog_admin, second, catalogue["colour"], catalogue["brass"]
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DUPLICATE_VARIANT_COMBINATION"

    def test_the_rejected_assignment_leaves_nothing_behind(
        self, client, catalog_admin, configured_product, test_engine
    ):
        """The assignment and the signature update commit together or not at all."""
        catalogue = configured_product
        first = create_variant(
            client, catalog_admin, catalogue["product"], sku="ATOMIC-A"
        ).json()["id"]
        assign_option_value(client, catalog_admin, first, catalogue["size"], catalogue["small"])

        second = create_variant(
            client, catalog_admin, catalogue["product"], sku="ATOMIC-B"
        ).json()["id"]
        assign_option_value(client, catalog_admin, second, catalogue["size"], catalogue["small"])

        with test_engine.connect() as connection:
            assignments = connection.execute(
                text("SELECT count(*) FROM variant_option_values WHERE variant_id = :v"),
                {"v": second},
            ).scalar_one()
        assert assignments == 0
        assert signature_of(test_engine, second) is None

    def test_a_partially_overlapping_combination_is_allowed(
        self, client, catalog_admin, configured_product
    ):
        catalogue = configured_product
        first = create_variant(
            client, catalog_admin, catalogue["product"], sku="OVERLAP-A"
        ).json()["id"]
        assign_option_value(client, catalog_admin, first, catalogue["size"], catalogue["small"])
        assign_option_value(client, catalog_admin, first, catalogue["colour"], catalogue["brass"])

        second = create_variant(
            client, catalog_admin, catalogue["product"], sku="OVERLAP-B"
        ).json()["id"]
        assign_option_value(client, catalog_admin, second, catalogue["size"], catalogue["small"])
        response = assign_option_value(
            client, catalog_admin, second, catalogue["colour"], catalogue["black"]
        )

        assert response.status_code == 201

    def test_the_same_combination_under_a_different_product_is_allowed(
        self, client, catalog_admin, configured_product
    ):
        catalogue = configured_product
        first = create_variant(
            client, catalog_admin, catalogue["product"], sku="CROSS-A"
        ).json()["id"]
        assign_option_value(client, catalog_admin, first, catalogue["size"], catalogue["small"])

        other = product_id(client, catalog_admin, name="Other Lamp")
        other_option = create_option(client, catalog_admin, other, name="Size").json()["id"]
        other_value = create_option_value(
            client, catalog_admin, other, other_option, "Small"
        ).json()["id"]
        other_variant = create_variant(client, catalog_admin, other, sku="CROSS-B").json()["id"]

        response = assign_option_value(
            client, catalog_admin, other_variant, other_option, other_value
        )

        assert response.status_code == 201
