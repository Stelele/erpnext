"""Repro for upstream issue: concurrent POS Invoice submits oversell the warehouse.

Ported from the pos_expenses race-guard test suite, stripped of every custom-app
dependency, so maintainers can run it with `bench run-tests --app erpnext`.

Two facts, both proven on a develop bench:

1. *Control*: when the draft's transaction is closed before the sibling
   reserved, the submit opens a fresh read view and the stock check correctly
   rejects (`has no stock in warehouse`).
2. *The bug*: the POS checkout saves and submits in ONE HTTP request (one DB
   transaction). Under REPEATABLE-READ, the availability read at save pins the
   whole transaction's read view; a sibling reservation that commits after that
   read but before the submit is invisible to the submit's re-validation, so
   both invoices submit for the last unit and the unconsolidated reservation
   pool ends up overdrawn. The negative swing surfaces later, at POS Closing
   consolidation, where it cannot be fixed with forward stock.
"""

import random
import unittest

import frappe
from frappe.utils import get_datetime

try:
	from frappe.tests import IntegrationTestCase
except ImportError:  # v15: the class still lives in frappe.tests.utils under its old name
	from frappe.tests.utils import FrappeTestCase as IntegrationTestCase

COMPANY = "_Test Company"
WAREHOUSE = "Stores - _TC"
ROLES = ("Accounts Manager", "Accounts User", "Sales Manager", "Stock User", "Item Manager")


class TestPOSInvoiceConcurrentSubmitRace(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Company", COMPANY):
			raise unittest.SkipTest("requires the erpnext _Test fixtures")

		# each test gets its own item and customer, so tests are fully
		# independent (no reservation pool leaks between them)

		# develop/higher defaults POS Settings to "Sales Invoice" mode, which
		# refuses POS Invoices the race lives in. The field does not exist on v15
		# (where every POS sale is a POS Invoice), so guard by meta first.
		fields = {f.name for f in frappe.get_meta("POS Settings").fields}
		cls.original_invoice_type = (
			frappe.db.get_single_value("POS Settings", "invoice_type") if "invoice_type" in fields else None
		)
		if "invoice_type" in fields:
			frappe.db.set_single_value("POS Settings", "invoice_type", "POS Invoice")
			frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		try:
			super().tearDownClass()
		finally:
			if cls.original_invoice_type is not None and "invoice_type" in (
				f.name for f in frappe.get_meta("POS Settings").fields
			):
				frappe.db.set_single_value("POS Settings", "invoice_type", cls.original_invoice_type)
				frappe.db.commit()

	@classmethod
	def _cancel_delete(cls, doctype, filters):
		for name in frappe.get_all(doctype, filters=filters, pluck="name"):
			try:
				doc = frappe.get_doc(doctype, name)
				if doc.docstatus == 1:
					doc.cancel()
			except Exception:
				frappe.db.rollback()
			frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)

	def _cleanup_this_test(self):
		frappe.local.db = self._primary_connection  # secondary_connection CM workaround
		self._cancel_delete("POS Invoice", {"customer": self.customer})
		self._cancel_delete(
			"Stock Entry",
			{
				"name": (
					"in",
					frappe.get_all(
						"Stock Entry Detail", filters={"item_code": self.item_code}, pluck="parent"
					),
				)
			},
		)
		self._cancel_delete("POS Opening Entry", {"pos_profile": self.profile.name})
		frappe.delete_doc("POS Profile", self.profile.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Item", self.item_code, ignore_permissions=True, force=True)
		frappe.delete_doc("Customer", self.customer, ignore_permissions=True, force=True)
		frappe.delete_doc("User", self.cashier, ignore_permissions=True, force=True)

	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(self._cleanup_this_test)

		suffix = str(random.randint(100000, 999999))
		self.item_code = "ZREPRO-Race-" + suffix
		self.customer = "ZREPRO Customer Race " + suffix

		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": self.customer,
				"customer_type": "Individual",
				"customer_group": "Commercial",
				"territory": "All Territories",
				"default_currency": "USD",
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": self.item_code,
				"item_name": self.item_code,
				"item_group": "All Item Groups",
				"is_stock_item": 1,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		from frappe.core.doctype.user_permission.test_user_permission import create_user

		self.cashier = create_user(
			f"pos_repro_cashier{random.randint(100000, 999999)}@example.com", *ROLES
		).name
		frappe.db.commit()
		self.profile = self.create_profile()
		frappe.db.commit()

	def create_profile(self):
		write_off = frappe.db.get_value(
			"Account",
			{"company": COMPANY, "is_group": 0, "root_type": "Expense", "disabled": 0},
			"name",
			order_by="creation",
		)
		if not write_off:
			raise unittest.SkipTest("no expense account available for the POS profile write-off field")
		profile_name = "ZREPRO Profile " + str(random.randint(100000, 999999))
		return frappe.get_doc(
			{
				"doctype": "POS Profile",
				"name": profile_name,
				"pos_profile_name": profile_name,
				"company": COMPANY,
				"warehouse": WAREHOUSE,
				"currency": "USD",
				"selling_price_list": "Standard Selling",
				"write_off_account": write_off,
				"write_off_cost_center": frappe.db.get_value(
					"Cost Center", {"company": COMPANY, "is_group": 0}, "name", order_by="creation"
				),
				"payments": [
					{
						"default": 1,
						"mode_of_payment": "Cash",
						"default_account": frappe.db.get_value(
							"Account", {"account_type": "Cash", "company": COMPANY}, "name"
						),
					}
				],
			}
		).insert(ignore_permissions=True)

	def create_pi(self, qty=1, **overrides):
		pi = frappe.new_doc("POS Invoice")
		pi.is_pos = 1
		pi.update_stock = 1
		pi.pos_profile = self.profile.name
		pi.company = COMPANY
		pi.customer = overrides.pop("customer", self.customer)
		pi.append(
			"items",
			{
				"item_code": self.item_code,
				"qty": qty,
				"rate": 100,
				"price_list_rate": 100,
				"warehouse": WAREHOUSE,
			},
		)
		pi.append(
			"payments",
			{"mode_of_payment": "Cash", "account": "Cash - _TC", "amount": 100 * qty},
		)
		pi.set_missing_values()
		pi.save(ignore_permissions=True)
		return pi

	def stock_in(self, qty):
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		make_stock_entry(item_code=self.item_code, target=WAREHOUSE, qty=qty, basic_rate=100)

	def open_pos(self):
		poe = frappe.new_doc("POS Opening Entry")
		poe.pos_profile = self.profile.name
		poe.user = self.cashier
		poe.company = COMPANY
		poe.period_start_date = get_datetime()
		poe.append("balance_details", {"mode_of_payment": "Cash"})
		poe.save(ignore_permissions=True)
		poe.submit()
		return poe

	def assert_no_negative_ledger(self):
		neg = frappe.db.sql(
			"SELECT COUNT(*) c FROM `tabStock Ledger Entry` WHERE item_code = %(item)s AND qty_after_transaction < 0",
			{"item": self.item_code},
			as_dict=1,
		)
		self.assertEqual(neg[0].c, 0, "a negative qty slipped into the stock ledger")

	def pool_state(self):
		return frappe.db.sql(
			"""SELECT p.name, SUM(c.stock_qty) reserved
			FROM `tabPOS Invoice` p
			JOIN `tabPOS Invoice Item` c ON c.parent = p.name
			WHERE c.item_code = %(item)s AND p.docstatus = 1
			  AND IFNULL(p.consolidated_invoice, '') = ''
			GROUP BY p.name""",
			{"item": self.item_code},
			as_dict=1,
		)

	def test_control_fresh_submit_rejects_committed_reservation(self):
		"""Control: when the draft's transaction ended before the sibling reserved,
		the submit opens a fresh read view and the check correctly rejects."""
		self.open_pos()
		frappe.db.commit()
		self.stock_in(2)
		frappe.db.commit()

		sibling = self.create_pi(qty=1)
		sibling.submit()
		frappe.db.commit()

		pi = self.create_pi(qty=1)
		frappe.db.commit()

		with self.secondary_connection():
			racer = self.create_pi(qty=1)
			racer.submit()
			frappe.db.commit()

		# WORKAROUND: same primary-restoration as the test below.
		frappe.local.db = self._primary_connection

		# committed stock: 2 units, committed reservations: 2, B needs 1 => must fail
		with self.assertRaises(frappe.ValidationError) as caught:
			pi.submit()

		self.assertIn("has no stock in warehouse", str(caught.exception))
		pi.reload()
		self.assertEqual(pi.docstatus, 0, "the racing invoice submitted for stock already sold")

	def test_single_request_save_then_submit_oversells(self):
		"""THE BUG: the POS checkout saves and submits in ONE HTTP request, i.e. one
		transaction. Under REPEATABLE-READ the reads during save pin the
		transaction's read view; a sibling reservation that commits after that read
		but before the submit is invisible to the submit's re-validation, so both
		invoices submit for the last unit."""
		self.open_pos()
		frappe.db.commit()
		self.stock_in(1)
		frappe.db.commit()

		# sibling C's draft is created and committed BEFORE B's transaction starts,
		# so C's submit needs no naming-series lock inside the race window (B's
		# uncommitted insert would otherwise block C's insert).
		racer = self.create_pi(qty=1)
		frappe.db.commit()

		# B's request: save + submit in ONE transaction (one HTTP request). The
		# availability read at save pins the read view. Do NOT commit in between:
		# committing here would hand B a fresh read view and close the race.
		pi = self.create_pi(qty=1)

		# C's submit completes inside B's stale read-view window — C took the last
		# unit (stock: 1, committed reservations after C: 1)
		with self.secondary_connection():
			racer.submit()
			frappe.db.commit()

		# WORKAROUND: on this frappe tree `secondary_connection` loses the primary
		# — `frappe.connect()` rebinds `frappe.local.db` before `current_conn` is
		# captured, so the context manager restores the secondary onto itself and
		# every later statement keeps running on the race leg's connection.
		# Explicitly hand the stage back to the primary.
		frappe.local.db = self._primary_connection

		# committed stock: 1 unit, all of it reserved by C. B needs 1 and must be
		# rejected — but its submit re-validates inside the stale read view
		# (bin: 1 unreserved, reserved: 0) and passes anyway.
		pi.submit()
		frappe.db.commit()

		pi.reload()
		racer.reload()
		self.assertEqual(pi.docstatus, 1, "B must submit for stock already sold (this is the bug)")
		self.assertEqual(racer.docstatus, 1, "the racer must have committed equally")

		# the pool is overdrawn: two submitted invoices reserved 2 units against
		# a bin that only ever held 1
		pool = self.pool_state()
		reserved = sum(float(row.reserved) for row in pool)
		self.assertEqual(reserved, 2.0)
		stock = frappe.db.get_value(
			"Bin", {"item_code": self.item_code, "warehouse": WAREHOUSE}, "actual_qty"
		)
		self.assertGreater(
			reserved, float(stock or 0),
			f"pool not overdrawn: reserved={reserved}, bin={stock}",
		)

		# the ledger itself is still clean here: POS Invoices post no stock. The
		# negative swing surfaces at POS Closing consolidation, backdated to the
		# historical position, where no forward stock can fix it.
		self.assert_no_negative_ledger()
