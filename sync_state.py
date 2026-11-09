from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional


CUSTOMERS = "amo_customers"
LEADS = "amo_leads"
CONTACTS = "amo_contacts"
COMPANIES = "amo_companies"
ENTITY_FIELDS = (CUSTOMERS, LEADS, CONTACTS, COMPANIES)


@dataclass(eq=False)
class Group:
    id: Optional[int] = None
    entities: dict = field(default_factory=dict)
    seen: dict = field(default_factory=dict)
    send_methods: Optional[list] = None
    dirty: bool = False

    def values(self):
        values = {
            name: list(self.entities[name].values()) if name in self.entities else None
            for name in ENTITY_FIELDS
        }
        values["send_methods"] = self.send_methods
        return values


class SyncState:
    def __init__(self, is_full):
        self.is_full = is_full
        self.groups = set()
        self.deleted = set()
        self.by_id = {name: defaultdict(set) for name in (CUSTOMERS, LEADS)}
        self.by_login = {name: defaultdict(Counter) for name in (CUSTOMERS, LEADS)}

    def new_group(self):
        group = Group()
        self.groups.add(group)
        return group

    def load(self, row):
        group = Group(id=row["id"], send_methods=row["send_methods"])
        self.groups.add(group)
        for name in ENTITY_FIELDS:
            if row[name] is None:
                continue
            group.entities[name] = {}
            for entity in row[name]:
                self.put(group, name, entity, seen=False)
        group.dirty = self.is_full and any(
            len(group.entities[name]) != len(row[name]) for name in group.entities
        )

    def find_id(self, name, crm_id):
        groups = self.by_id[name].get(crm_id)
        if not groups:
            return None
        # Старые данные могли содержать копии одного CRM ID в разных группах.
        return min(groups, key=lambda group: (group.id is None, group.id or 0))

    def find_login(self, name, login):
        groups = self.by_login[name].get(login)
        if groups and len(groups) == 1:
            return next(iter(groups))
        return None

    def put(self, group, name, entity, *, seen=True):
        general = entity["general"]
        crm_id = general["id"]
        if type(crm_id) is not int:
            raise ValueError(f"Invalid CRM ID in {name}: {crm_id!r}")

        if seen:
            group.seen.setdefault(name, set()).add(crm_id)

        entries = group.entities.setdefault(name, {})
        previous = entries.get(crm_id)
        if previous == entity:
            return
        entries[crm_id] = entity
        group.dirty = True

        if name in self.by_id:
            self.by_id[name][crm_id].add(group)
            old_login = previous["general"].get("login") if previous else None
            login = general.get("login")
            if old_login != login:
                self._remove_login(group, name, old_login)
                if login:
                    self.by_login[name][login][group] += 1

    def _remove_login(self, group, name, login):
        if not login:
            return
        groups = self.by_login[name][login]
        groups[group] -= 1
        if not groups[group]:
            del groups[group]
        if not groups:
            del self.by_login[name][login]

    def put_related(self, group, name, rows):
        for row in rows:
            previous = group.entities.get(name, {}).get(row["id"], {})
            general = dict(previous.get("general", {}))
            for key in ("id", "name", "phone", "inn"):
                if key in row:
                    general[key] = row[key]
            # Ответ CRM может содержать только ссылку с ID.
            api_value = {**(previous.get("api_value") or {}), **row}
            self.put(group, name, {"general": general, "api_value": api_value})

    def merge(self, target, source):
        for name, entries in source.entities.items():
            source_seen = source.seen.get(name, ())
            target_entries = target.entities.get(name, {})
            target_seen = target.seen.get(name, ())
            for crm_id, entity in entries.items():
                if crm_id not in target_entries or (crm_id in source_seen and crm_id not in target_seen):
                    self.put(target, name, entity, seen=False)
                if name in self.by_id:
                    groups = self.by_id[name][crm_id]
                    groups.remove(source)
                    if not groups:
                        del self.by_id[name][crm_id]
                    self._remove_login(source, name, entity["general"].get("login"))
            if source_seen:
                target.seen.setdefault(name, set()).update(source_seen)

        methods = list(target.send_methods or ())
        for method in source.send_methods or ():
            if method not in methods:
                methods.append(method)
        if methods and methods != target.send_methods:
            target.send_methods = methods
            target.dirty = True
        self.groups.remove(source)
        if source.id is not None:
            self.deleted.add(source.id)

    def changed_groups(self):
        # После последней страницы индексы для поиска больше не нужны.
        self.by_id.clear()
        self.by_login.clear()
        for group in self.groups:
            if self.is_full:
                self._prune(group)
                if not (group.entities.get(CUSTOMERS) or group.entities.get(LEADS)):
                    if group.id is not None:
                        self.deleted.add(group.id)
                    continue
            if group.dirty:
                yield group

    @staticmethod
    def _prune(group):
        for name, entries in group.entities.items():
            seen = group.seen.get(name, ())
            retained = {}
            for crm_id, entry in entries.items():
                if crm_id not in seen:
                    continue
                title = entry["general"].get("name")
                marked = isinstance(title, str) and "удалить" in title.casefold()
                if not marked:
                    retained[crm_id] = entry
            if len(retained) != len(entries):
                group.entities[name] = retained
                group.dirty = True
        group.seen.clear()
