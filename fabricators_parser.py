#!/usr/bin/env python3
"""Standalone Fabricators registry collector. Python 3.11+. See README_RU.md.

All data lives in registry_fabricators, with no tenant or application dependency.
Online commands require the operator's agreed access reference in configuration.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import decimal
import email.utils
from portable_flock import fcntl
import hashlib
import json
import logging
import os
import pathlib
import random
import re
import signal
import sys
import time
import uuid
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup
from protego import Protego
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

VERSION = "1.0.1"
SCHEMA_VERSION = 1
SCHEMA = "registry_fabricators"
BASE = "https://fabricators.ru"
NS = uuid.UUID("ea11cd5d-67b7-4a12-a7f6-adb1d27d17bc")
LOG = logging.getLogger("fabricators")
STOP = False


def now():
    return dt.datetime.now(dt.timezone.utc)


def uid(kind, key):
    return uuid.uuid5(NS, kind + ":" + key)


def json_default(value):
    if isinstance(value, (uuid.UUID, dt.datetime, dt.date, decimal.Decimal)):
        return str(value) if not isinstance(value, (dt.datetime, dt.date)) else value.isoformat()
    raise TypeError(type(value).__name__)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=json_default)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def text_of(node):
    return node.get_text(" ", strip=True) if node else ""


def content_text(node):
    if not node:
        return None
    copy = BeautifulSoup(str(node), "html.parser")
    for x in copy.select("script,style,.label,h2,h3,h4"):
        x.decompose()
    return copy.get_text("\n", strip=True) or None


def canonical(url, base=BASE, keep_query=False):
    p = urlsplit(urljoin(base, url))
    if p.scheme not in ("https", "http") or p.username or p.password:
        return None
    if p.hostname not in ("fabricators.ru", "www.fabricators.ru") or p.port not in (None, 80, 443):
        return None
    path = re.sub("/+", "/", p.path).rstrip("/") or "/"
    if path == "/produkt":
        path = "/sitemap/produkt"  # Actual rubric linked by Fabricators, checked 2026-09-18.
    query = ""
    if keep_query:
        pairs = parse_qsl(p.query, keep_blank_values=True)
        if any(k != "page" and not k.startswith("utm_") for k, _ in pairs):
            return None
        pages = [v for k, v in pairs if k == "page"]
        if pages:
            if len(pages) != 1 or not pages[0].isdigit() or int(pages[0]) > 100000:
                return None
            if int(pages[0]):
                query = urlencode({"page": str(int(pages[0]))})
    return urlunsplit(("https", "fabricators.ru", path, query, ""))


def external_site(url):
    if not url:
        return None
    p = urlsplit(url if "://" in url else "https://" + url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return None
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not k.startswith("utm_")])
    return urlunsplit((p.scheme, p.netloc, p.path, query, ""))


def external_sites(url):
    """Return every explicit web address from a possibly combined source href."""
    parts = re.split(r"\s*,\s*(?=https?://)", url or "", flags=re.I)
    return [site for part in parts if (site := external_site(part))]


def kind_of(url):
    path = urlsplit(url).path
    for prefix, kind in (("/proizvoditel/", "company"), ("/produkt/", "product_type"),
                         ("/proizvodstvo/", "enterprise"), ("/tovar/", "product")):
        if path.startswith(prefix) and "/" not in path[len(prefix):]:
            return kind
    if path in ("/zavody", "/sitemap/produkt", "/sitemap/rubriki", "/sitemap/regionyi", "/sitemap/products"):
        return "listing"
    if path.endswith(".xml"):
        return "sitemap"
    return None


def inn_valid(s):
    if not s or not s.isdigit():
        return False
    def check(weights):
        return sum(int(s[i]) * w for i, w in enumerate(weights)) % 11 % 10
    if len(s) == 10:
        return check([2, 4, 10, 3, 5, 9, 4, 6, 8]) == int(s[9])
    if len(s) == 12:
        return (check([7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) == int(s[10]) and
                check([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) == int(s[11]))
    return False


def masked(value):
    return bool(re.search(r"[xхXХ*•]{2,}|[xхXХ]-[xхXХ]", value))


def normalize_contact(kind, value):
    if masked(value):
        return None
    value = value.strip()
    if kind == "email":
        value = value.removeprefix("mailto:").split("?")[0].strip().lower()
        return value if re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", value) else None
    value = re.split(r"(?:доб\.?|ext\.?|;ext=)", value, flags=re.I)[0]
    number = re.sub(r"\D", "", value)
    if len(number) == 11 and number.startswith("8"):
        number = "7" + number[1:]
    if len(number) == 10:
        number = "7" + number
    return "+" + number if 11 <= len(number) <= 15 else None


def phone_parts(value):
    """Split source text that contains two or more displayed phone numbers."""
    value = re.sub(r"\s+", " ", value or "").strip()
    parts = re.split(r"\s*[,;|]\s*|(?<=\d)\s+(?=(?:\+?\d{1,3}\s*)?\(\d{2,5}\))", value)
    return [part.strip() for part in parts if len(re.sub(r"\D", "", part)) >= 7]


@dataclasses.dataclass
class Config:
    # Real credentials only in DATABASE_URL, never embedded in the deliverable.
    database_env: str = "DATABASE_URL"
    access_reference: str = ""
    user_agent: str = "FabricatorsRegistryCollector/1.0"
    min_delay: float = 7.0
    max_delay: float = 13.0
    timeout: float = 35.0
    max_attempts: int = 4
    max_bytes: int = 8_000_000
    max_requests: int = 5000
    data_dir: str = "./data"
    browser_contacts: bool = False
    browser_state_env: str = "FABRICATORS_BROWSER_STATE"
    contact_wait_ms: int = 30000
    seeds: list[str] = dataclasses.field(default_factory=lambda: [BASE + "/zavody", BASE + "/sitemap/produkt", BASE + "/sitemap.xml"])
    phone_click_selector: str = ".field_phone a.view-phone"
    email_click_selector: str = ".field_email a.view-email"

    @classmethod
    def load(cls, path):
        data = json.loads(pathlib.Path(path).read_text("utf-8")) if path else {}
        result = cls(**data)
        if not 1 <= result.min_delay <= result.max_delay <= 3600:
            raise ValueError("Delay must satisfy 1 <= min_delay <= max_delay <= 3600")
        if result.max_attempts < 1 or result.max_requests < 1 or result.max_bytes < 1024:
            raise ValueError("Invalid limits")
        if result.timeout <= 0 or result.contact_wait_ms < 1000:
            raise ValueError("Invalid timeouts")
        return result


M = sa.MetaData(schema=SCHEMA)
J = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
U = sa.Uuid()
TS = sa.DateTime(timezone=True)


def col(name, typ, *args, **kw):
    return sa.Column(name, typ, *args, **kw)


def identity():
    return [col("id", U, primary_key=True), col("source_url", sa.Text, nullable=False, unique=True),
            col("source_node_id", sa.Text), col("name", sa.Text, nullable=False),
            col("first_seen_at", TS, nullable=False), col("last_seen_at", TS, nullable=False)]


versions = sa.Table("schema_versions", M, col("version", sa.Integer, primary_key=True), col("installed_at", TS, nullable=False))
companies = sa.Table("companies", M, *identity(),
    col("full_name", sa.Text), col("inn_raw", sa.Text), col("inn_normalized", sa.String(12), index=True),
    col("inn_is_valid", sa.Boolean, nullable=False, default=False), col("ogrn", sa.String(15)),
    col("kpp", sa.String(9)), col("description", sa.Text), col("address", sa.Text),
    col("legal_address", sa.Text), col("site_url", sa.Text), col("domain", sa.Text),
    col("registration_date", sa.Date), col("employee_count", sa.Integer), col("capital", sa.Numeric(24, 2)),
    col("source_updated_at", sa.Date), col("phone_status", sa.Text), col("email_status", sa.Text),
    col("record_status", sa.Text, nullable=False, default="stub"),
    col("metadata_jsonb", J, nullable=False, default=dict))
enterprise_nodes = sa.Table("enterprise_nodes", M, *identity(), col("metadata_jsonb", J, nullable=False, default=dict))
product_types = sa.Table("product_types", M, *identity(), col("metadata_jsonb", J, nullable=False, default=dict))


def relation(name, left, right, left_name, right_name):
    return sa.Table(name, M,
        col(left_name, U, sa.ForeignKey(left.c.id), primary_key=True),
        col(right_name, U, sa.ForeignKey(right.c.id), primary_key=True, index=True),
        col("source_url", sa.Text, nullable=False), col("observed_at", TS, nullable=False))


company_enterprises = relation("company_enterprises", companies, enterprise_nodes, "company_id", "enterprise_id")
company_product_types = relation("company_product_types", companies, product_types, "company_id", "product_type_id")
enterprise_edges = relation("enterprise_edges", enterprise_nodes, enterprise_nodes, "parent_id", "child_id")
product_type_edges = relation("product_type_edges", product_types, product_types, "parent_id", "child_id")
products = sa.Table("products", M, *identity(),
    col("company_id", U, sa.ForeignKey(companies.c.id), nullable=False, index=True),
    col("description", sa.Text), col("price_text", sa.Text), col("price", sa.Numeric(24, 2)),
    col("currency", sa.String(8)), col("source_updated_at", sa.Date),
    col("record_status", sa.Text, nullable=False, default="stub"), col("metadata_jsonb", J, nullable=False, default=dict))
product_type_links = relation("product_type_links", products, product_types, "product_id", "product_type_id")
goods_categories = sa.Table("goods_categories", M, *identity(), col("metadata_jsonb", J, nullable=False, default=dict))
product_category_links = relation("product_category_links", products, goods_categories, "product_id", "category_id")
contacts = sa.Table("contacts", M, col("id", U, primary_key=True),
    col("company_id", U, sa.ForeignKey(companies.c.id), nullable=False, index=True),
    col("contact_type", sa.String(16), nullable=False), col("raw_value", sa.Text, nullable=False),
    col("normalized_value", sa.Text, nullable=False), col("source_url", sa.Text, nullable=False),
    col("extraction_method", sa.Text, nullable=False), col("validation_status", sa.Text, nullable=False),
    col("first_seen_at", TS, nullable=False), col("last_seen_at", TS, nullable=False),
    col("is_current", sa.Boolean, nullable=False, default=True),
    sa.UniqueConstraint("company_id", "contact_type", "normalized_value"))
company_sites = sa.Table("company_sites", M, col("id", U, primary_key=True),
    col("company_id", U, sa.ForeignKey(companies.c.id), nullable=False, index=True),
    col("site_url", sa.Text, nullable=False), col("domain", sa.Text), col("label", sa.Text),
    col("source_url", sa.Text, nullable=False), col("validation_status", sa.Text, nullable=False),
    col("is_primary", sa.Boolean, nullable=False, default=False),
    col("first_seen_at", TS, nullable=False), col("last_seen_at", TS, nullable=False),
    col("is_current", sa.Boolean, nullable=False, default=True),
    sa.UniqueConstraint("company_id", "site_url"))
snapshots = sa.Table("page_snapshots", M, col("id", U, primary_key=True),
    col("source_url", sa.Text, nullable=False, index=True), col("kind", sa.Text, nullable=False),
    col("fetched_at", TS, nullable=False), col("content_hash", sa.String(64), nullable=False),
    col("parser_version", sa.Text, nullable=False), col("payload_jsonb", J, nullable=False))
queue = sa.Table("crawl_queue", M, col("url", sa.Text, primary_key=True), col("kind", sa.Text, nullable=False),
    col("owner_url", sa.Text), col("status", sa.Text, nullable=False), col("attempts", sa.Integer, nullable=False),
    col("priority", sa.Integer, nullable=False), col("available_at", TS, nullable=False),
    col("last_error", sa.Text), col("updated_at", TS, nullable=False))
runs = sa.Table("crawl_runs", M, col("id", U, primary_key=True), col("started_at", TS, nullable=False),
    col("finished_at", TS), col("status", sa.Text, nullable=False), col("requests", sa.Integer, nullable=False),
    col("processed", sa.Integer, nullable=False), col("access_reference", sa.Text), col("summary_jsonb", J))
issues = sa.Table("issues", M, col("id", U, primary_key=True), col("source_url", sa.Text, nullable=False),
    col("code", sa.Text, nullable=False), col("detail", sa.Text), col("created_at", TS, nullable=False))


class Database:
    def __init__(self, url):
        self.engine = sa.create_engine(url, pool_pre_ping=True)
        if self.engine.dialect.name == "sqlite":
            self.engine = self.engine.execution_options(schema_translate_map={SCHEMA: None})
            @sa.event.listens_for(self.engine, "connect")
            def foreign_keys(conn, record):
                conn.execute("PRAGMA foreign_keys=ON")
        elif self.engine.dialect.name != "postgresql":
            raise ValueError("Use PostgreSQL (production) or SQLite (tests)")

    def init(self):
        with self.engine.begin() as c:
            if self.engine.dialect.name == "postgresql":
                c.execute(sa.schema.CreateSchema(SCHEMA, if_not_exists=True))
            M.create_all(c)
            present = c.execute(sa.select(versions.c.version)).scalars().all()
            if present and present != [SCHEMA_VERSION]:
                raise ValueError("Schema version mismatch; apply an explicit migration")
            self.upsert(c, versions, {"version": SCHEMA_VERSION, "installed_at": now()}, update=False)

    def upsert(self, c, table, values, update=True):
        build = pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert
        stmt = build(table).values(**values)
        keys = [x.name for x in table.primary_key]
        changes = {k: stmt.excluded[k] for k in values if k not in keys and k not in ("first_seen_at", "installed_at")}
        stmt = stmt.on_conflict_do_update(index_elements=keys, set_=changes) if update and changes else stmt.on_conflict_do_nothing(index_elements=keys)
        c.execute(stmt)

    def upsert_unique(self, c, table, values, keys):
        build = pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert
        stmt = build(table).values(**values)
        changes = {k: stmt.excluded[k] for k in values
                   if k not in set(keys) | {"id", "first_seen_at", "installed_at"}}
        c.execute(stmt.on_conflict_do_update(index_elements=list(keys), set_=changes))

    def issue(self, c, url, code, detail=""):
        self.upsert(c, issues, {"id": uid("issue", url + code + detail), "source_url": url,
            "code": code, "detail": detail, "created_at": now()})

    def stub(self, c, table, url, name=None, owner=None):
        ident = uid(table.name, url)
        row = dict(id=ident, source_url=url, name=name or urlsplit(url).path.rsplit("/", 1)[-1],
                   first_seen_at=now(), last_seen_at=now(), metadata_jsonb={})
        if table is products:
            row["company_id"] = owner
        self.upsert(c, table, row, update=False)
        return ident

    def enqueue(self, c, url, kind=None, owner=None):
        url = canonical(url, keep_query=True)
        kind = kind or (kind_of(url) if url else None)
        if not url or not kind:
            return
        self.upsert(c, queue, dict(url=url, kind=kind, owner_url=owner, status="pending", attempts=0,
            # Lower values are claimed first.  Discovery pages must precede
            # detail cards so the queue reaches its complete, stable size
            # before the long tail of company/product requests is processed.
            priority={"sitemap": 0, "listing": 5, "company": 10, "product": 20}.get(kind, 40),
            available_at=now(), updated_at=now()), update=False)
        if owner:
            c.execute(queue.update().where(queue.c.url == url, queue.c.owner_url.is_(None)).values(owner_url=owner))

    def save(self, c, record):
        kind, url = record["kind"], record["source_url"]
        snapshot_payload = {k: v for k, v in record.items() if k != "fetched_at"}
        h = digest(dumps(snapshot_payload))
        self.upsert(c, snapshots, dict(id=uid("snapshot", url + h), source_url=url, kind=kind,
            fetched_at=now(), content_hash=h, parser_version=VERSION, payload_jsonb=json.loads(dumps(snapshot_payload))), update=False)
        for warning in record.get("warnings", []):
            self.issue(c, url, "parse_warning", warning)
        if kind == "company":
            ident = self.stub(c, companies, url, record["name"])
            values = {k: v for k, v in record["fields"].items() if k in companies.c}
            values.update(id=ident, name=record["name"], source_url=url, source_node_id=record.get("source_node_id"),
                          first_seen_at=now(), last_seen_at=now(), record_status="parsed", metadata_jsonb=record["metadata"])
            self.upsert(c, companies, values)
            for key, target, rel, fk in (("enterprise_nodes", enterprise_nodes, company_enterprises, "enterprise_id"),
                                        ("product_types", product_types, company_product_types, "product_type_id")):
                # Replace only the association field actually present in a successful card.
                if key in record["present"]:
                    c.execute(rel.delete().where(rel.c.company_id == ident))
                for item in record[key]:
                    other = self.stub(c, target, item["url"], item["name"])
                    self.upsert(c, rel, {"company_id": ident, fk: other, "source_url": url, "observed_at": now()})
                    self.enqueue(c, item["url"])
            for contact_kind in ("phone", "email"):
                status = record["fields"].get(contact_kind + "_status")
                if status in ("public", "revealed", "empty"):
                    c.execute(contacts.update().where(contacts.c.company_id == ident,
                        contacts.c.contact_type == contact_kind).values(is_current=False))
            for item in record["contacts"]:
                self.upsert_unique(c, contacts, dict(id=uid("contact", str(ident) + item["kind"] + item["normalized"]),
                    company_id=ident, contact_type=item["kind"], raw_value=item["raw"], normalized_value=item["normalized"],
                    source_url=url, extraction_method=item["method"], validation_status="format_valid_unverified",
                    first_seen_at=now(), last_seen_at=now(), is_current=True),
                    ("company_id", "contact_type", "normalized_value"))
            if "sites" in record["present"]:
                c.execute(company_sites.update().where(company_sites.c.company_id == ident).values(is_current=False))
                for pos, site_url in enumerate(record["metadata"].get("site_links", [])):
                    self.upsert_unique(c, company_sites, dict(
                        id=uid("company_site", str(ident) + site_url), company_id=ident,
                        site_url=site_url, domain=urlsplit(site_url).hostname, label=None,
                        source_url=url, validation_status="format_valid_unverified",
                        is_primary=pos == 0, first_seen_at=now(), last_seen_at=now(), is_current=True),
                        ("company_id", "site_url"))
            for item in record["products"]:
                old = c.execute(sa.select(products.c.company_id).where(products.c.source_url == item["url"])).scalar_one_or_none()
                if old and old != ident:
                    self.issue(c, item["url"], "owner_conflict", "Product linked from multiple company cards")
                    continue
                self.stub(c, products, item["url"], item["name"], ident)
                self.enqueue(c, item["url"], "product", url)
        elif kind in ("enterprise", "product_type"):
            table, edge = (enterprise_nodes, enterprise_edges) if kind == "enterprise" else (product_types, product_type_edges)
            self.stub(c, table, url, record["name"])
            self.upsert(c, table, dict(id=uid(table.name, url), source_url=url, name=record["name"],
                source_node_id=record.get("source_node_id"), first_seen_at=now(), last_seen_at=now(), metadata_jsonb=record["metadata"]))
            chain = record.get("ancestors", []) + [{"url": url, "name": record["name"]}]
            for parent, child in zip(chain, chain[1:]):
                a = self.stub(c, table, parent["url"], parent["name"])
                b = self.stub(c, table, child["url"], child["name"])
                if a != b:
                    self.upsert(c, edge, dict(parent_id=a, child_id=b, source_url=url, observed_at=now()))
        elif kind == "product":
            owner_url = record.get("owner_url")
            if not owner_url:
                self.issue(c, url, "owner_missing", "Cannot save a product without a company")
                return
            owner = self.stub(c, companies, owner_url)
            existing = c.execute(sa.select(products.c.company_id).where(products.c.source_url == url)).scalar_one_or_none()
            if existing and existing != owner:
                self.issue(c, url, "owner_conflict", "Stored owner differs from product page")
                return
            ident = self.stub(c, products, url, record["name"], owner)
            self.upsert(c, products, dict(id=ident, source_url=url, name=record["name"], company_id=owner,
                source_node_id=record.get("source_node_id"), first_seen_at=now(), last_seen_at=now(),
                record_status="parsed", metadata_jsonb=record["metadata"], **record["fields"]))
            c.execute(product_type_links.delete().where(product_type_links.c.product_id == ident))
            for item in record["product_types"]:
                other = self.stub(c, product_types, item["url"], item["name"])
                self.upsert(c, product_type_links, dict(product_id=ident, product_type_id=other, source_url=url, observed_at=now()))
            c.execute(product_category_links.delete().where(product_category_links.c.product_id == ident))
            for item in record["metadata"].get("goods_categories", []):
                if item["url"] and urlsplit(item["url"]).path.startswith("/tovary/"):
                    other = self.stub(c, goods_categories, item["url"], item["name"])
                    self.upsert(c, product_category_links, dict(product_id=ident, category_id=other, source_url=url, observed_at=now()))
            self.enqueue(c, owner_url, "company")
        for item in record.get("discovered", []):
            self.enqueue(c, item["url"], item.get("kind"), item.get("owner_url"))


def field(soup, cls):
    return soup.select_one("." + cls)


def links(node, kind, base):
    result = {}
    if node:
        for a in node.select("a[href]"):
            url = canonical(a["href"], base)
            name = text_of(a)
            if url and kind_of(url) == kind and name:
                result[url] = {"url": url, "name": name}
    return list(result.values())


def field_values(soup):
    result = {}
    for node in soup.select(".fd-node"):
        names = [x for x in node.get("class", []) if x.startswith("field_")]
        for name in names:
            if name not in result:
                result[name] = {"label": text_of(node.select_one(".label")), "text": content_text(node)}
    return result


def source_node_id(soup):
    s = soup.select_one('script[data-drupal-selector="drupal-settings-json"]')
    if s:
        try:
            path = json.loads(s.string or s.get_text()).get("path", {}).get("currentPath", "")
            match = re.fullmatch(r"(?:node|taxonomy/term)/(\d+)", path)
            if match:
                return match[1]
        except (ValueError, TypeError):
            pass
    return None


def date_value(value):
    m = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\b", value or "")
    if m:
        try:
            return dt.datetime.strptime(m[1], "%d.%m.%Y").date()
        except ValueError:
            pass
    return None


def number_value(value):
    m = re.search(r"\d[\d\s\u00a0]*(?:[.,]\d{1,2})?", value or "")
    if not m:
        return None
    return decimal.Decimal(re.sub(r"\s", "", m[0]).replace(",", "."))


def collect_contacts(soup, kind, method="html"):
    block = field(soup, "field_" + kind)
    if not block:
        return [], "missing"
    values = []
    for line in block.select(".line") or [block]:
        selector = 'a[href^="tel:"]' if kind == "phone" else 'a[href^="mailto:"]'
        anchors = line.select(selector)
        sources = [text_of(line)] if not anchors else []
        for anchor in anchors:
            visible = text_of(anchor)
            href = anchor["href"].split(":", 1)[1].split("?")[0]
            visible_parts = (phone_parts(visible) if kind == "phone" else
                re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", visible))
            sources.extend([visible] if any(normalize_contact(kind, x) for x in visible_parts) else [href])
        for source in sources:
            candidates = phone_parts(source) if kind == "phone" else re.findall(
                r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", source)
            for raw in candidates:
                normalized = normalize_contact(kind, raw)
                if normalized:
                    values.append({"kind": kind, "raw": raw, "normalized": normalized, "method": method})
    dedupe = {x["normalized"]: x for x in values}
    if dedupe:
        status = "partial" if masked(text_of(block)) else ("revealed" if method == "browser_click" else "public")
        return list(dedupe.values()), status
    return [], "masked" if masked(text_of(block)) else ("empty" if not content_text(block) else "unresolved")


def discover(soup, url, kind):
    main = soup.select_one("main") or soup
    out = {}
    for a in main.select("a[href]"):
        target = canonical(a["href"], url, keep_query=True)
        if not target or target == url:
            continue
        target_kind = kind_of(target)
        is_pager = a.find_parent(class_=re.compile("pager|pagination")) is not None
        if is_pager and urlsplit(target).path == urlsplit(url).path:
            out[target] = {"url": target, "kind": kind}
        elif kind in ("listing", "product_type", "enterprise") and target_kind in ("company", "product_type", "enterprise"):
            out[target] = {"url": target, "kind": target_kind}
    return list(out.values())


class ParseError(Exception):
    pass


def parse_html(html, url, kind=None, owner_hint=None):
    soup = BeautifulSoup(html, "html.parser")
    requested_url = canonical(url, keep_query=True)
    url = canonical(url)
    kind = kind or kind_of(url)
    h1 = soup.select_one("h1")
    if not h1:
        raise ParseError("No h1: possible challenge, error page or changed layout")
    can = soup.select_one('link[rel="canonical"][href]')
    target = canonical(can["href"], url) if can else None
    if target and kind_of(target) == kind:
        url = target
    name = text_of(h1)
    if kind in ("product_type", "enterprise"):
        crumb = soup.select_one(".breadcrumb li:last-of-type [itemprop='name']")
        name = text_of(crumb) or name
    record = dict(kind=kind, source_url=url, name=name, source_node_id=source_node_id(soup),
                  metadata={}, fields={}, warnings=[], discovered=discover(soup, requested_url, kind))
    fields = field_values(soup)
    # Keep all requested union sections, not only the first one; never render raw HTML in a UI.
    record["metadata"] = {"fields": fields, "union_blocks": [
        {"text": text_of(x), "html": str(x)} for x in soup.select(".fd-node-union")],
        "parser_version": VERSION, "source": "fabricators", "requested_url": requested_url}
    all_text = text_of(soup.select_one("main") or soup)
    match = re.search(r"Последнее обновление\s*(\d{2}\.\d{2}\.\d{4})", all_text)
    updated = date_value(match[1]) if match else None
    if kind == "company":
        if not soup.select_one(".field_about_company,.field_enterprise,.field_industrial_products,.field_inn,.field_address"):
            raise ParseError("Company structure not recognized")
        record.update(enterprise_nodes=links(field(soup, "field_enterprise"), "enterprise", url),
                      product_types=links(field(soup, "field_industrial_products"), "product_type", url),
                      products=links(soup.select_one("#catalog"), "product", url), contacts=[], present=[])
        for cls, key in (("field_enterprise", "enterprise_nodes"), ("field_industrial_products", "product_types")):
            if field(soup, cls) is not None:
                record["present"].append(key)
        result = {"source_updated_at": updated}
        mappings = {"description": "field_about_company", "address": "field_address", "full_name": "field_jur_lico",
                    "legal_address": "field_legal_address", "inn_raw": "field_inn", "ogrn": "field_ogrn", "kpp": "field_kpp"}
        for dest, cls in mappings.items():
            if field(soup, cls) is not None:
                result[dest] = content_text(field(soup, cls))
        raw_inn = re.sub(r"\D", "", result.get("inn_raw") or "")
        if raw_inn:
            result.update(inn_is_valid=inn_valid(raw_inn), inn_normalized=raw_inn if inn_valid(raw_inn) else None)
            if not result["inn_is_valid"]:
                record["warnings"].append("invalid_inn_checksum")
        for key, lengths in (("ogrn", (13, 15)), ("kpp", (9,))):
            if key in result:
                val = re.sub(r"\D", "", result[key] or "")
                result[key] = val if len(val) in lengths else None
        for cls in ("field_registration_date", "field_data_registracii", "field_date_reg"):
            if field(soup, cls):
                result["registration_date"] = date_value(content_text(field(soup, cls)))
        for item in fields.values():
            if "дата регистрации" in item["label"].lower():
                result["registration_date"] = date_value(item["text"])
        if field(soup, "field_people"):
            n = number_value(content_text(field(soup, "field_people")))
            result["employee_count"] = int(n) if n is not None else None
        if field(soup, "field_ustavnoy_kapital"):
            result["capital"] = number_value(content_text(field(soup, "field_ustavnoy_kapital")))
        site_field = field(soup, "field_site")
        site_links = [link for a in soup.select(".field_site a[href]") for link in external_sites(a["href"])]
        record["metadata"]["site_links"] = list(dict.fromkeys(site_links))
        if site_field is not None:
            record["present"].append("sites")
        if record["metadata"]["site_links"]:
            result["site_url"] = record["metadata"]["site_links"][0]
            result["domain"] = urlsplit(result["site_url"]).hostname if result["site_url"] else None
        for contact_kind in ("phone", "email"):
            values, status = collect_contacts(soup, contact_kind)
            record["contacts"].extend(values)
            result[contact_kind + "_status"] = status
        record["fields"] = result
        if not record["product_types"] and "product_types" not in record["present"]:
            record["warnings"].append("product_type_field_missing")
    elif kind in ("enterprise", "product_type"):
        record["ancestors"] = links(soup.select_one(".breadcrumb"), kind, url)
        record["ancestors"] = [x for x in record["ancestors"] if x["url"] != url]
        # No company→type assertion is inferred from a possibly hierarchical listing.
    elif kind == "product":
        # Scope fields to the primary article: recommendations contain other owners/prices.
        soup = soup.select_one("article.node-product.node-full") or soup
        if not soup.select_one(".field_goods_text,.field_description,.field_about_product,.field_body,.field_opisanie,.field--name-body,.field_rel_company"):
            raise ParseError("Product structure not recognized")
        product_owner = links(soup.select_one(".field_rel_company,.field_company,.field_proizvoditel,.field_manufacturer"), "company", url)
        candidates = {x["url"] for x in product_owner}
        if owner_hint and len(candidates) == 1 and owner_hint not in candidates:
            raise ParseError("Product owner conflicts with company catalog")
        record["owner_url"] = owner_hint or (next(iter(candidates)) if len(candidates) == 1 else None)
        if not record["owner_url"]:
            record["warnings"].append("product_owner_missing_or_ambiguous")
        description = soup.select_one(".field_goods_text,.field_description,.field_about_product,.field_body,.field_opisanie,.field--name-body")
        if not description:
            heading = soup.find(["h2", "h3"], string=re.compile("О товаре|Описание"))
            if heading:
                chunks = []
                for sibling in heading.next_siblings:
                    if getattr(sibling, "name", None) in ("h2", "h3", "h4"):
                        break
                    if getattr(sibling, "name", None):
                        chunks.append(str(sibling))
                description = BeautifulSoup("".join(chunks), "html.parser")
        price_node = soup.select_one(".field_goods_cost,.field_price,[itemprop='price'],.price")
        price_text = (text_of(price_node) or price_node.get("content")) if price_node else None
        record["fields"] = dict(description=content_text(description), price_text=price_text,
                                 price=number_value(price_text), currency="RUB" if price_text and re.search("руб|₽|RUB", price_text, re.I) else None,
                                 source_updated_at=updated)
        record["product_types"] = links(soup.select_one(".field_industrial_products"), "product_type", url)
        record["metadata"]["page_text"] = text_of(soup.select_one("main") or soup)[:100000]
        record["metadata"]["images"] = [urljoin(url, x["href"]) for x in soup.select(".field_goods_photo a[href]")]
        record["metadata"]["goods_categories"] = [
            {"name": text_of(a), "url": canonical(a["href"], url)}
            for a in soup.select(".field_cat3level a[href]")]
        record["metadata"]["price_qualifier"] = "from" if price_text and re.search(r"\bот\b", price_text, re.I) else None
        if not record["fields"]["description"]:
            record["warnings"].append("product_description_missing_check_metadata")
    return record


class StopRun(Exception):
    """Global pause: operator review or request budget; never evade it."""


class RobotsDenied(Exception):
    pass


class RetryPage(Exception):
    pass


class Gone(Exception):
    pass


def interruptible_wait(seconds):
    end = time.monotonic() + max(0, seconds)
    while time.monotonic() < end:
        if STOP:
            raise StopRun("signal")
        # The clock advances between the loop condition and this calculation.
        # Clamp again so a scheduler pause cannot produce sleep(-epsilon).
        time.sleep(max(0, min(0.25, end - time.monotonic())))


def retry_after(value, default=900):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        try:
            d = email.utils.parsedate_to_datetime(value)
            return max(1, (d - now()).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return default


def allowed_origin(url):
    try:
        p = urlsplit(url)
        return p.scheme == "https" and p.hostname in ("fabricators.ru", "www.fabricators.ru") and p.port in (None, 443) and not p.username and not p.password
    except ValueError:
        return False


class RateLimiter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.path = pathlib.Path(cfg.data_dir) / "rate_state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.requests = 0
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}

    def persist(self):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(dumps(self.state), "utf-8")
        temp.replace(self.path)

    def before(self, paced=True):
        if STOP:
            raise StopRun("signal")
        if self.requests >= self.cfg.max_requests:
            raise StopRun("request_budget")
        pause = self.state.get("cooldown_until", 0) - time.time()
        if pause > 0:
            raise StopRun(f"cooldown_active_seconds={int(pause)}")
        if paced:
            interruptible_wait(self.state.get("next_at", 0) - time.time())
            self.state["next_at"] = time.time() + random.uniform(self.cfg.min_delay, self.cfg.max_delay)
            self.persist()
        self.requests += 1

    def cooldown(self, seconds):
        self.state["cooldown_until"] = time.time() + seconds
        self.persist()


class Network:
    def __init__(self, cfg, transport=None):
        self.cfg = cfg
        self.rate = RateLimiter(cfg)
        self.client = httpx.Client(headers={"User-Agent": cfg.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml,text/xml;q=0.9"},
            timeout=cfg.timeout, follow_redirects=False, transport=transport, trust_env=False)
        self.robots = None
        self.robots_loaded_at = 0.0

    def close(self):
        self.client.close()

    def status(self, code, headers):
        if code in (401, 403):
            raise StopRun(f"http_{code}_access_denied")
        if code == 429:
            seconds = retry_after(headers.get("retry-after"))
            self.rate.cooldown(seconds)
            raise StopRun(f"http_429_cooldown_seconds={int(seconds)}")
        if code in (404, 410):
            raise Gone(str(code))
        if code in (408, 425) or code >= 500:
            raise RetryPage(f"http_{code}")
        if code >= 400:
            raise ParseError(f"http_{code}")

    def load_robots(self):
        try:
            text, _ = self.get(BASE + "/robots.txt", robots_request=True)
        except Gone:
            raise StopRun("robots_unavailable: inspect before collecting")
        if "<html" in text[:1000].lower():
            raise StopRun("robots_returned_html")
        self.robots = Protego.parse(text)
        self.robots_loaded_at = time.time()
        (pathlib.Path(self.cfg.data_dir) / "robots.txt").write_text(text, "utf-8")
        delay = self.robots.crawl_delay(self.cfg.user_agent)
        if delay:
            self.cfg.min_delay = max(float(delay), self.cfg.min_delay)
            self.cfg.max_delay = max(self.cfg.min_delay, self.cfg.max_delay)

    def permits(self, url):
        if not allowed_origin(url):
            return False
        return self.robots is not None and self.robots.can_fetch(url, self.cfg.user_agent)

    def get(self, url, robots_request=False):
        for _ in range(6):
            if not allowed_origin(url):
                raise StopRun("redirect_outside_allowed_origin")
            if not robots_request and not self.permits(url):
                raise RobotsDenied(url)
            self.rate.before()
            try:
                with self.client.stream("GET", url) as response:
                    self.status(response.status_code, response.headers)
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise ParseError("redirect_without_location")
                        url = urljoin(url, location)
                        continue
                    media = response.headers.get("content-type", "").lower()
                    if not robots_request and not any(x in media for x in ("html", "xml", "text/plain")):
                        raise ParseError("unexpected_content_type")
                    buf = bytearray()
                    for chunk in response.iter_bytes():
                        buf.extend(chunk)
                        if len(buf) > self.cfg.max_bytes:
                            raise ParseError("response_size_limit")
                    text = bytes(buf).decode(response.encoding or "utf-8", errors="replace")
                    if not robots_request and looks_blocked(text):
                        raise StopRun("challenge_or_login_page")
                    return text, url
            except httpx.HTTPError as exc:
                raise RetryPage(type(exc).__name__) from exc
        raise ParseError("redirect_limit")


def looks_blocked(html):
    soup = BeautifulSoup(html, "html.parser")
    title = text_of(soup.title).lower()
    return bool(soup.select_one("#challenge-form,#cf-challenge-running,.g-recaptcha,.h-captcha") or
                any(s in title for s in ("just a moment", "access denied", "проверка безопасности")) or
                (soup.select_one('form input[type="password"]') and not soup.select_one(".field_about_company,.field_goods_text")))


class BrowserContacts:
    """Use ordinary visible controls only. No private endpoint assumptions or mask decoding."""
    def __init__(self, cfg, network):
        from playwright.sync_api import sync_playwright
        self.cfg, self.network = cfg, network
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=True)
        state = os.environ.get(cfg.browser_state_env)
        kwargs = {"user_agent": cfg.user_agent, "locale": "ru-RU", "service_workers": "block"}
        if state:
            kwargs["storage_state"] = state
        self.context = self.browser.new_context(**kwargs)
        self.context.set_default_timeout(cfg.contact_wait_ms)
        self.fatal = None
        self.denied = False
        self.context.route("**/*", self.route)
        self.context.on("response", self.response)

    def route(self, route):
        req = route.request
        if self.fatal or req.resource_type in ("image", "media", "font") or not allowed_origin(req.url):
            route.abort()
            return
        if not self.network.permits(req.url):
            self.denied = True
            route.abort()
            return
        try:
            self.network.rate.before(paced=req.resource_type in ("document", "xhr", "fetch"))
        except StopRun as exc:
            self.fatal = exc
            route.abort()
            return
        route.continue_()

    def response(self, response):
        if response.status in (401, 403, 429):
            try:
                self.network.status(response.status, response.headers)
            except StopRun as exc:
                self.fatal = exc

    def reveal(self, record):
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        self.fatal, self.denied = None, False
        page = self.context.new_page()
        try:
            page.goto(record["source_url"], wait_until="domcontentloaded", timeout=int(self.cfg.timeout * 1000))
            if self.fatal:
                raise self.fatal
            if looks_blocked(page.content()):
                raise StopRun("browser_challenge_or_login")
            for kind, selector in (("phone", self.cfg.phone_click_selector), ("email", self.cfg.email_click_selector)):
                if record["fields"].get(kind + "_status") not in ("masked", "unresolved", "partial"):
                    continue
                controls = page.locator(selector)
                # A click can replace the complete block. Re-read locators each time.
                for index in range(min(controls.count(), 20)):
                    if index >= controls.count():
                        break
                    control = controls.nth(index)
                    if not control.is_visible() or not masked(control.inner_text()):
                        continue
                    self.network.rate.before()  # Also pace clicks served without XHR.
                    try:
                        control.click(timeout=self.cfg.contact_wait_ms, no_wait_after=True)
                        page.wait_for_function("""({selector}) => {
                          const es = [...document.querySelectorAll(selector)];
                          return es.length === 0 || es.every(e => !/[xхXХ*•]{2,}|[xхXХ]-[xхXХ]/.test(e.textContent));
                        }""", arg={"selector": selector}, timeout=self.cfg.contact_wait_ms)
                    except PlaywrightTimeout:
                        pass  # Unrevealed is a data status, not fabricated contact content.
                    if self.fatal:
                        raise self.fatal
                html = page.content()
                if looks_blocked(html) or page.locator('input[type="password"]:visible').count():
                    record["fields"][kind + "_status"] = "auth_required"
                    continue
                found, status = collect_contacts(BeautifulSoup(html, "html.parser"), kind, "browser_click")
                if found:
                    record["contacts"] = [x for x in record["contacts"] if x["kind"] != kind] + found
                record["fields"][kind + "_status"] = status if found else ("robots_blocked" if self.denied else "unresolved")
            if self.fatal:
                raise self.fatal
        except StopRun:
            raise
        except Exception as exc:
            if self.fatal:
                raise self.fatal
            record["warnings"].append("browser_contact_error:" + type(exc).__name__)
        finally:
            page.close()
        return record

    def close(self):
        self.context.close()
        self.browser.close()
        self.pw.stop()


def parse_sitemap(xml):
    if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise ParseError("XML_entities_disallowed")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ParseError("invalid_sitemap_xml") from exc
    if root.tag.rsplit("}", 1)[-1] not in ("sitemapindex", "urlset"):
        raise ParseError("unexpected_sitemap_root")
    result = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] == "loc" and node.text:
            url = canonical(node.text.strip(), keep_query=True)
            if url and kind_of(url):
                result.append(url)
    return list(dict.fromkeys(result))


@contextlib.contextmanager
def exclusive(db, cfg):
    directory = pathlib.Path(cfg.data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "collector.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StopRun("another_worker_in_data_directory") from exc
        with db.engine.connect() as c:
            pg = db.engine.dialect.name == "postgresql"
            acquired = not pg or c.execute(sa.text("SELECT pg_try_advisory_lock(172913001, 1)")).scalar()
            if not acquired:
                raise StopRun("another_worker_in_database")
            try:
                yield
            finally:
                if pg:
                    c.execute(sa.text("SELECT pg_advisory_unlock(172913001, 1)"))
                fcntl.flock(lock, fcntl.LOCK_UN)


def require_access(cfg):
    if not cfg.access_reference.strip() or cfg.access_reference.startswith("REPLACE"):
        raise ValueError("Set access_reference to the actual agreed access reference before online collection; offline parsing is available")


def crawl(db, cfg, limit):
    require_access(cfg)
    network, browser = Network(cfg), None
    run_id = uuid.uuid4()
    processed, outcome, reason = 0, "complete", "queue_empty"
    try:
        with exclusive(db, cfg):
            with db.engine.begin() as c:
                c.execute(queue.update().where(queue.c.status == "running").values(status="pending", updated_at=now()))
                c.execute(runs.insert().values(id=run_id, started_at=now(), status="running", requests=0,
                    processed=0, access_reference=cfg.access_reference))
            network.load_robots()
            if cfg.browser_contacts:
                browser = BrowserContacts(cfg, network)
            while processed < limit and not STOP:
                if time.time() - network.robots_loaded_at > 86400:
                    network.load_robots()
                with db.engine.begin() as c:
                    row = c.execute(sa.select(queue).where(queue.c.status.in_(["pending", "retry"]),
                        queue.c.available_at <= now()).order_by(queue.c.priority, queue.c.url).limit(1)).mappings().first()
                    if row:
                        c.execute(queue.update().where(queue.c.url == row["url"]).values(status="running", updated_at=now()))
                if not row:
                    with db.engine.connect() as c:
                        pending = c.execute(sa.select(sa.func.count()).select_from(queue).where(queue.c.status.in_(["pending", "retry"]))).scalar()
                    reason = "delayed_items_pending" if pending else "queue_empty"
                    break
                url, attempt = row["url"], row["attempts"] + 1
                state, error, available = "done", None, now()
                halt = None
                try:
                    html, final_url = network.get(url)
                    final_kind = kind_of(canonical(final_url) or "")
                    if final_kind != row["kind"]:
                        raise ParseError("redirect_changed_page_kind")
                    if row["kind"] == "sitemap":
                        with db.engine.begin() as c:
                            for target in parse_sitemap(html):
                                db.enqueue(c, target)
                    else:
                        record = parse_html(html, final_url, row["kind"], row["owner_url"])
                        if browser and row["kind"] == "company" and any(record["fields"].get(k + "_status") in ("masked", "unresolved", "partial") for k in ("phone", "email")):
                            try:
                                record = browser.reveal(record)
                            except StopRun:
                                # Preserve permitted public fields already obtained, then pause the run.
                                with db.engine.begin() as c:
                                    db.save(c, record)
                                raise
                        with db.engine.begin() as c:
                            db.save(c, record)
                except RobotsDenied:
                    state, error = "robots_denied", "robots_disallow"
                except Gone:
                    state, error = "gone", "http_404_or_410"
                    table = companies if row["kind"] == "company" else products if row["kind"] == "product" else None
                    if table is not None:
                        with db.engine.begin() as c:
                            c.execute(table.update().where(table.c.source_url == canonical(url)).values(record_status="gone"))
                except RetryPage as exc:
                    state = "retry" if attempt < cfg.max_attempts else "failed"
                    error = str(exc)
                    available = now() + dt.timedelta(seconds=min(3600, 30 * 2 ** (attempt - 1)) + random.uniform(0, 15))
                except ParseError as exc:
                    state, error = "failed", str(exc)
                except StopRun as exc:
                    error, halt = str(exc), exc
                    state = "blocked" if any(x in error for x in ("denied", "challenge", "login", "outside")) else "pending"
                    attempt = row["attempts"]
                except Exception:
                    # Restore claim after any database/program failure, with no contact/credential logging.
                    with db.engine.begin() as c:
                        c.execute(queue.update().where(queue.c.url == url).values(status="pending", updated_at=now()))
                    raise
                with db.engine.begin() as c:
                    c.execute(queue.update().where(queue.c.url == url).values(status=state, attempts=attempt,
                        available_at=available, last_error=error, updated_at=now()))
                processed += 1
                LOG.info("page=%s status=%s requests=%d", url, state, network.rate.requests)
                if halt:
                    raise halt
            else:
                reason = "signal" if STOP else "page_limit"
                outcome = "paused"
    except StopRun as exc:
        outcome, reason = "paused", str(exc)
    except Exception as exc:
        outcome, reason = "error", type(exc).__name__
        raise
    finally:
        if browser:
            browser.close()
        network.close()
        with db.engine.begin() as c:
            c.execute(runs.update().where(runs.c.id == run_id).values(finished_at=now(), status=outcome,
                requests=network.rate.requests, processed=processed, summary_jsonb={"reason": reason}))
    return {"status": outcome, "reason": reason, "processed": processed, "requests": network.rate.requests}


PUBLIC_TABLES = [companies, enterprise_nodes, product_types, goods_categories, company_enterprises,
    company_product_types, enterprise_edges, product_type_edges, products, product_type_links,
    product_category_links, contacts, company_sites]
EXPORT_TABLES = PUBLIC_TABLES + [snapshots, issues]
EXPORT_TABLES = [t for t in M.sorted_tables if t in EXPORT_TABLES]


def export_bundle(db, directory):
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "manifest.json").exists():
        raise ValueError("Export directory already contains a manifest; choose a new directory")
    manifest = {"schema": SCHEMA, "schema_version": SCHEMA_VERSION, "parser_version": VERSION,
                "exported_at": now().isoformat(), "tables": []}
    # Consistent snapshot across tables, suitable for FK-preserving migration.
    with db.engine.connect() as c:
        if db.engine.dialect.name == "postgresql":
            c = c.execution_options(isolation_level="REPEATABLE READ")
        with c.begin():
            for table in EXPORT_TABLES:
                path = directory / (table.name + ".jsonl")
                checksum, count = hashlib.sha256(), 0
                with path.open("wb") as f:
                    result = c.execution_options(stream_results=True).execute(sa.select(table))
                    for row in result.mappings():
                        line = (dumps(dict(row)) + "\n").encode("utf-8")
                        f.write(line)
                        checksum.update(line)
                        count += 1
                manifest["tables"].append({"name": table.name, "file": path.name, "rows": count, "sha256": checksum.hexdigest()})
    (directory / "manifest.json").write_text(dumps(manifest) + "\n", "utf-8")
    return manifest


def decode_row(table, row):
    if set(row) - set(table.c.keys()):
        raise ValueError("Unknown import columns")
    for column in table.c:
        val = row.get(column.name)
        if val is None:
            continue
        if isinstance(column.type, sa.Uuid):
            row[column.name] = uuid.UUID(val)
        elif isinstance(column.type, sa.DateTime):
            row[column.name] = dt.datetime.fromisoformat(val)
        elif isinstance(column.type, sa.Date):
            row[column.name] = dt.date.fromisoformat(val)
        elif isinstance(column.type, sa.Numeric):
            row[column.name] = decimal.Decimal(val)
    return row


def import_bundle(db, directory):
    directory = pathlib.Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    if manifest.get("schema") != SCHEMA or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Import schema mismatch")
    entries = {x["name"]: x for x in manifest["tables"]}
    if set(entries) != {t.name for t in EXPORT_TABLES} or len(entries) != len(manifest["tables"]):
        raise ValueError("Incomplete/duplicate/unknown tables in import")
    # Verify every checksum before opening a write transaction.
    for table in EXPORT_TABLES:
        info = entries[table.name]
        if info["file"] != table.name + ".jsonl":
            raise ValueError("Invalid bundle path")
        h, count = hashlib.sha256(), 0
        with (directory / info["file"]).open("rb") as f:
            for line in f:
                h.update(line)
                count += 1
        if h.hexdigest() != info["sha256"] or count != info["rows"]:
            raise ValueError("Import checksum/count mismatch: " + table.name)
    # Initial migrations only. Refuse merge into populated data to avoid stale associations.
    with db.engine.begin() as c:
        if any(c.execute(sa.select(sa.func.count()).select_from(t)).scalar() for t in EXPORT_TABLES):
            raise ValueError("Import target registry must be empty; use pg_dump or a reviewed update migration for refreshes")
        for table in EXPORT_TABLES:
            with (directory / entries[table.name]["file"]).open("r", encoding="utf-8") as f:
                batch = []
                for line in f:
                    batch.append(decode_row(table, json.loads(line)))
                    if len(batch) >= 500:
                        c.execute(table.insert(), batch)
                        batch.clear()
                if batch:
                    c.execute(table.insert(), batch)
    return {"imported_rows": sum(x["rows"] for x in entries.values())}


def schema_sql():
    dialect = postgresql.dialect()
    parts = [f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}";']
    for table in M.sorted_tables:
        parts.append(str(sa.schema.CreateTable(table).compile(dialect=dialect)).strip() + ";")
        for index in sorted(table.indexes, key=lambda x: x.name):
            parts.append(str(sa.schema.CreateIndex(index).compile(dialect=dialect)).strip() + ";")
    parts.append(f"INSERT INTO {SCHEMA}.schema_versions(version, installed_at) VALUES ({SCHEMA_VERSION}, CURRENT_TIMESTAMP);")
    return "\n\n".join(parts) + "\n"


def status(db):
    with db.engine.connect() as c:
        return {"tables": {t.name: c.execute(sa.select(sa.func.count()).select_from(t)).scalar() for t in EXPORT_TABLES},
                "queue": dict(c.execute(sa.select(queue.c.status, sa.func.count()).group_by(queue.c.status)).all())}


def main(argv=None):
    global STOP
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="JSON configuration, default: built-in offline-safe settings")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create the dedicated registry schema")
    sub.add_parser("schema", help="Print PostgreSQL DDL; no database required")
    p = sub.add_parser("seed", help="Queue defaults or specific URLs; no network requests")
    p.add_argument("urls", nargs="*")
    p = sub.add_parser("crawl", help="Run one sequential resumable worker")
    p.add_argument("--max-pages", type=int, default=500)
    sub.add_parser("status")
    p = sub.add_parser("requeue", help="Explicitly reschedule selected queue status")
    p.add_argument("--status", required=True, choices=["failed", "blocked", "robots_denied", "gone", "done", "retry"])
    p.add_argument("--kind", choices=["company", "product", "enterprise", "product_type", "listing", "sitemap"])
    p = sub.add_parser("parse-file", help="Parse saved HTML without network access")
    p.add_argument("path")
    p.add_argument("--url", required=True)
    p.add_argument("--owner-url")
    p.add_argument("--save", action="store_true", help="Also save parsed data to DATABASE_URL")
    p.add_argument("--output", help="Write JSON to this path instead of stdout")
    for name in ("export", "import"):
        sub.add_parser(name).add_argument("directory")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Third-party request logs can expose cookies/URLs; application logs never print credentials.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = Config.load(args.config)
    if args.command == "schema":
        print(schema_sql(), end="")
        return 0
    if args.command == "parse-file":
        url = canonical(args.url, keep_query=True)
        if not url or not kind_of(url):
            raise ValueError("Unsupported source URL")
        record = parse_html(pathlib.Path(args.path).read_text("utf-8"), url, owner_hint=canonical(args.owner_url) if args.owner_url else None)
        output = dumps(record) + "\n"
        if args.output:
            pathlib.Path(args.output).write_text(output, "utf-8")
        else:
            print(output, end="")
        if not args.save:
            return 0
    database_url = os.environ.get(cfg.database_env)
    if not database_url:
        raise ValueError("Missing environment variable " + cfg.database_env)
    db = Database(database_url)
    db.init()
    if args.command == "init":
        result = {"schema": SCHEMA, "version": SCHEMA_VERSION}
    elif args.command == "parse-file":
        with exclusive(db, cfg), db.engine.begin() as c:
            db.save(c, record)
        return 0
    elif args.command == "seed":
        urls = args.urls or cfg.seeds
        with exclusive(db, cfg), db.engine.begin() as c:
            for url in urls:
                normalized = canonical(url, keep_query=True)
                if not normalized or not kind_of(normalized):
                    raise ValueError("Unsupported seed URL")
                db.enqueue(c, normalized)
        result = {"seeded": len(urls)}
    elif args.command == "crawl":
        if args.max_pages < 1:
            raise ValueError("max-pages must be positive")
        def stop(signum, frame):
            global STOP
            STOP = True
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        result = crawl(db, cfg, args.max_pages)
    elif args.command == "status":
        result = status(db)
    elif args.command == "requeue":
        with exclusive(db, cfg), db.engine.begin() as c:
            condition = queue.c.status == args.status
            if args.kind:
                condition = sa.and_(condition, queue.c.kind == args.kind)
            changed = c.execute(queue.update().where(condition).values(status="pending", attempts=0,
                available_at=now(), last_error=None, updated_at=now())).rowcount
        result = {"requeued": changed}
    elif args.command == "export":
        with exclusive(db, cfg):
            result = export_bundle(db, args.directory)
    else:
        with exclusive(db, cfg):
            result = import_bundle(db, args.directory)
    print(dumps(result))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, StopRun, ParseError) as exc:
        LOG.error("%s", str(exc))
        sys.exit(2)
    except Exception as exc:
        # No DSN, SQL parameter dumps or extracted contact content in log output.
        LOG.error("Fatal %s. Check configuration/database; do not publish credentials in support logs.", type(exc).__name__)
        sys.exit(1)
