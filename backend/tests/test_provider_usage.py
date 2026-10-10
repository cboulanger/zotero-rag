"""Rate-limit header parsing shared by every provider (spec A9)."""

import unittest

from backend.providers.usage import (
    parse_duration,
    parse_ietf_headers,
    parse_openai_headers,
    parse_standard_headers,
    period_name,
)

AS_OF = "2026-10-10T12:00:00+00:00"


def std(headers):
    return parse_standard_headers(headers, side="embedding", as_of=AS_OF)


class TestParseDuration(unittest.TestCase):
    def test_plain_seconds(self):
        self.assertEqual(parse_duration("30"), 30.0)

    def test_go_style_durations(self):
        self.assertEqual(parse_duration("1s"), 1.0)
        self.assertEqual(parse_duration("6m0s"), 360.0)
        self.assertEqual(parse_duration("1h2m3s"), 3723.0)
        self.assertAlmostEqual(parse_duration("20ms"), 0.02)
        self.assertAlmostEqual(parse_duration("1m30.5s"), 90.5)

    def test_garbage_is_none(self):
        for bad in ("", "abc", "1x", "6m0", "s", "--"):
            with self.subTest(value=bad):
                self.assertIsNone(parse_duration(bad))


class TestPeriodName(unittest.TestCase):
    def test_known_and_unusual_windows(self):
        self.assertEqual(period_name(60), "minute")
        self.assertEqual(period_name(3600), "hour")
        self.assertEqual(period_name(86400), "day")
        self.assertEqual(period_name(90), "90s")
        self.assertIsNone(period_name(None))
        self.assertIsNone(period_name(0))


class TestOpenAIStyle(unittest.TestCase):
    def test_requests_and_tokens_meters(self):
        meters = std({
            "x-ratelimit-limit-requests": "500",
            "x-ratelimit-remaining-requests": "499",
            "x-ratelimit-reset-requests": "6m0s",
            "x-ratelimit-limit-tokens": "30000",
            "x-ratelimit-remaining-tokens": "29000",
            "x-ratelimit-reset-tokens": "1s",
        })
        by_unit = {m.unit: m for m in meters}
        self.assertEqual(set(by_unit), {"requests", "tokens"})
        self.assertEqual((by_unit["requests"].limit, by_unit["requests"].remaining), (500, 499))
        self.assertEqual(by_unit["requests"].resets_at, "2026-10-10T12:06:00+00:00")
        self.assertEqual(by_unit["tokens"].resets_at, "2026-10-10T12:00:01+00:00")
        self.assertEqual(by_unit["requests"].side, "embedding")
        self.assertEqual(by_unit["requests"].as_of, AS_OF)

    def test_header_names_are_case_insensitive(self):
        meters = std({"X-RateLimit-Limit-Requests": "10", "X-RateLimit-Remaining-Requests": "4"})
        self.assertEqual([(m.unit, m.limit, m.remaining) for m in meters], [("requests", 10, 4)])

    def test_missing_remaining_yields_no_meter(self):
        self.assertEqual(std({"x-ratelimit-limit-requests": "10"}), [])

    def test_non_numeric_values_yield_no_meter(self):
        self.assertEqual(std({"x-ratelimit-limit-requests": "lots", "x-ratelimit-remaining-requests": "some"}), [])

    def test_remaining_above_limit_is_clamped(self):
        (meter,) = std({"x-ratelimit-limit-requests": "10", "x-ratelimit-remaining-requests": "99"})
        self.assertEqual(meter.remaining, 10)

    def test_zero_limit_and_negative_remaining_are_dropped(self):
        self.assertEqual(std({"x-ratelimit-limit-requests": "0", "x-ratelimit-remaining-requests": "0"}), [])
        self.assertEqual(std({"x-ratelimit-limit-requests": "10", "x-ratelimit-remaining-requests": "-1"}), [])


class TestIETF(unittest.TestCase):
    def test_basic_triplet(self):
        (meter,) = std({"RateLimit-Limit": "100", "RateLimit-Remaining": "40", "RateLimit-Reset": "30"})
        self.assertEqual((meter.unit, meter.limit, meter.remaining), ("requests", 100, 40))
        self.assertIsNone(meter.period)
        self.assertEqual(meter.resets_at, "2026-10-10T12:00:30+00:00")

    def test_policy_window_gives_the_period(self):
        (meter,) = std({
            "RateLimit-Limit": "100", "RateLimit-Remaining": "40",
            "RateLimit-Policy": '"default";q=100;w=3600',
        })
        self.assertEqual(meter.period, "hour")
        self.assertEqual(meter.id, "requests/hour")

    def test_policy_alone_can_supply_the_limit(self):
        (meter,) = std({"RateLimit-Remaining": "5", "RateLimit-Policy": "100;w=60"})
        self.assertEqual((meter.limit, meter.remaining, meter.period), (100, 5, "minute"))

    def test_combined_structured_field_form(self):
        (meter,) = std({
            "RateLimit": '"default";r=50;t=30',
            "RateLimit-Policy": '"default";q=100;w=60',
        })
        self.assertEqual((meter.limit, meter.remaining, meter.period), (100, 50, "minute"))
        self.assertEqual(meter.resets_at, "2026-10-10T12:00:30+00:00")

    def test_garbage_yields_nothing(self):
        self.assertEqual(std({"RateLimit-Limit": "x", "RateLimit-Remaining": "y"}), [])


class TestStandardCombined(unittest.TestCase):
    def test_empty_and_unrelated_headers(self):
        self.assertEqual(std({}), [])
        self.assertEqual(std(None), [])
        self.assertEqual(std({"content-type": "application/json", "retry-after": "5"}), [])

    def test_both_dialects_together_openai_first(self):
        meters = std({
            "x-ratelimit-limit-requests": "10", "x-ratelimit-remaining-requests": "9",
            "RateLimit-Limit": "100", "RateLimit-Remaining": "50",
        })
        self.assertEqual([m.limit for m in meters], [10, 100])

    def test_kisski_dialect_is_not_understood_by_the_generic_parser(self):
        self.assertEqual(std({"x-ratelimit-limit-hour": "100", "x-ratelimit-remaining-hour": "40"}), [])

    def test_individual_parsers_are_exposed(self):
        self.assertEqual(parse_openai_headers({}, side="llm", as_of=AS_OF), [])
        self.assertEqual(parse_ietf_headers({}, side="llm", as_of=AS_OF), [])


if __name__ == "__main__":
    unittest.main()
