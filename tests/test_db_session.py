from sqlalchemy import text


def test_can_obtain_session_and_execute_select_1(db_session):
    result = db_session.execute(text("SELECT 1")).scalar_one()

    assert result == 1
