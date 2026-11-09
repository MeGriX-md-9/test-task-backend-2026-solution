import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from flask import Flask
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.schema import CreateSchema, DropSchema

from abstract_customer import get_customer_json
from amocrm import Customer, Lead
from main import collect_leads_customers
from models import AbstractCustomer, db


DATABASE_URL = os.getenv("TEST_DATABASE_URL")


def buyer(crm_id, login, *, name="Покупатель", status=0, contacts=(), companies=()):
    return Customer(
        id=crm_id, name=name, status_id=status,
        next_price=None, next_date=None, responsible_user_id=None,
        periodicity=None, created_by=None, updated_by=None, created_at=None,
        updated_at=None, closest_task_at=None, is_deleted=False, ltv=None,
        purchases_count=None, average_check=None, account_id=None,
        custom_fields_values={"Логин": {"value": login}} if login else None,
        _embedded={"contacts": list(contacts), "companies": list(companies)},
    )


def deal(crm_id, login=None, *, name="Сделка", status=0, contacts=()):
    return Lead(
        id=crm_id, name=name, status_id=status,
        custom_fields_values={"Логин": {"value": login}} if login else None,
        _embedded={"contacts": list(contacts)},
    )


def record(crm_id, login=None, name=None):
    return {"general": {"id": crm_id, "login": login, "name": name}, "api_value": {}}


@unittest.skipUnless(DATABASE_URL, "Set TEST_DATABASE_URL for PostgreSQL integration tests")
class SyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        schema = f"sync_test_{uuid4().hex}"
        engine = create_engine(DATABASE_URL)
        cls.addClassCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))

        def drop_test_schema():
            with engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))

        cls.addClassCleanup(drop_test_schema)
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI=DATABASE_URL,
            SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"options": f"-c search_path={schema}"}},
        )
        db.init_app(cls.app)
        with cls.app.app_context():
            cls.addClassCleanup(db.engine.dispose)
            db.create_all()

    def setUp(self):
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        db.session.execute(delete(AbstractCustomer))
        db.session.commit()

    def seed(self, **payload):
        group = AbstractCustomer(**payload)
        db.session.add(group)
        db.session.flush()
        group_id = group.id
        db.session.commit()
        return group_id

    def rows(self):
        return db.session.scalars(select(AbstractCustomer).order_by(AbstractCustomer.id)).all()

    def sync(self, buyers=(), deals=(), *, full=True):
        def fetch(entity, **kwargs):
            return iter(buyers if entity is Customer else deals)
        collect_leads_customers(full, fetch=fetch)

    def test_merge_preserves_lead_group_and_applies_current_data(self):
        self.seed(
            amo_customers=[record(1, "org"), record(2, "old")],
            amo_contacts=[record(101, name="Old contact name"), record(102)],
            amo_companies=[record(201, name="Company from previous run"), record(202)],
            send_methods=[{"type": "mail", "value": "buyer@example.test"}],
        )
        winner = self.seed(
            amo_leads=[record(3, "old", "Old deal name"), record(4)],
            amo_contacts=[record(104)],
        )
        self.seed(amo_customers=[record(8, "unseen", "УДАЛИТЬ")])
        self.sync(
            buyers=[buyer(1, "org", contacts=[{"id": 101, "name": "New contact name"}],
                          companies=[{"id": 201}])],
            deals=[deal(3, "org", name="New deal name", contacts=[{"id": 103}]),
                   deal(5, "org", name=None)],
        )
        row, = self.rows()
        self.assertEqual(row.id, winner)
        self.assertEqual([e["general"]["id"] for e in row.amo_customers], [1])
        self.assertEqual({e["general"]["id"]: e["general"]["name"] for e in row.amo_leads},
                         {3: "New deal name", 5: None})
        self.assertEqual({e["general"]["id"]: e["general"].get("name") for e in row.amo_contacts},
                         {101: "New contact name", 103: None})
        self.assertEqual(row.amo_companies[0]["general"]["name"], "Company from previous run")
        self.assertEqual(len(row.amo_companies), 1)
        self.assertEqual(row.send_methods, [{"type": "mail", "value": "buyer@example.test"}])

    def test_customer_prefers_id_and_only_uses_unique_login_group(self):
        same = self.seed(amo_leads=[record(11, "same"), record(12, "same")])
        self.seed(amo_leads=[record(13, "ambiguous")])
        self.seed(amo_leads=[record(14, "ambiguous")])
        by_id = self.seed(amo_customers=[record(23, "old")])
        self.sync(
            buyers=[buyer(21, "same"), buyer(22, "ambiguous"), buyer(23, "same")],
            full=False,
        )
        owners = {
            item["general"]["id"]: row.id for row in self.rows() for item in row.amo_customers or []
        }
        self.assertEqual(owners[21], same)
        self.assertEqual(owners[23], by_id)
        self.assertNotEqual(owners[22], same)
        self.assertEqual(len(self.rows()), 5)

    def test_changed_login_updates_index_and_ambiguous_buyer_login_is_ignored(self):
        match = self.seed(amo_customers=[record(1, "old")])
        self.seed(amo_customers=[record(2, "ambiguous")])
        self.seed(amo_customers=[record(3, "ambiguous")])
        self.sync(
            buyers=[buyer(1, "new")],
            deals=[deal(31, "old"), deal(32, "new"), deal(33, "ambiguous")],
            full=False,
        )
        owners = {
            item["general"]["id"]: row.id for row in self.rows() for item in row.amo_leads or []
        }
        self.assertEqual(owners[32], match)
        self.assertNotEqual(owners[31], match)
        self.assertEqual(len(self.rows()), 5)

    def test_full_skips_loginless_buyer_and_removes_marked_entities(self):
        self.seed(amo_customers=[record(1, "old")])
        self.sync(
            buyers=[buyer(1, None), buyer(2, "org", name="к Удалить"),
                    buyer(3, "other", contacts=[{"id": 9, "name": "УДАЛИТЬ"}])],
            deals=[deal(4), deal(5, "other", name="Удалить сделку")],
        )
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        buyer_row, = [row for row in rows if row.amo_customers]
        self.assertEqual(buyer_row.amo_customers[0]["general"]["id"], 3)
        self.assertEqual(buyer_row.amo_contacts, [])
        lead_row, = [row for row in rows if row.amo_leads]
        self.assertEqual(lead_row.amo_leads[0]["general"]["id"], 4)

    def test_full_collapses_legacy_duplicates_even_if_payload_is_unchanged(self):
        customer = buyer(1, "current")
        self.seed(amo_customers=[record(1, "old"), get_customer_json(customer)])
        self.sync(buyers=[customer])
        row, = self.rows()
        self.assertEqual(row.amo_customers, [get_customer_json(customer)])

    def test_partial_merge_does_not_replace_current_buyer_with_a_legacy_copy(self):
        self.seed(amo_customers=[record(1, "old")])
        winner = self.seed(amo_customers=[record(1, "outdated")], amo_leads=[record(2)])
        current = buyer(1, "new")
        self.sync(buyers=[current], deals=[deal(2, "new")], full=False)
        row, = self.rows()
        self.assertEqual(row.id, winner)
        self.assertEqual(row.amo_customers, [get_customer_json(current)])

    def test_partial_ignores_closed_entities_and_leaves_stale_entities(self):
        old = self.seed(amo_customers=[record(1, "kept", "Удалить")])
        self.sync(
            buyers=[buyer(2, "closed", status=111349)],
            deals=[deal(3, status=143)],
            full=False,
        )
        row, = self.rows()
        self.assertEqual(row.id, old)

    def test_crm_failure_keeps_original_data(self):
        original = self.seed(amo_customers=[record(1, "original")])

        def broken_deals():
            yield deal(3, "new")
            raise TimeoutError("CRM page unavailable")

        with self.assertRaises(TimeoutError):
            self.sync(buyers=[buyer(2, "new")], deals=broken_deals())
        row, = self.rows()
        self.assertEqual(row.id, original)
        self.assertEqual(row.amo_customers, [record(1, "original")])

    def test_failure_during_cleanup_rolls_back_previous_writes(self):
        original = self.seed(amo_customers=[record(1, "original")])
        calls = []

        def fail_delete(conn, cursor, statement, parameters, context, executemany):
            calls.append(statement.split()[0])
            if statement.startswith("DELETE"):
                raise RuntimeError("forced failure before cleanup")

        engine = db.engine
        event.listen(engine, "before_cursor_execute", fail_delete)
        try:
            with self.assertRaisesRegex(RuntimeError, "forced failure"):
                self.sync(buyers=[buyer(2, "new")])
        finally:
            event.remove(engine, "before_cursor_execute", fail_delete)
        self.assertIn("INSERT", calls)
        row, = self.rows()
        self.assertEqual(row.id, original)

    def test_writes_are_batched_and_unchanged_groups_are_not_rewritten(self):
        inputs = [buyer(i, f"buyer-{i}") for i in range(1, 6)]
        statements = []

        def count_statements(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement.split()[0])

        engine = db.engine
        event.listen(engine, "before_cursor_execute", count_statements)
        try:
            with patch("main.BATCH_SIZE", 2):
                self.sync(buyers=inputs)
                self.assertEqual(statements, ["SELECT", "SELECT", "INSERT", "INSERT", "INSERT"])
                statements.clear()
                changed = [buyer(i, f"buyer-{i}", name="Changed") for i in range(1, 6)]
                self.sync(buyers=changed)
                self.assertEqual(statements, ["SELECT", "SELECT", "UPDATE", "UPDATE", "UPDATE"])
                statements.clear()
                self.sync(buyers=changed)
                self.assertEqual(statements, ["SELECT", "SELECT"])
        finally:
            event.remove(engine, "before_cursor_execute", count_statements)


if __name__ == "__main__":
    unittest.main()