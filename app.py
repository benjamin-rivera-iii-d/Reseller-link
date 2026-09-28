"""
ResellerLink Order Manager — Streamlit version of the Stitch prototype.

Screens
  1. Seller Dashboard   – courier pickup banner, KPIs, search, filters,
                          order cards with "Mark as Sent to Courier" and print.
  2. Submit Order       – reseller order form with stock check, frequent items,
                          quantity, recipient details and address lookup.

Run:  pip install -r requirements.txt
      streamlit run app.py
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import streamlit as st

TZ = ZoneInfo("Asia/Manila")
YESTERDAY_SENT = 9          # used for the "+N vs yesterday" comparison
MINUTES_PER_PICK = 4.5      # estimated packing time per pending order

st.set_page_config(page_title="ResellerLink Order Manager", page_icon="📦", layout="centered")


# ─────────────────────────────── Data model ───────────────────────────────
@dataclass
class Product:
    sku: str
    name: str
    unit_price: float
    stock: int

    def in_stock(self, qty: int = 1) -> bool:
        return self.stock >= qty

    def reduce_stock(self, qty: int) -> None:
        self.stock -= qty


@dataclass
class Reseller:
    name: str
    tier: str  # "Gold", "Silver" or "Verified"


@dataclass
class Order:
    order_id: str
    reseller: Reseller
    product: Product
    qty: int
    recipient: str
    mobile: str
    address: str
    courier: str
    created_at: datetime
    status: str = "Pending"
    label_printed: bool = False
    tracking_no: Optional[str] = None
    dispatched_at: Optional[datetime] = None

    @property
    def total(self) -> float:
        return self.qty * self.product.unit_price

    def mark_sent(self, when: datetime) -> None:
        prefix = "JT" if self.courier == "J&T Express" else "LBC"
        self.status = "Sent to Courier"
        self.dispatched_at = when
        self.tracking_no = f"{prefix}{random.randint(10**9, 10**10 - 1)}"


# ─────────────────────────────── Reference data ───────────────────────────
COURIERS = ["J&T Express", "LBC"]
PICKUP_SCHEDULE = [          # (courier, daily pickup time)
    ("J&T Express", time(10, 0)),
    ("LBC", time(11, 30)),
    ("J&T Express", time(14, 0)),
    ("LBC", time(16, 0)),
    ("J&T Express", time(17, 30)),
]

# Demo address directory used by the address lookup (stands in for GPS geocoding).
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

TIER_COLORS = {"Gold": ("#FFF4D6", "#9A6B00"), "Silver": ("#ECEEF3", "#4A5160"), "Verified": ("#DDF7EE", "#0B7A55")}
STATUS_COLORS = {"Pending": ("#FFE8D9", "#B4531A"), "Sent to Courier": ("#D7F5F0", "#0B7A6B")}


def now() -> datetime:
    return datetime.now(TZ)


def peso(amount: float) -> str:
    return f"₱{amount:,.0f}" if amount == int(amount) else f"₱{amount:,.2f}"


def time_ago(ts: datetime) -> str:
    mins = int((now() - ts).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min{'s' if mins != 1 else ''} ago"
    hrs = mins // 60
    return f"{hrs} hr{'s' if hrs != 1 else ''} ago"


def badge(text: str, bg: str, fg: str) -> str:
    return (f"<span style='background:{bg};color:{fg};padding:2px 10px;border-radius:999px;"
            f"font-size:0.75rem;font-weight:600;white-space:nowrap'>{text}</span>")


# ─────────────────────────────── Session state ────────────────────────────
def seed_data() -> None:
    rng = random.Random(42)
    products = {
        "GLW-30": Product("GLW-30", "Glow Serum 30ml", 450, 40),
        "OAT-150": Product("OAT-150", "Oat Cleanser 150ml", 450, 25),
        "MST-100": Product("MST-100", "Hydrating Mist 100ml", 390, 30),
        "NRC-50": Product("NRC-50", "Night Repair Cream 50ml", 620, 0),
    }
    resellers = {
        "Aria Cruz": Reseller("Aria Cruz", "Gold"),
        "Mark Rivera": Reseller("Mark Rivera", "Silver"),
        "Jenny Santos": Reseller("Jenny Santos", "Verified"),
        "Lea Mendoza": Reseller("Lea Mendoza", "Verified"),
        "Paolo Reyes": Reseller("Paolo Reyes", "Silver"),
    }
    t = now()
    orders = [
        Order("RL-8494", resellers["Paolo Reyes"], products["GLW-30"], 2, "Ramon Dizon", "9171234567",
              ADDRESS_DIRECTORY[4], "LBC", t - timedelta(minutes=5)),
        Order("RL-8493", resellers["Lea Mendoza"], products["MST-100"], 4, "Bea Lim", "9285551234",
              ADDRESS_DIRECTORY[7], "LBC", t - timedelta(minutes=9)),
        Order("RL-8492", resellers["Aria Cruz"], products["GLW-30"], 3, "Aria Cruz", "9175528904",
              ADDRESS_DIRECTORY[0], "J&T Express", t - timedelta(minutes=12), label_printed=True),
        Order("RL-8490", resellers["Mark Rivera"], products["OAT-150"], 5, "Mark Rivera", "9998887766",
              ADDRESS_DIRECTORY[2], "J&T Express", t - timedelta(minutes=38), label_printed=True),
    ]
    # 12 orders already sent today (RL-8478 … RL-8489). Dispatch times are spread
    # between midnight and now so they always count as "today".
    names = list(resellers)
    mins_since_midnight = (t - t.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 60
    step = min(25.0, max(mins_since_midnight - 1, 1) / 13)
    for i, num in enumerate(range(8489, 8477, -1)):
        dispatched = t - timedelta(minutes=(i + 1) * step)
        created = dispatched - timedelta(minutes=15)
        if num == 8488:
            o = Order("RL-8488", resellers["Jenny Santos"], products["MST-100"], 2, "Jenny Santos",
                      "9061112233", ADDRESS_DIRECTORY[3], "J&T Express", created)
        else:
            o = Order(f"RL-{num}", resellers[rng.choice(names)], products[rng.choice(["GLW-30", "OAT-150", "MST-100"])],
                      rng.randint(1, 5), "Customer", "9170000000", rng.choice(ADDRESS_DIRECTORY),
                      rng.choice(COURIERS), created)
        o.label_printed = True
        o.mark_sent(dispatched)
        orders.append(o)

    st.session_state.products = products
    st.session_state.resellers = resellers
    st.session_state.orders = orders
    st.session_state.seeded = True


if "seeded" not in st.session_state:
    seed_data()
st.session_state.setdefault("flash", None)
st.session_state.setdefault("page", "Dashboard")

# Navigation requests made after the sidebar has rendered are applied here, on the next run.
if st.session_state.get("goto"):
    st.session_state.page = st.session_state.pop("goto")

orders: list[Order] = st.session_state.orders
products: dict[str, Product] = st.session_state.products
resellers: dict[str, Reseller] = st.session_state.resellers


def pending_orders() -> list[Order]:
    return [o for o in orders if o.status == "Pending"]


def sent_today() -> list[Order]:
    today = now().date()
    return [o for o in orders if o.status == "Sent to Courier" and o.dispatched_at and o.dispatched_at.date() == today]


def find_order(order_id: str) -> Order:
    return next(o for o in orders if o.order_id == order_id)


# ─────────────────────────────── Callbacks ────────────────────────────────
def cb_mark_sent(order_id: str) -> None:
    find_order(order_id).mark_sent(now())
    st.session_state.flash = f"Order #{order_id} marked as sent to courier!"


def cb_print_label(order_id: str) -> None:
    find_order(order_id).label_printed = True
    st.session_state.flash = f"Waybill for #{order_id} downloaded — order staged for pickup."


def cb_pick_frequent(sku: str) -> None:
    st.session_state.product_sku = sku
    clamp_qty()


def clamp_qty() -> None:
    p = products[st.session_state.product_sku]
    if p.stock > 0:
        st.session_state.qty = min(max(1, st.session_state.get("qty", 1)), p.stock)


def waybill_text(o: Order) -> str:
    return "\n".join([
        "RESELLERLINK WAYBILL",
        "=" * 36,
        f"Order ID : #{o.order_id}",
        f"Courier  : {o.courier}",
        f"Date     : {now():%b %d, %Y %I:%M %p}",
        "-" * 36,
        f"Ship to  : {o.recipient}",
        f"Mobile   : +63 {o.mobile}",
        f"Address  : {o.address}",
        "-" * 36,
        f"Item     : {o.product.name} ({o.product.sku})",
        f"Qty      : {o.qty}",
        f"Amount   : {peso(o.total)}",
        f"Reseller : {o.reseller.name} ({o.reseller.tier})",
        "=" * 36,
    ])


# ─────────────────────────────── Styles ───────────────────────────────────
st.markdown("""
<style>
.rl-banner {background:linear-gradient(135deg,#2B2A8C,#3F3DB8);color:#fff;border-radius:14px;padding:14px 18px;margin-bottom:6px}
.rl-banner small {opacity:.8;letter-spacing:.06em;font-weight:600}
.rl-banner h3 {color:#fff;margin:2px 0 4px 0}
.rl-pill {background:#7EF0D8;color:#0B3B33;border-radius:999px;padding:2px 10px;font-weight:700;font-size:.8rem;float:right}
.rl-muted {color:#6B7280;font-size:.85rem}
</style>
""", unsafe_allow_html=True)

if st.session_state.flash:
    st.toast(st.session_state.flash, icon="✅")
    st.session_state.flash = None

# ─────────────────────────────── Sidebar / nav ────────────────────────────
with st.sidebar:
    st.markdown("## 📦 ResellerLink")
    st.caption("● Main Warehouse Hub")
    st.radio("Navigate", ["Dashboard", "Submit Order"], key="page",
             format_func=lambda p: f"📊 {p}  ({len(pending_orders())} pending)" if p == "Dashboard" else f"📝 {p}")
    st.divider()
    if st.button("Reset demo data"):
        seed_data()
        st.rerun()


# ─────────────────────────────── Dashboard ────────────────────────────────
def next_pickup() -> tuple[str, datetime]:
    t = now()
    for courier, slot in PICKUP_SCHEDULE:
        at = datetime.combine(t.date(), slot, tzinfo=TZ)
        if at > t:
            return courier, at
    courier, slot = PICKUP_SCHEDULE[0]
    return courier, datetime.combine(t.date() + timedelta(days=1), slot, tzinfo=TZ)


def countdown(at: datetime) -> str:
    mins = int((at - now()).total_seconds() // 60) + 1
    h, m = divmod(mins, 60)
    return f"In {h}h {m}m" if h else f"In {m}m"


def render_order_card(o: Order) -> None:
    with st.container(border=True):
        left, right = st.columns([3, 1.3])
        left.markdown(f"**#{o.order_id}** <span class='rl-muted'>· {time_ago(o.created_at)}</span>",
                      unsafe_allow_html=True)
        right.markdown(f"<div style='text-align:right'>{badge(o.status, *STATUS_COLORS[o.status])}</div>",
                       unsafe_allow_html=True)
        st.markdown(f"{o.reseller.name} &nbsp;{badge(o.reseller.tier.upper(), *TIER_COLORS[o.reseller.tier])}",
                    unsafe_allow_html=True)

        a, b = st.columns([3, 1.3])
        a.markdown(f"🧴 **{o.product.name}**  \n<span class='rl-muted'>SKU: {o.product.sku} • Qty: {o.qty} pcs</span>",
                   unsafe_allow_html=True)
        b.markdown(f"<div style='text-align:right;font-weight:700;font-size:1.1rem'>{peso(o.total)}</div>",
                   unsafe_allow_html=True)
        st.markdown(f"<span class='rl-muted'>📍 {o.address}</span>", unsafe_allow_html=True)

        if o.status == "Pending":
            c1, c2 = st.columns([3, 1.3])
            c1.button("🚚 Mark as Sent to Courier", key=f"send_{o.order_id}", type="primary",
                      on_click=cb_mark_sent, args=(o.order_id,))
            c2.download_button("🖨️ Print", data=waybill_text(o), file_name=f"{o.order_id}_waybill.txt",
                               mime="text/plain", key=f"print_{o.order_id}",
                               on_click=cb_print_label, args=(o.order_id,))
            if o.label_printed:
                st.caption(f"🏷️ Label printed · staged for {o.courier}")
        else:
            c1, c2 = st.columns([3, 1.3])
            c1.markdown(f"<span class='rl-muted'>✅ Dispatched via {o.courier} at "
                        f"{o.dispatched_at:%I:%M %p}</span>", unsafe_allow_html=True)
            with c2.popover("View Tracking"):
                st.markdown(f"**Tracking no.:** `{o.tracking_no}`")
                st.markdown(f"- {o.created_at:%I:%M %p} — Order placed by {o.reseller.name}\n"
                            f"- {o.dispatched_at:%I:%M %p} — Handed over to {o.courier}\n"
                            f"- In transit to {o.address.split(',')[-2].strip()}")


def page_dashboard() -> None:
    st.markdown("### ResellerLink")
    st.caption("● Main Warehouse Hub")

    courier, at = next_pickup()
    staged = [o for o in pending_orders() if o.courier == courier and o.label_printed]
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
                st.markdown(f"- **#{o.order_id}** · {o.product.name} × {o.qty} → {o.recipient}, {o.address}")
        else:
            st.caption("No orders staged yet. Print a label on a pending order to stage it for this courier.")

    pend, sent = pending_orders(), sent_today()
    k1, k2 = st.columns(2)
    k1.metric("⏳ Pending Queue", f"{len(pend)} awaiting",
              help=f"Estimated pick & pack time: {round(len(pend) * MINUTES_PER_PICK)} mins")
    k1.caption(f"Est. pick: {round(len(pend) * MINUTES_PER_PICK)} mins")
    k2.metric("✅ Sent Today", f"{len(sent)} orders", delta=f"{len(sent) - YESTERDAY_SENT:+d} vs yesterday")

    query = st.text_input("Search", placeholder="🔍 Search Order ID, Reseller, SKU…", label_visibility="collapsed")
    view = st.radio("Filter", ["All Orders", "Pending Queue", "Sent to Courier"], horizontal=True,
                    label_visibility="collapsed", key="filter")

    counts = {"All Orders": len(orders), "Pending Queue": len(pend),
              "Sent to Courier": len([o for o in orders if o.status == "Sent to Courier"])}
    st.caption(" · ".join(f"{k}: **{v}**" for k, v in counts.items()))

    shown = orders
    if view == "Pending Queue":
        shown = [o for o in shown if o.status == "Pending"]
    elif view == "Sent to Courier":
        shown = [o for o in shown if o.status == "Sent to Courier"]
    if query.strip():
        q = query.strip().lower().lstrip("#")
        shown = [o for o in shown if q in " ".join(
            [o.order_id, o.reseller.name, o.product.sku, o.product.name, o.recipient]).lower()]

    # Pending first, then newest first.
    shown = sorted(shown, key=lambda o: (o.status != "Pending", -o.created_at.timestamp()))
    if not shown:
        st.info("No orders match your search.")
    for o in shown:
        render_order_card(o)


# ─────────────────────────────── Order form ───────────────────────────────
FORM_KEYS = ["product_sku", "qty", "recipient", "mobile", "addr_query", "courier", "reseller_name"]


def page_submit_order() -> None:
    if st.session_state.pop("reset_form", False):
        for k in FORM_KEYS:
            st.session_state.pop(k, None)

    st.session_state.setdefault("product_sku", "GLW-30")
    batch_no = 104 + len(sent_today()) // 10
    h1, h2 = st.columns([3, 1])
    h1.markdown("<span style='color:#0B7A6B;font-weight:700'>● FAST DIRECT DISPATCH</span>", unsafe_allow_html=True)
    h2.markdown(f"<div style='text-align:right'>{badge(f'Batch #{batch_no}', '#E7E9FB', '#2B2A8C')}</div>",
                unsafe_allow_html=True)

    reseller_name = st.selectbox("Submitting as (reseller)", list(resellers), key="reseller_name",
                                 format_func=lambda n: f"{n} — {resellers[n].tier}")

    # ── Order details ──
    with st.container(border=True):
        st.markdown("#### 🗂️ Order Details")
        sku = st.selectbox("Product Item", list(products), key="product_sku", on_change=clamp_qty,
                           format_func=lambda s: f"{products[s].name}  ({s})")
        product = products[sku]
        if product.stock > 0:
            st.markdown(badge(f"SKU In Stock · {product.stock} left", "#D7F5F0", "#0B7A6B"), unsafe_allow_html=True)
        else:
            st.markdown(badge("Out of Stock", "#FDE2E1", "#B42318"), unsafe_allow_html=True)

        st.caption("Frequent Reseller Items")
        freq = [s for s in ["GLW-30", "MST-100", "OAT-150"] if s in products]
        cols = st.columns(len(freq))
        for col, s in zip(cols, freq):
            col.button(("✨ " if s == sku else "") + products[s].name, key=f"freq_{s}",
                       on_click=cb_pick_frequent, args=(s,),
                       type="primary" if s == sku else "secondary")

        qty = 0
        if product.stock > 0:
            st.session_state.setdefault("qty", 1)
            q1, q2 = st.columns([2, 1])
            qty = q1.number_input("Allocated Quantity", min_value=1, max_value=product.stock, step=1, key="qty")
            q2.markdown(f"<div style='text-align:right;padding-top:1.8rem'>{peso(product.unit_price)} / unit</div>",
                        unsafe_allow_html=True)
            st.markdown(f"**Order total: {peso(qty * product.unit_price)}**")

    # ── Recipient & delivery ──
    with st.container(border=True):
        st.markdown("#### 🚚 Recipient & Delivery")
        courier = st.radio("Courier", COURIERS, horizontal=True, key="courier")
        recipient = st.text_input("Recipient Full Name", key="recipient", placeholder="e.g. Clarisse De Guia")
        m1, m2 = st.columns([1, 5])
        m1.text_input("Code", value="+63", disabled=True)
        mobile_raw = m2.text_input("Mobile Contact Number", key="mobile", placeholder="917 552 8904")
        mobile = re.sub(r"\D", "", mobile_raw)
        mobile_ok = bool(re.fullmatch(r"9\d{9}", mobile))
        if mobile_raw and not mobile_ok:
            st.caption("⚠️ Enter a 10-digit PH mobile number starting with 9 (e.g. 917 552 8904).")

        addr_query = st.text_input("Address Lookup", key="addr_query", placeholder="🔍 Type street, barangay or city")
        address = None
        if addr_query.strip():
            tokens = addr_query.lower().replace(",", " ").split()
            matches = [a for a in ADDRESS_DIRECTORY if all(t in a.lower() for t in tokens)]
            if matches:
                address = st.radio("Select the correct address", matches)
                st.markdown(badge("VERIFIED", "#D7F5F0", "#0B7A6B") + " <span class='rl-muted'>address matched in directory</span>",
                            unsafe_allow_html=True)
                if len(matches) > 1:
                    st.caption("⚠️ Several addresses match — double-check the city before submitting.")
            else:
                st.warning("Address not found in the directory. Check the spelling.")
        st.caption("Demo: the lookup searches a sample address directory in place of live GPS geocoding.")

    # ── Submit ──
    if st.button("📨 Submit Order", type="primary"):
        errors = []
        if product.stock <= 0:
            errors.append("This product is out of stock.")
        if not recipient.strip():
            errors.append("Enter the recipient's full name.")
        if not mobile_ok:
            errors.append("Enter a valid mobile number.")
        if not address:
            errors.append("Select a verified delivery address.")
        if errors:
            for e in errors:
                st.error(e)
            return

        next_num = max(int(o.order_id.split("-")[1]) for o in orders) + 1
        new = Order(f"RL-{next_num}", resellers[reseller_name], product, int(qty), recipient.strip(),
                    mobile, address, courier, now())
        product.reduce_stock(int(qty))
        orders.append(new)
        st.session_state.flash = f"Order #{new.order_id} submitted — {peso(new.total)} added to the Pending Queue."
        st.session_state.reset_form = True
        st.session_state.goto = "Dashboard"
        st.rerun()


# ─────────────────────────────── Router ───────────────────────────────────
if st.session_state.page == "Dashboard":
    page_dashboard()
else:
    page_submit_order()
