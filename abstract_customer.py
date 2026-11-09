import logging
from typing import Optional

from amocrm import Customer, Lead
from models import AbstractCustomer, db


logger = logging.getLogger(__name__)


def get_customer_json(customer: Customer) -> dict:
    return {
        'general': {
            'id': customer.id,
            'login': customer.get_field('Логин'),
            'billing_type': customer.get_field('Вид оплаты'),
            'is_robot': bool((customer.custom_fields_values or {}).get('Робот')),
            'is_nds': bool((customer.custom_fields_values or {}).get('Оплата с НДС')),
            'system': customer.get_field('Система'),
            'host': customer.get_field('Хост'),
            'name': customer.name,
        },
        'api_value': customer.as_dict(),
    }


def get_lead_json(lead: Lead) -> dict:
    return {
        'general': {
            'id': lead.id,
            'login': lead.get_field('Логин'),
            'name': lead.name,
        },
        'api_value': lead.as_dict(),
    }


def get_abccustomer_by_lead_login(lead_login: str) -> Optional[AbstractCustomer]:
    return _find_unique_login(AbstractCustomer.amo_leads, lead_login)


def get_abccustomer_by_amocustomer_login(customer_login: str) -> Optional[AbstractCustomer]:
    return _find_unique_login(AbstractCustomer.amo_customers, customer_login)


def _find_unique_login(field, login):
    groups = db.session.query(AbstractCustomer).filter(
        field.contains([{'general': {'login': login}}])
    ).limit(2).all()
    if len(groups) > 1:
        logger.warning("More than one AbstractCustomer for %s=%r", field.key, login)
        return None
    return groups[0] if groups else None