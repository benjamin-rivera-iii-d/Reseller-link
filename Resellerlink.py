class Product:
    def __init__(self, sku, name, unit_price):
        self.sku = sku
        self.name = name
        self.unit_price = unit_price


class Address:
    ph_addresses = {
        "24 orchid st": [
            {"barangay": "San Antonio", "city": "Pasig City", "province": "Metro Manila", "zip": "1605"},
            {"barangay": "Pembo", "city": "Taguig City", "province": "Metro Manila", "zip": "1642"},
        ],
        "unit 402 solstice tower": [
            {"barangay": "Poblacion", "city": "Makati City", "province": "Metro Manila", "zip": "1210"}
        ],
        "block 12 lot 5 camella homes": [
            {"barangay": "Molino III", "city": "Bacoor", "province": "Cavite", "zip": "4102"}
        ],
    }

    def __init__(self, raw_input):
        self.raw_input = raw_input
        self.street = raw_input
        self.barangay = None
        self.city = None
        self.province = None
        self.zip_code = None
        self.verified = False
        self.suggestions = []
        self.autocomplete()

    def autocomplete(self):
        key = self.raw_input.strip().lower()
        for known_key, matches in Address.ph_addresses.items():
            if known_key in key or key in known_key:
                self.suggestions = matches
                best = matches[0]
                self.street = known_key.title()
                self.barangay = best["barangay"]
                self.city = best["city"]
                self.province = best["province"]
                self.zip_code = best["zip"]
                self.verified = True
                return
        self.verified = False

    def full_address(self):
        if self.verified:
            return f"{self.street}, Brgy. {self.barangay}, {self.city}, {self.province} {self.zip_code}"
        return f"{self.raw_input} (unverified)"


class User:
    def __init__(self, name):
        self.name = name

    def describe(self):
        return f"{self.__class__.__name__}: {self.name}"


class Reseller(User):
    def __init__(self, name, tier="Bronze"):
        super().__init__(name)
        self.tier = tier
        self.frequent_items = []

    def add_frequent_item(self, product):
        if product not in self.frequent_items:
            self.frequent_items.append(product)

    def submit_order(self, product, quantity, recipient_name, mobile_number, raw_address, queue, notes=""):
        address = Address(raw_address)
        order = Order(self, product, quantity, recipient_name, mobile_number, address, notes)
        queue.add_order(order)
        return order


class Order:
    _id_counter = 8491
    COMMISSION_RATE = 0.20
    EXPRESS_DELIVERY_FEE = 85.00

    def __init__(self, reseller, product, quantity, recipient_name, mobile_number, address, notes=""):
        Order._id_counter += 1
        self.order_id = f"RL-{Order._id_counter}"
        self.reseller = reseller
        self.product = product
        self.quantity = quantity
        self.recipient_name = recipient_name
        self.mobile_number = mobile_number
        self.address = address
        self.notes = notes
        self.status = "Pending"
        self.tracking_id = None

    def subtotal(self):
        return self.product.unit_price * self.quantity

    def commission(self):
        return round(self.subtotal() * Order.COMMISSION_RATE, 2)

    def merchant_payable(self):
        return round(self.subtotal() + Order.EXPRESS_DELIVERY_FEE, 2)

    def settlement_summary(self):
        return (
            f"Subtotal: P{self.subtotal():,.2f} | "
            f"Est. Express Delivery: P{Order.EXPRESS_DELIVERY_FEE:,.2f} | "
            f"Reseller Commission (20%): +P{self.commission():,.2f} | "
            f"Merchant Payable: P{self.merchant_payable():,.2f} | "
            f"COD Payout: Collect P{self.merchant_payable():,.2f}"
        )

    def mark_sent(self, tracking_id):
        self.status = "Sent to Courier"
        self.tracking_id = tracking_id

    def dashboard_line(self):
        tag = f"{self.tracking_id}" if self.tracking_id else ""
        return (
            f"[{self.order_id}] {self.reseller.name} ({self.reseller.tier} Tier) - "
            f"{self.product.name} SKU:{self.product.sku} x{self.quantity} P{self.subtotal():,.2f} | "
            f"{self.address.full_address()} | {self.status} {tag}"
        )


class OrderQueue:
    def __init__(self):
        self.orders = []

    def add_order(self, order):
        self.orders.append(order)

    def pending_orders(self):
        return [o for o in self.orders if o.status == "Pending"]

    def sent_orders(self):
        return [o for o in self.orders if o.status == "Sent to Courier"]

    def find_order(self, order_id):
        for o in self.orders:
            if o.order_id == order_id:
                return o
        return None

    def search(self, keyword):
        keyword = keyword.lower()
        return [
            o for o in self.orders
            if keyword in o.order_id.lower()
            or keyword in o.reseller.name.lower()
            or keyword in o.product.sku.lower()
            or keyword in o.reseller.tier.lower()
        ]


class Seller(User):
    def __init__(self, name, queue, next_courier="J&T Express", pickup_time="2:00 PM"):
        super().__init__(name)
        self.queue = queue
        self.next_courier = next_courier
        self.pickup_time = pickup_time

    def dashboard_summary(self):
        pending = self.queue.pending_orders()
        sent = self.queue.sent_orders()
        return (
            f"NEXT COURIER PICKUP: {self.next_courier} - {self.pickup_time} "
            f"({len(pending)} orders staged for driver handoff)\n"
            f"PENDING QUEUE: {len(pending)} awaiting\n"
            f"SENT TODAY: {len(sent)} orders"
        )

    def mark_order_sent(self, order_id, tracking_id):
        order = self.queue.find_order(order_id)
        if order:
            order.mark_sent(tracking_id)
            return True
        return False

    def view_all(self):
        return [o.dashboard_line() for o in self.queue.orders]


if __name__ == "__main__":
    glow_serum = Product("GLW-30", "Glow Serum 30ml (Vitamin C + Hyaluronic)", 450.00)
    oat_cleanser = Product("OAT-150", "Oat Cleanser 150ml", 450.00)
    hydrating_mist = Product("MST-100", "Hydrating Mist 100ml", 390.00)

    queue = OrderQueue()
    seller = Seller("Benj", queue)

    aria = Reseller("Aria Cruz", tier="Gold")
    aria.add_frequent_item(glow_serum)
    aria.add_frequent_item(hydrating_mist)
    aria.add_frequent_item(oat_cleanser)

    mark = Reseller("Mark Rivera", tier="Silver")
    jenny = Reseller("Jenny Santos", tier="Verified")

    order1 = aria.submit_order(
        glow_serum, 3, "Clarisse De Guia", "+63 917 552 8904",
        "24 Orchid St", queue, notes="Leave at front porch with guard. Ring doorbell"
    )

    order2 = mark.submit_order(
        oat_cleanser, 5, "Mark Rivera", "+63 918 000 1122",
        "Unit 402 Solstice Tower", queue
    )

    order3 = jenny.submit_order(
        hydrating_mist, 2, "Jenny Santos", "+63 919 333 4455",
        "Block 12 Lot 5 Camella Homes", queue
    )
    seller.mark_order_sent(order3.order_id, "#JNT-8821903")

    print("=== Seller Dashboard Summary ===")
    print(seller.dashboard_summary())

    print("\n=== All Orders ===")
    for line in seller.view_all():
        print(line)

    print(f"\n=== Order Settlement for {order1.order_id} ===")
    print(order1.settlement_summary())

    print(f"\n=== Address Auto-Complete Suggestions for '{order1.address.raw_input}' ===")
    for s in order1.address.suggestions:
        print(f"{order1.address.street}, Brgy. {s['barangay']}, {s['city']}, {s['province']} {s['zip']}")

    print("\n=== Marking order2 as Sent to Courier ===")
    seller.mark_order_sent(order2.order_id, "#JNT-8821905")
    for line in seller.view_all():
        print(line)

    print("\n=== Search: 'gold' ===")
    for o in queue.search("gold"):
        print(o.dashboard_line())