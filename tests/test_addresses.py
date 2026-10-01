"""Saved addresses: ownership, the single default, and what they are not.

Addresses are authentication-only with ownership as the authorization, for
the same reason the cart is: Phase 2 locked zero-roles-by-default, so an
``addresses.*`` permission code would refuse every newly registered
customer access to their own address book.

The other property under test here is that a saved address is a
**convenience, never a record**. Nothing in this table is the source of
truth for where an order went - that is a JSONB snapshot on the order with
no foreign key - which is what makes a saved address safe to edit or
delete. That guarantee is asserted from the order's side, in
``test_orders.py``.
"""

import uuid

from tests.order_helpers import (
    ADDRESSES,
    address_payload,
    create_address,
    delete_address,
    error_code,
    list_addresses,
    patch_address,
)


class TestCreating:
    def test_an_address_is_created_and_returned(self, authorize, client):
        headers = authorize()

        response = create_address(client, headers)

        assert response.status_code == 201
        body = response.json()
        assert body["recipient_name"] == "Ada Lovelace"
        assert body["country_code"] == "GB"
        assert body["is_default"] is False

    def test_a_country_code_is_stored_upper_case(self, authorize, client):
        headers = authorize()

        response = create_address(client, headers, country_code="gb")

        assert response.json()["country_code"] == "GB"

    def test_the_optional_fields_may_be_omitted(self, authorize, client):
        headers = authorize()
        payload = address_payload()
        for optional in ("label", "region"):
            payload.pop(optional)

        response = client.post(ADDRESSES, json=payload, headers=headers)

        assert response.status_code == 201
        assert response.json()["region"] is None

    def test_a_required_field_is_refused(self, authorize, client):
        headers = authorize()
        payload = address_payload()
        payload.pop("line1")

        response = client.post(ADDRESSES, json=payload, headers=headers)

        assert response.status_code == 422
        assert error_code(response) == "VALIDATION_ERROR"

    def test_a_country_code_of_the_wrong_length_is_refused(self, authorize, client):
        headers = authorize()

        response = create_address(client, headers, country_code="GBR")

        assert response.status_code == 422


class TestListing:
    def test_only_the_callers_own_addresses_are_listed(self, authorize, client):
        mine = authorize(email="mine@example.com")
        theirs = authorize(email="theirs@example.com")
        create_address(client, mine, recipient_name="Mine")
        create_address(client, theirs, recipient_name="Theirs")

        listed = list_addresses(client, mine).json()

        assert [row["recipient_name"] for row in listed] == ["Mine"]

    def test_an_empty_address_book_is_an_empty_list(self, authorize, client):
        assert list_addresses(client, authorize()).json() == []

    def test_listing_requires_authentication(self, client):
        response = client.get(ADDRESSES)

        assert response.status_code == 401


class TestTheSingleDefault:
    def test_an_address_can_be_created_as_the_default(self, authorize, client):
        headers = authorize()

        response = create_address(client, headers, is_default=True)

        assert response.json()["is_default"] is True

    def test_making_a_second_address_default_clears_the_first(self, authorize, client):
        headers = authorize()
        first = create_address(client, headers, is_default=True).json()["id"]
        second = create_address(client, headers, line1="9 Other Road", is_default=True).json()

        assert second["is_default"] is True
        listed = {row["id"]: row["is_default"] for row in list_addresses(client, headers).json()}
        assert listed[first] is False

    def test_promoting_an_existing_address_clears_the_old_default(self, authorize, client):
        headers = authorize()
        first = create_address(client, headers, is_default=True).json()["id"]
        second = create_address(client, headers, line1="9 Other Road").json()["id"]

        patch_address(client, headers, second, is_default=True)

        listed = {row["id"]: row["is_default"] for row in list_addresses(client, headers).json()}
        assert listed[first] is False
        assert listed[second] is True

    def test_two_customers_may_each_have_a_default(self, authorize, client):
        mine = authorize(email="a@example.com")
        theirs = authorize(email="b@example.com")

        assert create_address(client, mine, is_default=True).status_code == 201
        assert create_address(client, theirs, is_default=True).status_code == 201

    def test_the_database_refuses_two_defaults_for_one_user(self, test_engine):
        """The service clears the old default; this is the guarantee under it."""
        import pytest
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        with test_engine.begin() as connection:
            user = connection.execute(
                text(
                    "INSERT INTO users (email, password_hash) "
                    "VALUES (:e, 'x') RETURNING id"
                ),
                {"e": f"dual-default-{uuid.uuid4()}@example.com"},
            ).scalar_one()
            insert = text(
                "INSERT INTO addresses "
                "(user_id, recipient_name, line1, city, postal_code, country_code, is_default) "
                "VALUES (:u, 'A', 'B', 'C', 'D', 'GB', true)"
            )
            connection.execute(insert, {"u": user})
            with pytest.raises(IntegrityError):
                connection.execute(insert, {"u": user})


class TestUpdating:
    def test_a_field_can_be_changed(self, authorize, client):
        headers = authorize()
        address = create_address(client, headers).json()["id"]

        response = patch_address(client, headers, address, city="Manchester")

        assert response.status_code == 200
        assert response.json()["city"] == "Manchester"

    def test_untouched_fields_are_left_alone(self, authorize, client):
        headers = authorize()
        address = create_address(client, headers).json()["id"]

        response = patch_address(client, headers, address, city="Manchester")

        assert response.json()["recipient_name"] == "Ada Lovelace"

    def test_another_customers_address_reports_not_found(self, authorize, client):
        mine = authorize(email="mine2@example.com")
        theirs = authorize(email="theirs2@example.com")
        address = create_address(client, theirs).json()["id"]

        response = patch_address(client, mine, address, city="Manchester")

        assert response.status_code == 404
        assert error_code(response) == "ADDRESS_NOT_FOUND"

    def test_an_unknown_address_reports_not_found(self, authorize, client):
        response = patch_address(client, authorize(), uuid.uuid4(), city="Manchester")

        assert response.status_code == 404
        assert error_code(response) == "ADDRESS_NOT_FOUND"


class TestDeleting:
    def test_an_address_is_removed(self, authorize, client):
        headers = authorize()
        address = create_address(client, headers).json()["id"]

        assert delete_address(client, headers, address).status_code == 204
        assert list_addresses(client, headers).json() == []

    def test_another_customers_address_cannot_be_deleted(self, authorize, client):
        mine = authorize(email="mine3@example.com")
        theirs = authorize(email="theirs3@example.com")
        address = create_address(client, theirs).json()["id"]

        response = delete_address(client, mine, address)

        assert response.status_code == 404
        assert error_code(response) == "ADDRESS_NOT_FOUND"
        assert len(list_addresses(client, theirs).json()) == 1

    def test_an_unknown_address_reports_not_found(self, authorize, client):
        response = delete_address(client, authorize(), uuid.uuid4())

        assert response.status_code == 404


class TestAuthorization:
    def test_every_address_route_requires_authentication(self, client):
        assert client.get(ADDRESSES).status_code == 401
        assert client.post(ADDRESSES, json=address_payload()).status_code == 401
        assert client.patch(f"{ADDRESSES}/{uuid.uuid4()}", json={}).status_code == 401
        assert client.delete(f"{ADDRESSES}/{uuid.uuid4()}").status_code == 401

    def test_no_route_accepts_a_user_id(self, client):
        """Ownership is the authorization, so there is nothing to tamper with."""
        paths = client.app.openapi()["paths"]
        address_paths = {path: spec for path, spec in paths.items() if "/addresses" in path}

        assert address_paths, "the address routes are not registered"
        for path, spec in address_paths.items():
            assert "user_id" not in path
            for method in spec.values():
                for parameter in method.get("parameters", []):
                    assert parameter["name"] != "user_id", path
