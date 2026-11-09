from sqlalchemy import column, delete, insert, select, text, update, values
from sqlalchemy.orm import Session

from abstract_customer import get_customer_json, get_lead_json
from amocrm import COMPANIES, CONTACTS, Customer, Lead, amocrm_get
from models import AbstractCustomer, db
from sync_state import COMPANIES as AMO_COMPANIES
from sync_state import CONTACTS as AMO_CONTACTS
from sync_state import CUSTOMERS, ENTITY_FIELDS, LEADS, SyncState


BATCH_SIZE = 500
WRITE_FIELDS = (*ENTITY_FIELDS, "send_methods")
SYNC_LOCK = 0x414D4F43524D


def collect_leads_customers(is_full=True, *, fetch=None):
    """Синхронизировать две выгрузки CRM в одной транзакции Flask-приложения."""
    if fetch is None:
        fetch = amocrm_get
    table = AbstractCustomer.__table__
    state = SyncState(is_full)

    with Session(db.engine) as session, session.begin():
        # Координирует параллельные вызовы этого сборщика в одной БД.
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SYNC_LOCK})
        snapshot = select(table.c.id, *(table.c[name] for name in ENTITY_FIELDS), table.c.send_methods)
        with session.execute(snapshot.execution_options(yield_per=BATCH_SIZE)) as rows:
            for row in rows.mappings():
                state.load(row)

        for customer in fetch(Customer, _with=f"{COMPANIES},{CONTACTS}"):
            if not is_full and customer.status_id == 111349:
                continue
            login = customer.get_field("Логин")
            if not login:
                continue
            group = state.find_id(CUSTOMERS, customer.id) or state.find_login(LEADS, login)
            if group is None:
                group = state.new_group()
            state.put(group, CUSTOMERS, get_customer_json(customer))
            state.put_related(group, AMO_CONTACTS, customer.contacts)
            state.put_related(group, AMO_COMPANIES, customer.companies)

        for lead in fetch(Lead, _with=CONTACTS):
            if not is_full and lead.status_id == 143:
                continue
            by_id = state.find_id(LEADS, lead.id)
            by_login = state.find_login(CUSTOMERS, lead.get_field("Логин"))
            group = by_id or by_login
            if group is None:
                group = state.new_group()
            if by_id is not None and by_login is not None and by_id is not by_login:
                state.merge(by_id, by_login)
            state.put(group, LEADS, get_lead_json(lead))
            state.put_related(group, AMO_CONTACTS, lead.contacts)
            state.put_related(group, AMO_COMPANIES, lead.companies)

        _save_changes(session, table, state)


def _save_changes(session, table, state):
    inserts = []
    updates = []
    for group in state.changed_groups():
        payload = group.values()
        if group.id is None:
            inserts.append(payload)
            if len(inserts) == BATCH_SIZE:
                session.execute(insert(table).values(inserts))
                inserts.clear()
        else:
            updates.append((group.id, *(payload[name] for name in WRITE_FIELDS)))
            if len(updates) == BATCH_SIZE:
                _update_batch(session, table, updates)
                updates.clear()

    if inserts:
        session.execute(insert(table).values(inserts))
    if updates:
        _update_batch(session, table, updates)

    batch = []
    for group_id in state.deleted:
        batch.append(group_id)
        if len(batch) == BATCH_SIZE:
            session.execute(delete(table).where(table.c.id.in_(batch)))
            batch.clear()
    if batch:
        session.execute(delete(table).where(table.c.id.in_(batch)))


def _update_batch(session, table, rows):
    changes = values(
        column("id", table.c.id.type),
        *(column(name, table.c[name].type) for name in WRITE_FIELDS),
        name="changes",
    ).data(rows)
    session.execute(
        update(table).where(table.c.id == changes.c.id).values({
            name: changes.c[name] for name in WRITE_FIELDS
        })
    )