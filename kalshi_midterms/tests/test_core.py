"""Offline tests: python -m unittest discover -s kalshi_midterms/tests -t ."""

import unittest
from datetime import date

from kalshi_midterms import kalshi
from kalshi_midterms.model import ModelParams, win_probabilities
from kalshi_midterms.races import (Race, ap_rows, apply_polls, apply_results,
                                   parse_outcome, parse_race)
from kalshi_midterms.value import ScanParams, evaluate_side, scan, taker_fee

TODAY = date(2026, 11, 4)


def poll(race, cand, party, pct, end="2026-11-01", n=800, pollster="P1"):
    return {"race": race, "candidate": cand, "party": party, "pct": str(pct),
            "end_date": end, "sample_size": str(n), "pollster": pollster}


def result(race, cand, party, votes, rep, winner=""):
    return {"race": race, "candidate": cand, "party": party, "votes": str(votes),
            "pct_reporting": str(rep), "winner": winner}


class TestKalshiParsing(unittest.TestCase):
    def test_price_formats(self):
        self.assertAlmostEqual(kalshi.price({"yes_ask_dollars": "0.1200"}, "yes_ask"), 0.12)
        self.assertAlmostEqual(kalshi.price({"yes_ask": 12}, "yes_ask"), 0.12)
        self.assertIsNone(kalshi.price({}, "yes_ask"))

    def test_quote_derives_no_side_and_drops_empty_book(self):
        q = kalshi.quote({"yes_bid": 30, "yes_ask": 33, "volume": 5})
        self.assertAlmostEqual(q["no_ask"], 0.70)
        self.assertAlmostEqual(q["no_bid"], 0.67)
        q = kalshi.quote({"yes_bid": 0, "yes_ask": 100})
        self.assertIsNone(q["yes_ask"])
        self.assertIsNone(q["no_ask"])


class TestRaceMatching(unittest.TestCase):
    def test_parse_race(self):
        self.assertEqual(parse_race({"title": "Georgia Senate Race 2026"}), "GA-SEN")
        self.assertEqual(parse_race({"title": "West Virginia Senate winner?"}), "WV-SEN")
        self.assertEqual(parse_race({"title": "Who will win Arizona's governor race?"}), "AZ-GOV")
        self.assertEqual(parse_race({"title": "California's 13th district: CA-13 House race"}), "CA-H13")
        self.assertEqual(parse_race({"title": "Wyoming at-large House seat"}), "WY-HAL")
        self.assertEqual(parse_race({"title": "Pennsylvania's 7th district House race"}), "PA-H7")
        self.assertEqual(parse_race({"title": "Who will win New York's 22nd congressional district?"}), "NY-H22")
        self.assertEqual(parse_race({"title": "Senate race", "series_ticker": "SENATEMI"}), "MI-SEN")
        self.assertIsNone(parse_race({"title": "Which party will win the House?"}))
        # real Kalshi shapes
        self.assertEqual(parse_race({"series_ticker": "GOVPARTYAZ", "event_ticker": "GOVPARTYAZ-26",
                                     "title": "Who will win the governorship?"}), "AZ-GOV")
        self.assertEqual(parse_race({"series_ticker": "HOUSEPA10", "event_ticker": "HOUSEPA10-26",
                                     "title": "Will Republican win the House race for PA-10?"}), "PA-H10")
        self.assertIsNone(parse_race({"title": "Texas state senate district 5"}))

    def test_parse_outcome(self):
        self.assertEqual(parse_outcome({"yes_sub_title": "Democratic party"})["party"], "D")
        self.assertEqual(parse_outcome({"yes_sub_title": "Republican"})["party"], "R")
        self.assertEqual(parse_outcome({"yes_sub_title": "Pat Example", "ticker": "X-26-R"})["party"], "R")
        real = {"yes_sub_title": "Pat Example", "subtitle": "Democratic party:: Democratic party",
                "title": "Will Democratic win the House race for PA-10?", "ticker": "HOUSEPA10-26-D"}
        self.assertEqual(parse_outcome(real), {"party": "D", "name": "Pat Example"})

    def test_find_by_name_and_party(self):
        r = Race("GA-SEN")
        r.upsert("Alex Alpha", "D")
        r.upsert("Blake Beta", "R")
        self.assertEqual(r.find("", "Blake Beta").party, "R")
        self.assertEqual(r.find("", "Beta wins").name, "Blake Beta")
        self.assertEqual(r.find("D", "Democratic party").name, "Alex Alpha")

    def test_shared_last_name_stays_within_party(self):
        races = {}
        apply_polls(races, [poll("X-SEN", "Sam Smith", "D", 47), poll("X-SEN", "Pat Smith", "R", 48)], TODAY)
        r = races["X-SEN"]
        self.assertEqual(len(r.candidates), 2)
        self.assertEqual(r.find("R", "Republican party").name, "Pat Smith")
        self.assertEqual(r.find("R", "Smith").name, "Pat Smith")


class TestPollsAndResults(unittest.TestCase):
    def test_recency_weighting(self):
        races = {}
        apply_polls(races, [poll("X-SEN", "A", "D", 40, "2026-09-01", pollster="old"),
                            poll("X-SEN", "A", "D", 50, "2026-11-02", pollster="new")], TODAY)
        share = races["X-SEN"].candidates[0].poll_share
        self.assertGreater(share, 48)  # new poll dominates
        self.assertEqual(races["X-SEN"].n_polls, 2)

    def test_polls_and_results_merge_on_name(self):
        races = {}
        apply_polls(races, [poll("X-SEN", "Alex Alpha", "D", 48)], TODAY)
        apply_results(races, [result("X-SEN", "Alex Alpha", "Dem", 1000, 40)])
        self.assertEqual(len(races["X-SEN"].candidates), 1)
        c = races["X-SEN"].candidates[0]
        self.assertAlmostEqual(c.poll_share, 48.0)
        self.assertEqual((c.votes, races["X-SEN"].reporting), (1000.0, 0.4))

    def test_ap_rows(self):
        payload = {"races": [{"officeID": "H", "seatNum": "7", "reportingUnits": [
            {"level": "state", "statePostal": "VA", "eevp": 81.5, "candidates": [
                {"first": "A", "last": "Alpha", "party": "Dem", "voteCount": 10, "winner": "X"}]}]}]}
        rows = ap_rows(payload)
        self.assertEqual(rows[0]["race"], "VA-H7")
        races = {}
        apply_results(races, rows)
        self.assertTrue(races["VA-H7"].candidates[0].winner)
        self.assertAlmostEqual(races["VA-H7"].reporting, 0.815)


class TestModel(unittest.TestCase):
    def race(self, poll_d, poll_r, votes=None, rep=None):
        races = {}
        apply_polls(races, [poll("X-SEN", "Dee", "D", poll_d), poll("X-SEN", "Arr", "R", poll_r)], TODAY)
        if votes:
            apply_results(races, [result("X-SEN", "Dee", "D", votes[0], rep),
                                  result("X-SEN", "Arr", "R", votes[1], rep)])
        return races["X-SEN"]

    def probs(self, race):
        p = win_probabilities(race)
        return p["D:dee"]["p"], p["R:arr"]["p"]

    def test_tied_poll_is_coin_flip(self):
        d, r = self.probs(self.race(47, 47))
        self.assertAlmostEqual(d, 0.5, places=6)
        self.assertAlmostEqual(d + r, 1.0, places=9)

    def test_poll_lead_gives_sensible_probability(self):
        d, _ = self.probs(self.race(50, 45))  # +5 with sigma 5.5 -> ~82%
        self.assertTrue(0.78 < d < 0.86, d)

    def test_count_dominates_late(self):
        # Polls say R +4, but D leads by 6 with 95% counted -> D strong favourite.
        d, _ = self.probs(self.race(46, 50, votes=(530_000, 470_000), rep=95))
        self.assertGreater(d, 0.97)

    def test_early_count_barely_moves(self):
        d_poll, _ = self.probs(self.race(46, 50))
        d_early, _ = self.probs(self.race(46, 50, votes=(5300, 4700), rep=3))
        self.assertLess(abs(d_early - d_poll), 0.1)

    def test_called_race(self):
        races = {}
        apply_results(races, [result("X-SEN", "Dee", "D", 10, 99, "X"),
                              result("X-SEN", "Arr", "R", 9, 99)])
        p = win_probabilities(races["X-SEN"], ModelParams(called_prob=0.99))
        self.assertEqual(p["D:dee"]["basis"], "called")
        self.assertAlmostEqual(p["D:dee"]["p"], 0.99)

    def test_no_data_no_model(self):
        r = Race("X-SEN")
        r.upsert("Dee", "D")
        self.assertEqual(win_probabilities(r), {})


class TestValue(unittest.TestCase):
    def test_fee_schedule(self):
        # 0.07 * 100 * 0.5 * 0.5 = $1.75 for 100 contracts at 50c
        self.assertAlmostEqual(taker_fee(0.50), 0.0175)
        # 0.07 * 100 * 0.1 * 0.9 = 0.63
        self.assertAlmostEqual(taker_fee(0.10), 0.0063)
        # single contract rounds up to a full cent
        self.assertAlmostEqual(taker_fee(0.10, contracts=1), 0.01)

    def test_evaluate_side(self):
        e = evaluate_side(0.30, 0.20, 0.18, ScanParams())
        self.assertTrue(e["longshot"])
        self.assertAlmostEqual(e["edge"], 0.30 - 0.20 - 0.0112, places=4)
        self.assertGreater(e["kelly"], 0)
        self.assertIsNone(evaluate_side(0.3, None, 0.1, ScanParams()))

    def test_scan_end_to_end(self):
        races = {}
        apply_polls(races, [poll("GA-SEN", "Alex Alpha", "D", 47),
                            poll("GA-SEN", "Blake Beta", "R", 48)], TODAY)
        events = [{"event_ticker": "SENATEGA-26", "series_ticker": "SENATEGA",
                   "title": "Georgia Senate race", "markets": [
                       {"ticker": "SENATEGA-26-D", "yes_sub_title": "Democratic party",
                        "yes_bid": 15, "yes_ask": 17, "volume": 5000, "status": "active"},
                       {"ticker": "SENATEGA-26-R", "yes_sub_title": "Republican party",
                        "yes_bid": 83, "yes_ask": 85, "volume": 5000, "status": "active"}]},
                  {"event_ticker": "CONTROLH-26", "title": "Which party will win the House?",
                   "markets": [{"ticker": "CONTROLH-26-D", "yes_sub_title": "Democrats",
                                "yes_ask": 60, "yes_bid": 58}]}]
        out = scan(events, races)
        best = out["opportunities"][0]
        # 1-pt poll deficit is ~43% to win; market says 17c -> long-shot YES on D
        self.assertEqual((best["ticker"], best["side"]), ("SENATEGA-26-D", "YES"))
        self.assertTrue(best["longshot"])
        self.assertGreater(best["edge"], 0.2)
        self.assertEqual(out["unmatched"][0]["why"], "no race key")
        # override pins a market the parser can't read
        out = scan(events, races, overrides={"CONTROLH-26-D": {"race": "GA-SEN", "party": "D"}})
        self.assertFalse(out["unmatched"])


class TestArbitrage(unittest.TestCase):
    def ev(self, d, r):
        return [{"event_ticker": "E", "title": "Georgia Senate race", "markets": [
            {"ticker": "E-D", "yes_sub_title": "Democratic party", **d},
            {"ticker": "E-R", "yes_sub_title": "Republican party", **r}]}]

    def test_yes_bundle(self):
        from kalshi_midterms.value import find_arbitrage
        arb = find_arbitrage(self.ev({"yes_ask": 40, "yes_bid": 38}, {"yes_ask": 50, "yes_bid": 48}))
        self.assertEqual(arb[0]["kind"], "YES bundle")
        self.assertAlmostEqual(arb[0]["profit"], 1 - (0.40 + 0.0168 + 0.50 + 0.0175), places=4)

    def test_no_bundle(self):
        from kalshi_midterms.value import find_arbitrage
        # YES bids sum to 1.10 -> NO asks 0.45 + 0.35 = 0.80 < payout 1
        arb = find_arbitrage(self.ev({"yes_ask": 60, "yes_bid": 55}, {"yes_ask": 70, "yes_bid": 65}))
        self.assertEqual([a["kind"] for a in arb], ["NO bundle"])

    def test_fair_market_has_none(self):
        from kalshi_midterms.value import find_arbitrage
        self.assertEqual(find_arbitrage(self.ev({"yes_ask": 52, "yes_bid": 50},
                                                {"yes_ask": 50, "yes_bid": 48})), [])


if __name__ == "__main__":
    unittest.main()
