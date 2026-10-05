"""
ResellerLink Order Manager — Streamlit + Supabase

Pages
  1. Dashboard            – courier pickup banner, KPIs, search, filters, order cards.
                            Update status: Pending → Shipped (handed to courier) → Received.
  2. Submit Order         – reseller order form with validation and auto-correction.
  3. Transaction History  – every past order with filters, totals, timeline and CSV export.
  4. Resellers            – register resellers with validation and auto-correction.

Storage
  • Supabase (PostgreSQL) when SUPABASE_URL and SUPABASE_KEY are set in Streamlit secrets,
    so data persists across sessions and restarts.
  • Otherwise a local SQLite file (resellerlink.db), handy for testing on your own computer.

Run:  pip install -r requirements.txt
      streamlit run app.py
"""

from __future__ import annotations

import csv
import difflib
import io
import random
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import requests
import streamlit as st

TZ = ZoneInfo("Asia/Manila")
MINUTES_PER_PICK = 4.5  # estimated packing time per pending order

st.set_page_config(page_title="ResellerLink Order Manager", page_icon="📦", layout="centered")


# ═════════════════════════════ Time helpers ═══════════════════════════════
def now() -> datetime:
    return datetime.now(TZ)


def to_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def from_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(TZ)


# ═════════════════════════ Validation & auto-correction ═══════════════════
KNOWN_CITIES = [
    "Pasig City", "Taguig City", "Makati City", "Quezon City", "Manila", "Mandaluyong City",
    "Pasay City", "Parañaque City", "Las Piñas City", "Muntinlupa City", "Marikina City",
    "San Juan City", "Caloocan City", "Valenzuela City", "Malabon City", "Navotas City",
    "Bacoor", "Imus", "Dasmariñas", "Santa Rosa", "Calamba", "Biñan", "Antipolo",
    "Cebu City", "Davao City", "Baguio City", "Iloilo City",
]
KNOWN_PROVINCES = ["Metro Manila", "Cavite", "Laguna", "Rizal", "Bulacan", "Batangas", "Pampanga", "Cebu", "Davao del Sur"]
EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "mymail.mapua.edu.ph"]
ADDRESS_ABBREVIATIONS = {
    "st": "Street", "st.": "Street", "str": "Street", "str.": "Street",
    "ave": "Avenue", "ave.": "Avenue", "av": "Avenue", "av.": "Avenue",
    "rd": "Road", "rd.": "Road", "blvd": "Boulevard", "blvd.": "Boulevard",
    "brgy": "Brgy.", "brgy.": "Brgy.", "bgy": "Brgy.", "bgy.": "Brgy.", "barangay": "Brgy.",
    "blk": "Block", "blk.": "Block", "lt": "Lot", "lt.": "Lot",
    "sta": "Santa", "sta.": "Santa", "sto": "Santo", "sto.": "Santo",
    "subd": "Subdivision", "subd.": "Subdivision", "bldg": "Building", "bldg.": "Building",
}


def _place_aliases() -> dict[str, str]:
    aliases = {}
    for place in KNOWN_CITIES + KNOWN_PROVINCES:
        low = place.lower()
        aliases[low] = place
        aliases[low.replace(" city", "")] = place
        aliases[low.replace("ñ", "n")] = place
        aliases[low.replace("ñ", "n").replace(" city", "")] = place
    return aliases


PLACE_ALIASES = _place_aliases()


@dataclass
class Check:
    """Result of validating one field: the auto-corrected value, or an error."""
    value: str
    ok: bool
    error: str = ""
    notes: list[str] = field(default_factory=list)


def squeeze(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


ROMAN_NUMERALS = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}


def capitalize_word(word: str) -> str:
    def cap(part: str) -> str:
        if part.lower() in ROMAN_NUMERALS and part.lower() != "i":
            return part.upper()                      # Molino III
        if len(part) > 2 and part[1] == "'":
            return part[0].upper() + "'" + part[2].upper() + part[3:].lower()   # O'Neil
        return part[:1].upper() + part[1:].lower()
    return "-".join(cap(p) for p in word.split("-"))


def check_name(raw: str, label: str = "Name") -> Check:
    text = squeeze(raw)
    if not text:
        return Check("", False, f"{label} is required.")
    if re.search(r"\d", text):
        return Check(text, False, f"{label} can't contain numbers.")
    notes = []
    cleaned = squeeze(re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ .'\-]", "", text))
    if cleaned != text:
        notes.append("removed numbers/symbols")
    fixed = " ".join(capitalize_word(w) for w in cleaned.split(" "))
    if fixed != cleaned:
        notes.append("fixed capitalization")
    if len(fixed.split()) < 2:
        return Check(fixed, False, f"Enter the full {label.lower()} (first and last name).", notes)
    return Check(fixed, True, notes=notes)


def check_mobile(raw: str) -> Check:
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return Check("", False, "Mobile number is required.")
    if digits.startswith("63") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if not re.fullmatch(r"9\d{9}", digits):
        return Check(raw, False, "Enter a valid PH mobile number, e.g. 0917 552 8904.")
    return Check(f"+63 {digits[:3]} {digits[3:6]} {digits[6:]}", True, notes=["standardized to +63 format"])


def check_email(raw: str, required: bool = False) -> Check:
    text = (raw or "").strip().lower().replace(" ", "")
    if not text:
        return Check("", not required, "Email is required." if required else "")
    if text.count("@") != 1:
        return Check(text, False, "Email must contain one @, e.g. name@gmail.com.")
    local, domain = text.split("@")
    domain = domain.strip(".")
    notes = []
    match = difflib.get_close_matches(domain, EMAIL_DOMAINS, n=1, cutoff=0.8)
    if match and match[0] != domain:
        notes.append(f"corrected domain {domain} → {match[0]}")
        domain = match[0]
    fixed = f"{local}@{domain}"
    if not re.fullmatch(r"[a-z0-9._%+\-]+", local) or not re.fullmatch(r"[a-z0-9.\-]+\.[a-z]{2,}", domain):
        return Check(fixed, False, "Enter a valid email, e.g. name@gmail.com.", notes)
    return Check(fixed, True, notes=notes)


def match_place(part: str) -> Optional[str]:
    key = part.lower().strip()
    if key in PLACE_ALIASES:
        return PLACE_ALIASES[key]
    close = difflib.get_close_matches(key, list(PLACE_ALIASES), n=1, cutoff=0.85)
    return PLACE_ALIASES[close[0]] if close else None


def check_address(raw: str) -> Check:
    text = squeeze(raw)
    if not text:
        return Check("", False, "Address is required.")
    parts = [p.strip() for p in text.split(",") if p.strip()]
    fixed_parts, city, notes = [], None, []
    for part in parts:
        words = []
        for w in part.split(" "):
            key = w.lower()
            if key in ADDRESS_ABBREVIATIONS:
                words.append(ADDRESS_ABBREVIATIONS[key])
            elif any(ch.isdigit() for ch in w):
                words.append(w.upper() if re.fullmatch(r"\d+[a-zA-Z]", w) else w)
            else:
                words.append(capitalize_word(w))
        fixed = " ".join(words)
        if not any(ch.isdigit() for ch in fixed) and not fixed.startswith("Brgy."):
            place = match_place(fixed)
            if place:
                if place != fixed:
                    notes.append(f"{fixed} → {place}")
                fixed = place
                if place in KNOWN_CITIES:
                    city = place
        fixed_parts.append(fixed)
    if not city:  # "Lahug, Cebu" → the province name is used as the city (Cebu City)
        for i, part in enumerate(fixed_parts):
            if f"{part} City" in KNOWN_CITIES:
                fixed_parts[i] = city = f"{part} City"
                notes.append(f"{part} → {city}")
                break
    value = ", ".join(fixed_parts)
    if len(fixed_parts) < 2 or not city:
        return Check(value, False,
                     "Include the street and the city, separated by commas "
                     "(e.g. 24 Orchid St, Brgy San Antonio, Pasig).", notes)
    return Check(value, True, notes=notes)


# ═════════════════════════════ Data model ═════════════════════════════════
STATUSES = ["Pending", "Shipped", "Received"]


@dataclass
class Product:
    sku: str
    name: str
    unit_price: float
    stock: int

    def in_stock(self, qty: int = 1) -> bool:
        return self.stock >= qty


@dataclass
class Reseller:
    reseller_id: str
    name: str
    tier: str
    mobile: str = ""
    email: str = ""
    address: str = ""


@dataclass
class Order:
    order_id: str
    reseller: Reseller
    sku: str
    product_name: str
    qty: int
    unit_price: float
    recipient: str
    mobile: str
    address: str
    courier: str
    created_at: datetime
    status: str = "Pending"
    label_printed: bool = False
    tracking_no: Optional[str] = None
    shipped_at: Optional[datetime] = None
    received_at: Optional[datetime] = None

    @property
    def total(self) -> float:
        return self.qty * self.unit_price

    def mark_shipped(self, when: datetime) -> dict:
        """Hand the order to the courier. Returns the fields to save."""
        prefix = "JT" if self.courier == "J&T Express" else "LBC"
        self.status, self.shipped_at = "Shipped", when
        self.tracking_no = self.tracking_no or f"{prefix}{random.randint(10**9, 10**10 - 1)}"
        return {"status": self.status, "shipped_at": to_iso(when), "tracking_no": self.tracking_no}

    def mark_received(self, when: datetime) -> dict:
        """Customer received the parcel. Returns the fields to save."""
        self.status, self.received_at = "Received", when
        return {"status": self.status, "received_at": to_iso(when)}


# ═════════════════════════════ Storage layer ══════════════════════════════
TABLES = {"products": "sku", "resellers": "reseller_id", "orders": "order_id"}


class StoreError(Exception):
    pass


class SupabaseStore:
    """Talks to Supabase through its REST API (PostgREST)."""
    label = "Supabase (online database)"

    def __init__(self, url: str, key: str):
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {"apikey": key, "Content-Type": "application/json"}
        if not key.startswith("sb_"):          # legacy anon/service JWT keys also need the Bearer header
            self.headers["Authorization"] = f"Bearer {key}"

    def _call(self, method: str, table: str, params=None, json=None, prefer=None):
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        try:
            r = requests.request(method, f"{self.base}/{table}", params=params, json=json,
                                 headers=headers, timeout=15)
        except requests.RequestException as e:
            raise StoreError(f"Could not reach Supabase: {e}") from e
        if r.status_code >= 400:
            raise StoreError(f"Supabase error {r.status_code} on {table}: {r.text[:300]}")
        return r.json() if r.text else None

    def select(self, table: str) -> list[dict]:
        return self._call("GET", table, params={"select": "*"}) or []

    def insert(self, table: str, rows: list[dict]) -> None:
        self._call("POST", table, json=rows, prefer="return=minimal")

    def update(self, table: str, key_value: str, fields: dict) -> None:
        self._call("PATCH", table, params={TABLES[table]: f"eq.{key_value}"}, json=fields, prefer="return=minimal")

    def delete_all(self, table: str) -> None:
        self._call("DELETE", table, params={TABLES[table]: "not.is.null"})


class SQLiteStore:
    """Local fallback: a single SQLite file next to app.py."""
    label = "SQLite (local file)"
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS products (sku TEXT PRIMARY KEY, name TEXT NOT NULL,
        unit_price REAL NOT NULL, stock INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS resellers (reseller_id TEXT PRIMARY KEY, name TEXT NOT NULL,
        tier TEXT NOT NULL DEFAULT 'New', mobile TEXT, email TEXT, address TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY, reseller_id TEXT, sku TEXT,
        product_name TEXT, qty INTEGER NOT NULL, unit_price REAL NOT NULL, recipient TEXT, mobile TEXT,
        address TEXT, courier TEXT, status TEXT NOT NULL DEFAULT 'Pending', label_printed INTEGER DEFAULT 0,
        tracking_no TEXT, created_at TEXT, shipped_at TEXT, received_at TEXT);
    """

    def __init__(self, path: str = "resellerlink.db"):
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(self.SCHEMA)

    def select(self, table: str) -> list[dict]:
        assert table in TABLES
        with self.lock:
            return [dict(r) for r in self.conn.execute(f"SELECT * FROM {table}")]

    def insert(self, table: str, rows: list[dict]) -> None:
        assert table in TABLES
        with self.lock, self.conn:
            for row in rows:
                cols = ", ".join(row)
                marks = ", ".join("?" for _ in row)
                self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))

    def update(self, table: str, key_value: str, fields: dict) -> None:
        assert table in TABLES
        sets = ", ".join(f"{c} = ?" for c in fields)
        with self.lock, self.conn:
            self.conn.execute(f"UPDATE {table} SET {sets} WHERE {TABLES[table]} = ?",
                              [*fields.values(), key_value])

    def delete_all(self, table: str) -> None:
        assert table in TABLES
        with self.lock, self.conn:
            self.conn.execute(f"DELETE FROM {table}")


def _secret(name: str) -> str:
    try:
        return str(st.secrets.get(name, "") or "")
    except Exception:  # no secrets file at all
        return ""


@st.cache_resource
def _supabase_store(url: str, key: str):
    return SupabaseStore(url, key)


@st.cache_resource
def _sqlite_store():
    return SQLiteStore()


def get_store():
    # Not cached as a whole: if the secrets were missing on one run, the app must still
    # switch to Supabase as soon as they appear (instead of staying stuck on SQLite).
    url, key = _secret("SUPABASE_URL"), _secret("SUPABASE_KEY")
    if url and key:
        return _supabase_store(url, key)
    return _sqlite_store()


# ═════════════════════════════ Reference / seed data ══════════════════════
COURIERS = ["J&T Express", "LBC"]
PICKUP_SCHEDULE = [
    ("J&T Express", time(10, 0)), ("LBC", time(11, 30)), ("J&T Express", time(14, 0)),
    ("LBC", time(16, 0)), ("J&T Express", time(17, 30)),
]
ADDRESS_DIRECTORY = [
    "24 Orchid Street, Brgy. San Antonio, Pasig City, Metro Manila 1605",
    "24 Orchid Street, Brgy. Pembo, Taguig City, Metro Manila 1642",
    "Unit 402 Solstice Tower, Brgy. Poblacion, Makati City, Metro Manila 1210",
    "Block 12 Lot 5 Camella Homes, Brgy. Molino III, Bacoor, Cavite 4102",
    "88 Katipunan Avenue, Brgy. Loyola Heights, Quezon City, Metro Manila 1108",
    "15 Rizal Street, Brgy. Poblacion, Santa Rosa, Laguna 4026",
    "Unit 7B Avida Towers, Brgy. Lahug, Cebu City, Cebu 6000",
    "31 Mabini Street, Brgy. Malate, Manila, Metro Manila 1004",
]
TIERS = ["New", "Verified", "Silver", "Gold"]
TIER_COLORS = {"Gold": ("#FFF4D6", "#9A6B00"), "Silver": ("#ECEEF3", "#4A5160"),
               "Verified": ("#DDF7EE", "#0B7A55"), "New": ("#E7E9FB", "#2B2A8C")}
STATUS_COLORS = {"Pending": ("#FFE8D9", "#B4531A"), "Shipped": ("#D7F5F0", "#0B7A6B"),
                 "Received": ("#E3F0FF", "#1D4ED8")}


def seed_rows() -> tuple[list, list, list]:
    rng = random.Random(42)
    products = [
        {"sku": "GLW-30", "name": "Glow Serum 30ml", "unit_price": 450, "stock": 40},
        {"sku": "OAT-150", "name": "Oat Cleanser 150ml", "unit_price": 450, "stock": 25},
        {"sku": "MST-100", "name": "Hydrating Mist 100ml", "unit_price": 390, "stock": 30},
        {"sku": "NRC-50", "name": "Night Repair Cream 50ml", "unit_price": 620, "stock": 0},
    ]
    t = now()
    resellers = [
        {"reseller_id": "R-001", "name": "Aria Cruz", "tier": "Gold", "mobile": "+63 917 552 8904",
         "email": "aria.cruz@gmail.com", "address": ADDRESS_DIRECTORY[0]},
        {"reseller_id": "R-002", "name": "Mark Rivera", "tier": "Silver", "mobile": "+63 999 888 7766",
         "email": "mark.rivera@yahoo.com", "address": ADDRESS_DIRECTORY[2]},
        {"reseller_id": "R-003", "name": "Jenny Santos", "tier": "Verified", "mobile": "+63 906 111 2233",
         "email": "jenny.santos@gmail.com", "address": ADDRESS_DIRECTORY[3]},
        {"reseller_id": "R-004", "name": "Lea Mendoza", "tier": "Verified", "mobile": "+63 928 555 1234",
         "email": "lea.mendoza@outlook.com", "address": ADDRESS_DIRECTORY[7]},
        {"reseller_id": "R-005", "name": "Paolo Reyes", "tier": "Silver", "mobile": "+63 917 123 4567",
         "email": "paolo.reyes@gmail.com", "address": ADDRESS_DIRECTORY[4]},
    ]
    for r in resellers:
        r["created_at"] = to_iso(t - timedelta(days=30))
    prod = {p["sku"]: p for p in products}
    res_ids = [r["reseller_id"] for r in resellers]

    def make(num, rid, sku, qty, recipient, mobile, addr, courier, created, status="Pending",
             printed=False, shipped=None, received=None):
        prefix = "JT" if courier == "J&T Express" else "LBC"
        return {"order_id": f"RL-{num}", "reseller_id": rid, "sku": sku, "product_name": prod[sku]["name"],
                "qty": qty, "unit_price": prod[sku]["unit_price"], "recipient": recipient, "mobile": mobile,
                "address": addr, "courier": courier, "status": status, "label_printed": printed,
                "tracking_no": f"{prefix}{rng.randint(10**9, 10**10 - 1)}" if shipped else None,
                "created_at": to_iso(created), "shipped_at": to_iso(shipped), "received_at": to_iso(received)}

    orders = [
        make(8494, "R-005", "GLW-30", 2, "Ramon Dizon", "+63 917 123 4567", ADDRESS_DIRECTORY[4], "LBC",
             t - timedelta(minutes=5)),
        make(8493, "R-004", "MST-100", 4, "Bea Lim", "+63 928 555 1234", ADDRESS_DIRECTORY[7], "LBC",
             t - timedelta(minutes=9)),
        make(8492, "R-001", "GLW-30", 3, "Aria Cruz", "+63 917 552 8904", ADDRESS_DIRECTORY[0], "J&T Express",
             t - timedelta(minutes=12), printed=True),
        make(8490, "R-002", "OAT-150", 5, "Mark Rivera", "+63 999 888 7766", ADDRESS_DIRECTORY[2], "J&T Express",
             t - timedelta(minutes=38), printed=True),
    ]
    # 12 shipped today (the older half already received), spread between midnight and now.
    midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
    step = min(25.0, max((t - midnight).total_seconds() / 60 - 1, 1) / 13)
    for i, num in enumerate(range(8489, 8477, -1)):
        shipped = t - timedelta(minutes=(i + 1) * step)
        received = shipped + (t - shipped) / 2 if i >= 6 else None
        if num == 8488:
            rid, sku, qty, addr, courier = "R-003", "MST-100", 2, ADDRESS_DIRECTORY[3], "J&T Express"
        else:
            rid, sku = rng.choice(res_ids), rng.choice(["GLW-30", "OAT-150", "MST-100"])
            qty, addr, courier = rng.randint(1, 5), rng.choice(ADDRESS_DIRECTORY), rng.choice(COURIERS)
        orders.append(make(num, rid, sku, qty, "Juan Dela Cruz", "+63 917 000 0000", addr, courier,
                           shipped - timedelta(minutes=15), "Received" if received else "Shipped",
                           True, shipped, received))
    # 9 shipped and received yesterday
    for i, num in enumerate(range(8477, 8468, -1)):
        shipped = midnight - timedelta(hours=1 + i * 1.5)
        orders.append(make(num, rng.choice(res_ids), rng.choice(["GLW-30", "OAT-150", "MST-100"]),
                           rng.randint(1, 5), "Maria Clara Reyes", "+63 917 000 0000",
                           rng.choice(ADDRESS_DIRECTORY), rng.choice(COURIERS),
                           shipped - timedelta(minutes=30), "Received", True, shipped,
                           shipped + timedelta(hours=20)))
    return products, resellers, orders


def ensure_seeded(store) -> None:
    if not store.select("products"):
        products, resellers, orders = seed_rows()
        store.insert("products", products)
        store.insert("resellers", resellers)
        store.insert("orders", orders)


def reset_demo(store) -> None:
    for table in ["orders", "resellers", "products"]:
        store.delete_all(table)
    ensure_seeded(store)


# ═════════════════════════════ Load data ══════════════════════════════════
store = get_store()
try:
    ensure_seeded(store)
    products = {r["sku"]: Product(r["sku"], r["name"], float(r["unit_price"]), int(r["stock"]))
                for r in store.select("products")}
    resellers = {r["reseller_id"]: Reseller(r["reseller_id"], r["name"], r["tier"], r.get("mobile") or "",
                                            r.get("email") or "", r.get("address") or "")
                 for r in store.select("resellers")}
    orders: list[Order] = []
    for r in store.select("orders"):
        reseller = resellers.get(r["reseller_id"]) or Reseller(r["reseller_id"] or "?", "Unknown reseller", "New")
        orders.append(Order(
            r["order_id"], reseller, r["sku"], r.get("product_name") or r["sku"], int(r["qty"]),
            float(r["unit_price"]), r["recipient"], r["mobile"], r["address"], r["courier"],
            from_iso(r["created_at"]), r["status"], bool(r.get("label_printed")), r.get("tracking_no"),
            from_iso(r.get("shipped_at")), from_iso(r.get("received_at"))))
except StoreError as e:
    st.error(f"Database problem: {e}")
    st.info("Check SUPABASE_URL and SUPABASE_KEY in your app's Secrets, and that you ran "
            "supabase_setup.sql in the Supabase SQL Editor.")
    st.stop()


def find_order(order_id: str) -> Order:
    return next(o for o in orders if o.order_id == order_id)


def by_status(status: str) -> list[Order]:
    return [o for o in orders if o.status == status]


def shipped_on(day: date) -> list[Order]:
    return [o for o in orders if o.shipped_at and o.shipped_at.date() == day]


def received_on(day: date) -> list[Order]:
    return [o for o in orders if o.received_at and o.received_at.date() == day]


# ═════════════════════════════ UI helpers ═════════════════════════════════
def peso(amount: float) -> str:
    return f"₱{amount:,.0f}" if amount == int(amount) else f"₱{amount:,.2f}"


def time_ago(ts: datetime) -> str:
    mins = int((now() - ts).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min{'s' if mins != 1 else ''} ago"
    if mins < 60 * 24:
        hrs = mins // 60
        return f"{hrs} hr{'s' if hrs != 1 else ''} ago"
    return f"{ts:%b %d, %I:%M %p}"


def badge(text: str, bg: str, fg: str) -> str:
    return (f"<span style='background:{bg};color:{fg};padding:2px 10px;border-radius:999px;"
            f"font-size:0.75rem;font-weight:600;white-space:nowrap'>{text}</span>")


def show_check(raw: str, check: Check) -> None:
    """Live feedback under an input: error, or the auto-corrected value that will be saved."""
    if not (raw or "").strip():
        return
    if not check.ok:
        st.caption(f"⚠️ {check.error}")
    elif check.value != raw.strip():
        st.caption(f"✏️ Auto-corrected → **{check.value}**")
    else:
        st.caption("✅ Looks good")


def waybill_text(o: Order) -> str:
    return "\n".join([
        "RESELLERLINK WAYBILL", "=" * 36,
        f"Order ID : #{o.order_id}", f"Courier  : {o.courier}", f"Date     : {now():%b %d, %Y %I:%M %p}",
        "-" * 36,
        f"Ship to  : {o.recipient}", f"Mobile   : {o.mobile}", f"Address  : {o.address}",
        "-" * 36,
        f"Item     : {o.product_name} ({o.sku})", f"Qty      : {o.qty}", f"Amount   : {peso(o.total)}",
        f"Reseller : {o.reseller.name} ({o.reseller.tier})", "=" * 36,
    ])


# ═════════════════════════════ Callbacks (write to the database) ═════════
def cb_ship(order_id: str) -> None:
    store.update("orders", order_id, find_order(order_id).mark_shipped(now()))
    st.session_state.flash = f"Order #{order_id} marked as shipped (handed to courier)."


def cb_receive(order_id: str) -> None:
    store.update("orders", order_id, find_order(order_id).mark_received(now()))
    st.session_state.flash = f"Order #{order_id} marked as received."


def cb_print(order_id: str) -> None:
    store.update("orders", order_id, {"label_printed": True})
    st.session_state.flash = f"Waybill for #{order_id} downloaded — order staged for pickup."


def cb_pick_frequent(sku: str) -> None:
    st.session_state.product_sku = sku
    clamp_qty()


def clamp_qty() -> None:
    p = products.get(st.session_state.get("product_sku"))
    if p and p.stock > 0:
        st.session_state.qty = min(max(1, st.session_state.get("qty", 1)), p.stock)


# ═════════════════════════════ Page setup ═════════════════════════════════
st.markdown("""
<style>
.rl-banner {background:linear-gradient(135deg,#2B2A8C,#3F3DB8);color:#fff;border-radius:14px;padding:14px 18px;margin-bottom:6px}
.rl-banner small {opacity:.8;letter-spacing:.06em;font-weight:600}
.rl-banner h3 {color:#fff;margin:2px 0 4px 0}
.rl-pill {background:#7EF0D8;color:#0B3B33;border-radius:999px;padding:2px 10px;font-weight:700;font-size:.8rem;float:right}
.rl-muted {color:#6B7280;font-size:.85rem}
</style>
""", unsafe_allow_html=True)

PAGES = ["Dashboard", "Submit Order", "Transaction History", "Resellers"]
PAGE_ICONS = {"Dashboard": "📊", "Submit Order": "📝", "Transaction History": "🧾", "Resellers": "👥"}
st.session_state.setdefault("flash", None)
st.session_state.setdefault("page", "Dashboard")
if st.session_state.get("goto"):
    st.session_state.page = st.session_state.pop("goto")
if st.session_state.flash:
    st.toast(st.session_state.flash, icon="✅")
    st.session_state.flash = None

with st.sidebar:
    st.markdown("## 📦 ResellerLink")
    st.caption("● Main Warehouse Hub")
    st.radio("Navigate", PAGES, key="page",
             format_func=lambda p: f"{PAGE_ICONS[p]} {p}" + (f"  ({len(by_status('Pending'))} pending)"
                                                            if p == "Dashboard" else ""))
    st.divider()
    if isinstance(store, SupabaseStore):
        st.caption("🟢 Data saved to **Supabase** — persists across sessions.")
    else:
        st.caption("🟡 Using a **local SQLite file**. Add Supabase secrets to keep data online.")
    with st.expander("Demo tools"):
        confirm = st.checkbox("I understand this deletes all data")
        if st.button("Reset demo data", disabled=not confirm):
            reset_demo(store)
            st.session_state.flash = "Demo data restored."
            st.rerun()


# ═════════════════════════════ Dashboard ══════════════════════════════════
def next_pickup() -> tuple[str, datetime]:
    t = now()
    for courier, slot in PICKUP_SCHEDULE:
        at = datetime.combine(t.date(), slot, tzinfo=TZ)
        if at > t:
            return courier, at
    courier, slot = PICKUP_SCHEDULE[0]
    return courier, datetime.combine(t.date() + timedelta(days=1), slot, tzinfo=TZ)


def countdown(at: datetime) -> str:
    h, m = divmod(int((at - now()).total_seconds() // 60) + 1, 60)
    return f"In {h}h {m}m" if h else f"In {m}m"


def render_order_card(o: Order) -> None:
    with st.container(border=True):
        left, right = st.columns([3, 1.3])
        left.markdown(f"**#{o.order_id}** <span class='rl-muted'>· {time_ago(o.created_at)}</span>",
                      unsafe_allow_html=True)
        right.markdown(f"<div style='text-align:right'>{badge(o.status, *STATUS_COLORS[o.status])}</div>",
                       unsafe_allow_html=True)
        st.markdown(f"{o.reseller.name} &nbsp;{badge(o.reseller.tier.upper(), *TIER_COLORS.get(o.reseller.tier, TIER_COLORS['New']))}",
                    unsafe_allow_html=True)
        a, b = st.columns([3, 1.3])
        a.markdown(f"🧴 **{o.product_name}**  \n<span class='rl-muted'>SKU: {o.sku} • Qty: {o.qty} pcs</span>",
                   unsafe_allow_html=True)
        b.markdown(f"<div style='text-align:right;font-weight:700;font-size:1.1rem'>{peso(o.total)}</div>",
                   unsafe_allow_html=True)
        st.markdown(f"<span class='rl-muted'>📍 {o.address}</span>", unsafe_allow_html=True)

        c1, c2 = st.columns([3, 1.3])
        if o.status == "Pending":
            c1.button("🚚 Mark as Shipped", key=f"ship_{o.order_id}", type="primary",
                      on_click=cb_ship, args=(o.order_id,))
            c2.download_button("🖨️ Print", data=waybill_text(o), file_name=f"{o.order_id}_waybill.txt",
                               mime="text/plain", key=f"print_{o.order_id}", on_click=cb_print, args=(o.order_id,))
            if o.label_printed:
                st.caption(f"🏷️ Label printed · staged for {o.courier}")
        elif o.status == "Shipped":
            c1.button("📦 Mark as Received", key=f"recv_{o.order_id}", type="primary",
                      on_click=cb_receive, args=(o.order_id,))
            with c2.popover("Tracking"):
                render_timeline(o)
            st.caption(f"🚚 Shipped via {o.courier} at {o.shipped_at:%I:%M %p} · {o.tracking_no}")
        else:
            c1.markdown(f"<span class='rl-muted'>✅ Received {o.received_at:%b %d, %I:%M %p}</span>",
                        unsafe_allow_html=True)
            with c2.popover("Tracking"):
                render_timeline(o)


def render_timeline(o: Order) -> None:
    st.markdown(f"**#{o.order_id}** · Tracking no.: `{o.tracking_no or '—'}`")
    steps = [("📝 Order placed", o.created_at, f"by {o.reseller.name}"),
             ("🚚 Shipped", o.shipped_at, f"handed to {o.courier}"),
             ("📦 Received", o.received_at, f"by {o.recipient}")]
    for title, when, detail in steps:
        if when:
            st.markdown(f"- {title} — {when:%b %d, %I:%M %p} ({detail})")
        else:
            st.markdown(f"- <span class='rl-muted'>{title} — waiting</span>", unsafe_allow_html=True)


def page_dashboard() -> None:
    st.markdown("### ResellerLink")
    st.caption("● Main Warehouse Hub")

    courier, at = next_pickup()
    staged = [o for o in by_status("Pending") if o.courier == courier and o.label_printed]
    st.markdown(f"""
<div class="rl-banner">
  <span class="rl-pill">{countdown(at)}</span>
  <small>NEXT COURIER PICKUP</small>
  <h3>{courier} • {at:%I:%M %p}</h3>
  <span>{len(staged)} order{'s' if len(staged) != 1 else ''} staged for driver handoff</span>
</div>""", unsafe_allow_html=True)
    with st.expander(f"Manifest — {courier}"):
        if staged:
            for o in staged:
                st.markdown(f"- **#{o.order_id}** · {o.product_name} × {o.qty} → {o.recipient}, {o.address}")
        else:
            st.caption("No orders staged yet. Print a label on a pending order to stage it for this courier.")

    today, yesterday = now().date(), now().date() - timedelta(days=1)
    pending = by_status("Pending")
    shipped_today, shipped_yday = len(shipped_on(today)), len(shipped_on(yesterday))
    k1, k2, k3 = st.columns(3)
    k1.metric("⏳ Pending", f"{len(pending)}")
    k1.caption(f"Est. pick: {round(len(pending) * MINUTES_PER_PICK)} mins")
    k2.metric("🚚 Shipped today", f"{shipped_today}", delta=f"{shipped_today - shipped_yday:+d} vs yesterday")
    k3.metric("📦 Received today", f"{len(received_on(today))}")

    query = st.text_input("Search", placeholder="🔍 Search Order ID, Reseller, SKU…", label_visibility="collapsed")
    view = st.radio("Filter", ["All Orders", *STATUSES], horizontal=True, label_visibility="collapsed", key="filter")
    st.caption(" · ".join([f"All: **{len(orders)}**"] + [f"{s}: **{len(by_status(s))}**" for s in STATUSES]))

    shown = orders if view == "All Orders" else by_status(view)
    if query.strip():
        q = query.strip().lower().lstrip("#")
        shown = [o for o in shown if q in " ".join(
            [o.order_id, o.reseller.name, o.sku, o.product_name, o.recipient]).lower()]
    order_rank = {s: i for i, s in enumerate(STATUSES)}
    shown = sorted(shown, key=lambda o: (order_rank[o.status], -o.created_at.timestamp()))
    if view == "All Orders":
        shown = shown[:30]
        st.caption("Showing the 30 most relevant orders. See **Transaction History** for everything.")
    if not shown:
        st.info("No orders match your search.")
    for o in shown:
        render_order_card(o)


# ═════════════════════════════ Submit order ═══════════════════════════════
FORM_KEYS = ["product_sku", "qty", "recipient", "mobile", "address_raw", "courier", "reseller_id"]


def page_submit_order() -> None:
    if st.session_state.pop("reset_form", False):
        for k in FORM_KEYS:
            st.session_state.pop(k, None)
    if not products or not resellers:
        st.warning("Add products and resellers first.")
        return

    st.session_state.setdefault("product_sku", next(iter(products)))
    batch_no = 104 + len(shipped_on(now().date())) // 10
    h1, h2 = st.columns([3, 1])
    h1.markdown("<span style='color:#0B7A6B;font-weight:700'>● FAST DIRECT DISPATCH</span>", unsafe_allow_html=True)
    h2.markdown(f"<div style='text-align:right'>{badge(f'Batch #{batch_no}', '#E7E9FB', '#2B2A8C')}</div>",
                unsafe_allow_html=True)

    reseller_id = st.selectbox("Submitting as (reseller)", list(resellers), key="reseller_id",
                               format_func=lambda r: f"{resellers[r].name} — {resellers[r].tier}")

    with st.container(border=True):
        st.markdown("#### 🗂️ Order Details")
        sku = st.selectbox("Product Item", list(products), key="product_sku", on_change=clamp_qty,
                           format_func=lambda s: f"{products[s].name}  ({s})")
        product = products[sku]
        if product.stock > 0:
            st.markdown(badge(f"SKU In Stock · {product.stock} left", "#D7F5F0", "#0B7A6B"), unsafe_allow_html=True)
        else:
            st.markdown(badge("Out of Stock", "#FDE2E1", "#B42318"), unsafe_allow_html=True)

        recent = [o.sku for o in sorted(orders, key=lambda o: o.created_at, reverse=True)
                  if o.reseller.reseller_id == reseller_id and o.sku in products]
        freq = list(dict.fromkeys(recent))[:3] or [s for s in products if products[s].stock > 0][:3]
        st.caption("Frequent Reseller Items")
        for col, s in zip(st.columns(len(freq)), freq):
            col.button(("✨ " if s == sku else "") + products[s].name, key=f"freq_{s}",
                       on_click=cb_pick_frequent, args=(s,), type="primary" if s == sku else "secondary")

        qty = 0
        if product.stock > 0:
            st.session_state.setdefault("qty", 1)
            q1, q2 = st.columns([2, 1])
            qty = q1.number_input("Allocated Quantity", min_value=1, max_value=product.stock, step=1, key="qty")
            q2.markdown(f"<div style='text-align:right;padding-top:1.8rem'>{peso(product.unit_price)} / unit</div>",
                        unsafe_allow_html=True)
            st.markdown(f"**Order total: {peso(qty * product.unit_price)}**")

    with st.container(border=True):
        st.markdown("#### 🚚 Recipient & Delivery")
        courier = st.radio("Courier", COURIERS, horizontal=True, key="courier")
        recipient_raw = st.text_input("Recipient Full Name", key="recipient", placeholder="e.g. clarisse de guia")
        recipient = check_name(recipient_raw, "Recipient name")
        show_check(recipient_raw, recipient)
        mobile_raw = st.text_input("Mobile Contact Number", key="mobile", placeholder="e.g. 0917 552 8904")
        mobile = check_mobile(mobile_raw)
        show_check(mobile_raw, mobile)

        address_raw = st.text_input("Delivery Address", key="address_raw",
                                    placeholder="e.g. 24 orchid st, brgy san antonio, pasig")
        address = check_address(address_raw)
        show_check(address_raw, address)
        final_address = address.value if address.ok else None
        if address_raw.strip():
            tokens = [t for t in re.split(r"[\s,]+", address.value.lower()) if t]
            matches = [a for a in ADDRESS_DIRECTORY if all(t.rstrip(".") in a.lower() for t in tokens)]
            if matches:
                choice = st.radio("Verified matches — pick one, or keep yours",
                                  matches + ["Keep the address as entered"])
                if choice != "Keep the address as entered":
                    final_address = choice
                    st.markdown(badge("VERIFIED", "#D7F5F0", "#0B7A6B"), unsafe_allow_html=True)
            street = address.value.split(",")[0].strip().lower()
            twins = {a.split(",")[2].strip() for a in ADDRESS_DIRECTORY if a.lower().startswith(street) and street}
            if len(twins) > 1:
                st.caption(f"⚠️ This street exists in {', '.join(sorted(twins))} — double-check the city.")

    if st.button("📨 Submit Order", type="primary"):
        errors = []
        if product.stock <= 0:
            errors.append("This product is out of stock.")
        for chk in (recipient, mobile):
            if not chk.ok:
                errors.append(chk.error)
        if not final_address:
            errors.append(address.error or "Enter a delivery address.")
        if errors:
            for e in errors:
                st.error(e)
            return
        next_num = max([int(o.order_id.split("-")[1]) for o in orders] or [8000]) + 1
        row = {"order_id": f"RL-{next_num}", "reseller_id": reseller_id, "sku": sku,
               "product_name": product.name, "qty": int(qty), "unit_price": product.unit_price,
               "recipient": recipient.value, "mobile": mobile.value, "address": final_address,
               "courier": courier, "status": "Pending", "label_printed": False, "tracking_no": None,
               "created_at": to_iso(now()), "shipped_at": None, "received_at": None}
        store.insert("orders", [row])
        store.update("products", sku, {"stock": product.stock - int(qty)})
        st.session_state.flash = f"Order #RL-{next_num} submitted — {peso(qty * product.unit_price)} added to Pending."
        st.session_state.reset_form = True
        st.session_state.goto = "Dashboard"
        st.rerun()


# ═════════════════════════════ Transaction history ════════════════════════
def page_history() -> None:
    st.markdown("### 🧾 Transaction History")
    st.caption("Every order saved in the database, newest first.")

    f1, f2 = st.columns(2)
    picked_resellers = f1.multiselect("Reseller", list(resellers), format_func=lambda r: resellers[r].name)
    picked_status = f2.multiselect("Status", STATUSES)
    oldest = min([o.created_at.date() for o in orders] or [now().date()])
    d = st.date_input("Date range (order date)", value=(oldest, now().date()))
    start, end = (d if isinstance(d, (tuple, list)) and len(d) == 2 else (oldest, now().date()))
    query = st.text_input("Search history", placeholder="🔍 Order ID, recipient, product, tracking no.")

    rows = sorted(orders, key=lambda o: o.created_at, reverse=True)
    if picked_resellers:
        rows = [o for o in rows if o.reseller.reseller_id in picked_resellers]
    if picked_status:
        rows = [o for o in rows if o.status in picked_status]
    rows = [o for o in rows if start <= o.created_at.date() <= end]
    if query.strip():
        q = query.strip().lower().lstrip("#")
        rows = [o for o in rows if q in " ".join(
            [o.order_id, o.recipient, o.product_name, o.sku, o.tracking_no or "", o.reseller.name]).lower()]

    m1, m2, m3 = st.columns(3)
    m1.metric("Transactions", len(rows))
    m2.metric("Total sales", peso(sum(o.total for o in rows)))
    m3.metric("Items sold", sum(o.qty for o in rows))

    def fmt(dt):
        return f"{dt:%Y-%m-%d %I:%M %p}" if dt else ""

    table = [{"Order ID": o.order_id, "Order date": fmt(o.created_at), "Reseller": o.reseller.name,
              "Product": o.product_name, "Qty": o.qty, "Amount (₱)": o.total, "Recipient": o.recipient,
              "Courier": o.courier, "Status": o.status, "Tracking no.": o.tracking_no or "",
              "Shipped": fmt(o.shipped_at), "Received": fmt(o.received_at)} for o in rows]
    if not table:
        st.info("No transactions match these filters.")
        return
    st.dataframe(table, hide_index=True)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(table[0]))
    writer.writeheader()
    writer.writerows(table)
    st.download_button("⬇️ Download as CSV", data=buf.getvalue(), file_name="resellerlink_transactions.csv",
                       mime="text/csv")

    st.markdown("#### Order details")
    picked = st.selectbox("Choose an order", [o.order_id for o in rows])
    o = find_order(picked)
    with st.container(border=True):
        st.markdown(f"**#{o.order_id}** {badge(o.status, *STATUS_COLORS[o.status])}", unsafe_allow_html=True)
        st.markdown(f"{o.product_name} × {o.qty} = **{peso(o.total)}** · reseller **{o.reseller.name}**")
        st.markdown(f"<span class='rl-muted'>Ship to {o.recipient} · {o.mobile} · {o.address}</span>",
                    unsafe_allow_html=True)
        render_timeline(o)
        if o.status == "Pending":
            st.button("🚚 Mark as Shipped", key=f"h_ship_{o.order_id}", on_click=cb_ship, args=(o.order_id,))
        elif o.status == "Shipped":
            st.button("📦 Mark as Received", key=f"h_recv_{o.order_id}", on_click=cb_receive, args=(o.order_id,))


# ═════════════════════════════ Resellers ══════════════════════════════════
RESELLER_KEYS = ["r_name", "r_mobile", "r_email", "r_address", "r_tier"]


def page_resellers() -> None:
    if st.session_state.pop("reset_reseller_form", False):
        for k in RESELLER_KEYS:
            st.session_state.pop(k, None)

    st.markdown("### 👥 Resellers")
    summary = []
    for r in resellers.values():
        mine = [o for o in orders if o.reseller.reseller_id == r.reseller_id]
        summary.append({"ID": r.reseller_id, "Name": r.name, "Tier": r.tier, "Mobile": r.mobile,
                        "Email": r.email, "Orders": len(mine), "Total sales (₱)": sum(o.total for o in mine)})
    st.dataframe(summary, hide_index=True)

    with st.container(border=True):
        st.markdown("#### ➕ Register a reseller")
        st.caption("Type freely — names, numbers, emails and addresses are checked and auto-corrected as you go.")
        name_raw = st.text_input("Full name", key="r_name", placeholder="e.g. juan  dela cruz")
        name = check_name(name_raw, "Full name")
        show_check(name_raw, name)
        mobile_raw = st.text_input("Mobile number", key="r_mobile", placeholder="e.g. 09175528904 or +63 917 552 8904")
        mobile = check_mobile(mobile_raw)
        show_check(mobile_raw, mobile)
        email_raw = st.text_input("Email (optional)", key="r_email", placeholder="e.g. juan@gmial.com")
        email = check_email(email_raw)
        show_check(email_raw, email)
        address_raw = st.text_input("Address", key="r_address", placeholder="e.g. 15 rizal st, brgy poblacion, sta rosa")
        address = check_address(address_raw)
        show_check(address_raw, address)
        tier = st.selectbox("Tier", TIERS, key="r_tier")

        if st.button("Save reseller", type="primary"):
            errors = [c.error for c in (name, mobile, email, address) if not c.ok]
            if name.ok and any(r.name.lower() == name.value.lower() for r in resellers.values()):
                errors.append(f"{name.value} is already registered.")
            if mobile.ok and any(r.mobile == mobile.value for r in resellers.values()):
                errors.append(f"Mobile number {mobile.value} is already used by another reseller.")
            if errors:
                for e in errors:
                    st.error(e)
                return
            next_id = max([int(r.split("-")[1]) for r in resellers] or [0]) + 1
            store.insert("resellers", [{"reseller_id": f"R-{next_id:03d}", "name": name.value, "tier": tier,
                                        "mobile": mobile.value, "email": email.value, "address": address.value,
                                        "created_at": to_iso(now())}])
            st.session_state.flash = f"Reseller {name.value} saved (R-{next_id:03d})."
            st.session_state.reset_reseller_form = True
            st.rerun()


# ═════════════════════════════ Router ═════════════════════════════════════
{"Dashboard": page_dashboard, "Submit Order": page_submit_order,
 "Transaction History": page_history, "Resellers": page_resellers}[st.session_state.page]()
