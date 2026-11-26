"""Tests for the voucher window, batch reads and schedule in agent.py. Standard library only, no Tally or ERP:

    python -m unittest test_agent -v      (from this folder)
"""

import datetime
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import agent

DT = datetime.datetime


def make_config(extra=""):
	ini = (
		"[erp]\nurl = http://erp.test\napi_key = k\napi_secret = s\n"
		"[tally]\nurl = http://tally.test:9000\ncompany = Test Co\n" + extra
	)
	fd, path = tempfile.mkstemp(suffix=".ini")
	with os.fdopen(fd, "w", encoding="utf-8") as f:
		f.write(ini)
	try:
		return agent.Config(path)
	finally:
		os.remove(path)


class TestSinceFormula(unittest.TestCase):
	def _eval(self, formula, day):
		"""The formula as Python, run for one date -- the same comparisons Tally is given."""
		expr = (
			formula.replace("$$YearOfDate:$Date", str(day.year))
			.replace("$$MonthOfDate:$Date", str(day.month))
			.replace("$$DayOfDate:$Date", str(day.day))
			.replace(" OR ", " or ")
			.replace(" AND ", " and ")
			.replace(" = ", " == ")
		)
		return eval(expr)

	def test_it_selects_exactly_the_dates_on_or_after_the_start(self):
		# Month ends, year ends and a leap day, on both sides of several starts.
		for since in ("20260815", "20260101", "20261231", "20260301", "20240229", "20260430"):
			formula = agent.since_formula(since)
			start = datetime.datetime.strptime(since, "%Y%m%d").date()
			day = start - datetime.timedelta(days=800)
			while day < start + datetime.timedelta(days=800):
				self.assertEqual(self._eval(formula, day), day >= start, f"{since} vs {day}")
				day += datetime.timedelta(days=1)

	def test_it_is_one_sided_because_tally_refuses_a_long_two_sided_formula(self):
		self.assertNotIn("<=", agent.since_formula("20260815"))


class TestResolveSince(unittest.TestCase):
	TODAY = datetime.date(2026, 8, 20)

	def test_a_fixed_date_wins_over_days_back(self):
		cfg = make_config("[vouchers]\nfrom_date = 2026-08-01\nfrom_days_back = 7\n")
		self.assertEqual(agent.resolve_since(cfg, self.TODAY), "20260801")

	def test_days_back_counts_from_today_and_crosses_a_month(self):
		self.assertEqual(agent.resolve_since(make_config("[vouchers]\nfrom_days_back = 7\n"), self.TODAY), "20260813")
		self.assertEqual(agent.resolve_since(make_config("[vouchers]\nfrom_days_back = 25\n"), self.TODAY), "20260726")

	def test_nothing_set_means_the_whole_year(self):
		self.assertIsNone(agent.resolve_since(make_config(), self.TODAY))

	def test_dates_may_be_written_three_ways_and_a_bad_one_stops_the_agent(self):
		for text in ("2026-08-15", "15-08-2026", "20260815"):
			self.assertEqual(agent._yyyymmdd(text), "20260815")
		with self.assertRaises(SystemExit):
			agent._yyyymmdd("last week")


class TestTypes(unittest.TestCase):
	def test_tallys_odd_spacing_is_found_whichever_way_it_is_typed(self):
		self.assertEqual(agent._canon_types("Credit Note (Purchase), sales ,Debit Note"), ["Credit Note ( Purchase)", "Sales", "Debit Note"])

	def test_an_unknown_type_stops_the_agent_rather_than_being_ignored(self):
		with self.assertRaises(SystemExit):
			agent._canon_types("Sales, Salez")

	def test_none_given_means_all(self):
		self.assertIsNone(agent._canon_types(""))


class TestSchedule(unittest.TestCase):
	def sched(self, **items):
		return agent.parse_schedule(items)

	def test_daily_runs_today_if_the_time_is_ahead_else_tomorrow(self):
		s = self.sched(run_daily_at="22:00")
		self.assertEqual(agent.next_run(s, DT(2026, 8, 20, 9, 0)), DT(2026, 8, 20, 22, 0))
		self.assertEqual(agent.next_run(s, DT(2026, 8, 20, 22, 0)), DT(2026, 8, 21, 22, 0))
		self.assertEqual(agent.next_run(s, DT(2026, 8, 31, 23, 30)), DT(2026, 9, 1, 22, 0))  # month end
		self.assertEqual(agent.next_run(s, DT(2026, 12, 31, 23, 30)), DT(2027, 1, 1, 22, 0))  # year end

	def test_several_daily_times_pick_the_nearest(self):
		s = self.sched(run_daily_at="08:00, 13:30, 22:00")
		self.assertEqual(agent.next_run(s, DT(2026, 8, 20, 9, 0)), DT(2026, 8, 20, 13, 30))
		self.assertEqual(agent.next_run(s, DT(2026, 8, 20, 23, 0)), DT(2026, 8, 21, 8, 0))

	def test_weekly_runs_on_the_named_day(self):
		s = self.sched(run_weekly="Sun 23:00")
		# 20 Aug 2026 is a Thursday
		self.assertEqual(agent.next_run(s, DT(2026, 8, 20, 9, 0)), DT(2026, 8, 23, 23, 0))
		self.assertEqual(agent.next_run(s, DT(2026, 8, 23, 23, 0)), DT(2026, 8, 30, 23, 0))
		self.assertEqual(agent.next_run(s, DT(2026, 8, 23, 10, 0)), DT(2026, 8, 23, 23, 0))

	def test_every_n_minutes_within_hours(self):
		s = self.sched(run_every_minutes="30", run_between="07:00-22:00")
		self.assertEqual(agent.next_run(s, DT(2026, 8, 20, 9, 10)), DT(2026, 8, 20, 9, 40))
		self.assertEqual(agent.next_run(s, DT(2026, 8, 20, 21, 50)), DT(2026, 8, 21, 7, 0))  # 22:20 is outside the hours
		self.assertEqual(agent.next_run(s, DT(2026, 8, 20, 22, 0)), DT(2026, 8, 21, 7, 0))
		self.assertEqual(agent.next_run(s, DT(2026, 8, 20, 3, 0)), DT(2026, 8, 20, 7, 0))

	def test_every_n_minutes_without_hours_just_repeats(self):
		self.assertEqual(agent.next_run(self.sched(run_every_minutes="15"), DT(2026, 8, 20, 23, 50)), DT(2026, 8, 21, 0, 5))

	def test_hours_that_cross_midnight(self):
		s = self.sched(run_every_minutes="60", run_between="22:00-06:00")
		self.assertEqual(agent.next_run(s, DT(2026, 8, 20, 23, 0)), DT(2026, 8, 21, 0, 0))
		self.assertEqual(agent.next_run(s, DT(2026, 8, 21, 6, 0)), DT(2026, 8, 21, 22, 0))

	def test_styles_can_be_combined_and_the_earliest_wins(self):
		s = self.sched(run_daily_at="22:00", run_every_minutes="30", run_between="09:00-10:00")
		self.assertEqual(agent.next_run(s, DT(2026, 8, 20, 9, 10)), DT(2026, 8, 20, 9, 40))
		self.assertEqual(agent.next_run(s, DT(2026, 8, 20, 11, 0)), DT(2026, 8, 20, 22, 0))

	def test_nothing_set_is_no_schedule(self):
		self.assertIsNone(agent.parse_schedule({}))
		self.assertIsNone(agent.parse_schedule({"run_daily_at": "  "}))

	def test_a_bad_time_or_day_stops_the_agent(self):
		for bad in ({"run_daily_at": "25:00"}, {"run_daily_at": "noon"}, {"run_weekly": "Someday 10:00"}):
			with self.assertRaises(SystemExit):
				agent.parse_schedule(bad)

	def test_it_is_read_from_the_config_file(self):
		cfg = make_config("[schedule]\nrun_daily_at = 22:00\n[vouchers]\nfrom_days_back = 7\ntypes = Sales, Delivery Note\nread_batch = 10\n")
		self.assertEqual(cfg.schedule, {"daily": [(22, 0)]})
		self.assertEqual((cfg.voucher_days_back, cfg.voucher_types, cfg.read_batch), (7, ["Sales", "Delivery Note"], 10))
		plain = make_config()
		self.assertEqual((plain.schedule, plain.voucher_days_back, plain.voucher_types, plain.read_batch), (None, None, None, 25))


class FakeTally:
	"""Stands in for the Tally requests: records what was asked, answers with canned vouchers."""

	def __init__(self, index):
		self.index = index
		self.reads = []  # the guids of each detail request

	def details(self, cfg, guids):
		self.reads.append(list(guids))
		return {g: {"guid": g, "type": "Sales", "number": g, "rows": []} for g in guids if g not in getattr(self, "missing", ())}


class TestSendVouchers(unittest.TestCase):
	def setUp(self):
		self.cfg = make_config("[vouchers]\nread_batch = 2\n")
		self.index = [
			{"guid": f"g{i}", "type": "Sales", "number": f"S{i}", "date": f"202608{10 + i}", "cancelled": False, "optional": False} for i in range(1, 6)
		] + [
			{"guid": "gx", "type": "Sales", "number": "SX", "date": "20260815", "cancelled": True, "optional": False},
			{"guid": "go", "type": "Sales", "number": "SO", "date": "20260815", "cancelled": False, "optional": True},
			{"guid": "gj", "type": "Journal", "number": "J1", "date": "20260815", "cancelled": False, "optional": False},
		]
		self.tally = FakeTally(self.index)
		self.sent = []

		def erp(cfg, method, http="GET", data=None):
			import json

			if method.endswith("filter_new_tally_vouchers"):
				return json.loads(data["guids"])
			batch = json.loads(data["vouchers"])
			self.sent.append([v["guid"] for v in batch])
			return {"created": [{"voucher": v["number"]} for v in batch], "alreadyInErp": 0, "unplaced": [], "ignored": {}, "newItems": [], "newBatches": []}

		patches = [
			mock.patch.object(agent, "tally_voucher_index", side_effect=lambda cfg, since=None: self.index),
			mock.patch.object(agent, "tally_voucher_details", side_effect=self.tally.details),
			mock.patch.object(agent, "erp_call", side_effect=erp),
			mock.patch.object(agent, "tally_item_groups", return_value={}),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

	def test_cancelled_and_optional_vouchers_are_never_read_in_full(self):
		agent.send_vouchers(self.cfg, types=["Sales"])
		read = [g for batch in self.tally.reads for g in batch]
		self.assertNotIn("gx", read)
		self.assertNotIn("go", read)
		self.assertNotIn("gj", read)  # another type

	def test_vouchers_are_read_and_sent_in_batches_of_read_batch_oldest_first(self):
		agent.send_vouchers(self.cfg, types=["Sales"])
		self.assertEqual(self.tally.reads, [["g1", "g2"], ["g3", "g4"], ["g5"]])
		self.assertEqual(self.sent, [["g1", "g2"], ["g3", "g4"], ["g5"]])

	def test_the_window_start_is_passed_to_tally_and_also_applied_here(self):
		with mock.patch.object(agent, "tally_voucher_index", side_effect=lambda cfg, since=None: self.index) as idx:
			agent.send_vouchers(self.cfg, since="20260813", types=["Sales"])
		idx.assert_called_once_with(self.cfg, "20260813")
		self.assertEqual([g for batch in self.sent for g in batch], ["g3", "g4", "g5"])

	def test_a_voucher_tally_does_not_return_is_left_for_the_next_run_not_lost(self):
		self.tally.missing = {"g2"}
		agent.send_vouchers(self.cfg, types=["Sales"])
		self.assertEqual([g for batch in self.sent for g in batch], ["g1", "g3", "g4", "g5"])

	def test_a_dry_run_sends_nothing(self):
		agent.send_vouchers(self.cfg, dry_run=True, types=["Sales"])
		self.assertEqual(self.sent, [])


class TestBatchRead(unittest.TestCase):
	def test_several_vouchers_come_back_from_one_request_keyed_by_id(self):
		xml = "".join(f'<VOUCHER REMOTEID="x"><GUID>{g}</GUID><VOUCHERTYPENAME>Sales</VOUCHERTYPENAME><VOUCHERNUMBER>N-{g}</VOUCHERNUMBER></VOUCHER>' for g in ("a1", "b2", "c3"))
		with mock.patch.object(agent, "_voucher_collection", return_value=xml) as call:
			out = agent.tally_voucher_details(make_config(), ["a1", "b2", "c3"])
		self.assertEqual(sorted(out), ["a1", "b2", "c3"])
		self.assertEqual(out["b2"]["number"], "N-b2")
		self.assertEqual(call.call_count, 1)
		self.assertEqual(call.call_args.kwargs["where"], '$GUID = "a1" OR $GUID = "b2" OR $GUID = "c3"')

	def test_asking_for_nothing_asks_tally_nothing(self):
		with mock.patch.object(agent, "_voucher_collection") as call:
			self.assertEqual(agent.tally_voucher_details(make_config(), []), {})
		call.assert_not_called()


class TestRunScheduled(unittest.TestCase):
	def test_it_waits_for_each_time_runs_the_window_and_survives_a_failed_run(self):
		cfg = make_config("[schedule]\nrun_daily_at = 22:00\n[vouchers]\nfrom_days_back = 7\ntypes = Sales\n")
		now = {"t": DT(2026, 8, 20, 21, 59, 0)}

		def sleep(seconds):
			now["t"] += datetime.timedelta(seconds=seconds)

		calls = []

		def fake_send(cfg_, dry, since, limit, types):
			calls.append((now["t"].strftime("%d %H:%M"), since, types))
			if len(calls) == 1:
				raise urllib.error.URLError("Tally is not running")

		with mock.patch.object(agent, "send_vouchers", side_effect=fake_send), mock.patch.object(agent, "resolve_since", return_value="20260813"):
			agent.run_scheduled(cfg, sleep=sleep, clock=lambda: now["t"], max_runs=2)
		# the first run failed; the second was still made, a day later
		self.assertEqual(calls, [("20 22:00", "20260813", ["Sales"]), ("21 22:00", "20260813", ["Sales"])])

	def test_without_a_schedule_it_stops_with_a_message(self):
		with self.assertRaises(SystemExit):
			agent.run_scheduled(make_config())


if __name__ == "__main__":
	unittest.main()
