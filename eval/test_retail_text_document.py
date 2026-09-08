"""Synthetic sources exercise reviewed text formats, exact values and lineage."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import hashlib
from pathlib import Path
import unittest

from retail_ops.ingestion import preview, text_preview
from retail_ops.ingestion.text_document import (
    parse_document, reject_excluded_text, validate_text_locator,
)


ROOT = Path(__file__).resolve().parents[1]
DAY = "店铺：B\n时间范围2026-09-07至2026-09-07\n"
METRICS = "成交金额100.50\n成交订单量0\n搜索曝光人数\n"
SEARCH = "本店top3搜索词：词甲（曝光次数1，点击次数0），词乙（成单次数），词丙（曝光次数2）\n"
VOLUME = "商品销量top3交易商品：商品甲（销量0），商品乙（销量），商品乙（销量）\n"
AMOUNT = "top3交易商品成交金额：商品2026（成交金额100元），商品乙（成交金额），商品丙（成交金额0元）\n"


class RetailTextDocumentTests(unittest.TestCase):
    def parse(self, text=DAY + METRICS, version="manual_text_v3", **kwargs):
        return parse_document(ROOT, text.encode("utf-8"), version, **kwargs)

    def row(self, result, group=0, row=0):
        return result["groups"][group]["preview"]["validated_records"][row]

    def held(self, result):
        self.assertEqual(result["status"], "quarantined", result)
        self.assertTrue(result["errors"])
        for group in result["groups"]:
            self.assertEqual(group["preview"]["status"], "quarantined")
            self.assertEqual(group["preview"]["validated_records"], [])

    def test_explicit_daily_source_has_null_month_and_original_hash(self):
        result = self.parse()
        self.assertEqual(result["status"], "validated", result)
        group = result["groups"][0]
        self.assertEqual(group["source_block_line"], 1)
        self.assertEqual(group["dataset_id"], "store_period_panel_metrics")
        row = self.row(result)
        self.assertEqual((row["record"]["period_start"], row["record"]["period_end"]),
                         ("2026-09-07", "2026-09-07"))
        self.assertIsNone(row["record"]["period_month"])
        self.assertEqual(row["source_line_end"], 5)
        self.assertEqual(group["preview"]["file_sha256"], hashlib.sha256((DAY + METRICS).encode()).hexdigest())
        self.assertEqual(group["preview"]["mapping_version"], "manual_text_v3")

    def test_v3_actual_dates_cover_cross_month_year_and_leap_day(self):
        for window, month in (("2026-08-30至2026-09-07", None),
                              ("2025-12-30至2026-01-02", None),
                              ("2028-02-29至2028-02-29", None),
                              ("2026-03-01至2026-03-31", "2026-03")):
            with self.subTest(window=window):
                result = self.parse("店铺：B\n时间范围" + window + "\n成交金额1\n")
                self.assertEqual(result["status"], "validated", result)
                self.assertEqual(self.row(result)["record"]["period_month"], month)

    def test_v3_invalid_reversed_implicit_dates_are_held(self):
        for window in ("2026-02-29至2026-02-29", "2026-09-08至2026-09-07",
                       "2026-9-7至2026-9-7", "2026-09-07", "2026.9", "昨天", "截至0点",
                       "2026-09-07至09-08", "2026-09-07/2026-09-08"):
            with self.subTest(window=window):
                self.held(self.parse("店铺：B\n时间范围" + window + "\n成交金额1\n"))

    def test_v1_v2_monthly_preview_rules_stay_unchanged(self):
        for version in ("manual_text_v1", "manual_text_v2"):
            for window in ("2026.3", "2026-03", "2026.3.1-3.31"):
                result = text_preview.preview_text(ROOT, ("店铺：B\n时间范围" + window + "\n成交金额0\n").encode(), version)
                self.assertEqual(result["status"], "validated", result)
                self.assertEqual(result["validated_records"][0]["record"]["period_month"], "2026-03")
            self.assertEqual(text_preview.preview_text(ROOT, (DAY + METRICS).encode(), version)["status"], "quarantined")

    def test_v2_document_records_equal_existing_preview_records(self):
        text = "店铺：B\n时间范围2026.3\n成交金额100.50\n搜索曝光10\n" + SEARCH + VOLUME + AMOUNT
        old = text_preview.preview_text(ROOT, text.encode(), "manual_text_v2")
        new = self.parse(text, "manual_text_v2")
        self.assertEqual(new["status"], "validated", new)
        self.assertEqual([entry["record"] for entry in old["validated_records"]],
            [entry["record"] for group in new["groups"] for entry in group["preview"]["validated_records"]])

    def test_v3_requires_explicit_user_units(self):
        for label in ("搜索曝光", "搜索入店", "商家列表曝光", "商家列表入店",
                      "活动专区曝光", "活动专区入店", "订单页入店", "其他入店"):
            with self.subTest(label=label):
                self.held(self.parse(DAY + label + "10\n"))
                self.held(self.parse(DAY + METRICS + label + "\n"))
        result = self.parse(DAY + "搜索曝光人数10\n入店次数11\n")
        self.assertEqual(result["status"], "validated")
        self.assertEqual(self.row(result)["record"]["search_exposure_users"], 10)
        self.assertEqual(self.row(result)["record"]["entry_times"], 11)

    def test_v3_requires_labelled_sku_amount_and_preserves_digit_name(self):
        result = self.parse(DAY + AMOUNT)
        self.assertEqual(result["status"], "validated", result)
        self.assertEqual(self.row(result)["record"]["sku_name"], "商品2026")
        self.assertEqual(self.row(result)["record"]["sku_transaction_amount"], Decimal("100"))
        self.held(self.parse(DAY + AMOUNT.replace("商品2026（成交金额100元）", "商品2026100元")))

    def test_absent_values_have_no_invented_lineage_and_explicit_blank_does(self):
        row = self.row(self.parse())
        self.assertEqual(row["record"]["transaction_orders"], 0)
        self.assertIsNone(row["record"]["search_exposure_users"])
        self.assertIsNone(row["record"]["entry_users"])
        fields = {field for item in row["source_locator"]["values"] for field in item["fields"]}
        self.assertIn("search_exposure_users", fields)
        self.assertNotIn("entry_users", fields)

    def test_large_integer_and_decimal_precision_are_preserved(self):
        number = "900719925474099312345678901234567890"
        amount = number + ".0000000000000000007"
        row = self.row(self.parse(DAY + "成交订单量" + number + "\n成交金额" + amount + "\n"))
        self.assertEqual(row["record"]["transaction_orders"], int(number))
        self.assertEqual(row["record"]["transaction_amount"], Decimal(amount))
        self.assertIn(amount, preview.preview_json(row))

    def test_bom_crlf_indentation_and_unicode_columns_refer_to_original(self):
        text = "\ufeff  店铺：B  \r\n 时间范围2026-09-07至2026-09-07\r\n  成交金额1.20\r\n" + AMOUNT.replace("\n", "\r\n")
        result = self.parse(text)
        self.assertEqual(result["status"], "validated", result)
        originals = text.encode().decode("utf-8-sig").splitlines()
        for group in result["groups"]:
            for row in group["preview"]["validated_records"]:
                locator = row["source_locator"]
                for span in [locator["store"], locator["window"], *(value["span"] for value in locator["values"])]:
                    self.assertEqual(originals[span["line"] - 1][span["column_start"] - 1:span["column_end"] - 1], span["text"])
        self.assertEqual(self.row(result)["source_locator"]["store"]["column_start"], 3)

    def test_same_line_identical_rank_items_have_distinct_column_spans(self):
        result = self.parse(DAY + VOLUME)
        rows = result["groups"][0]["preview"]["validated_records"]
        self.assertEqual(len(rows), 3)
        self.assertEqual([row["source_line_end"] for row in rows], [3, 3, 3])
        self.assertEqual([row["source_locator"]["item_ordinal"] for row in rows], [1, 2, 3])
        spans = [row["source_locator"]["values"][0]["span"] for row in rows]
        self.assertEqual(spans[1]["text"], spans[2]["text"])
        self.assertLess(spans[1]["column_end"], spans[2]["column_start"])

    def test_ranking_only_document_has_no_phantom_store_metrics(self):
        result = self.parse(DAY + SEARCH + VOLUME + AMOUNT)
        self.assertEqual(result["status"], "validated", result)
        self.assertEqual([group["dataset_id"] for group in result["groups"]], [
            "demo2_top_search_terms", "demo2_top_skus_by_sales_volume", "demo2_top_skus_by_transaction_amount"])

    def test_only_store_window_headers_are_not_a_metric_snapshot(self):
        result = self.parse(DAY)
        self.held(result)
        self.assertEqual(result["groups"], [])

    def test_multiple_store_windows_keep_their_actual_block_lines(self):
        result = self.parse(DAY + "成交金额1\n" + DAY.replace("B", "C") + "成交金额2\n")
        self.assertEqual(result["status"], "validated", result)
        self.assertEqual([group["source_block_line"] for group in result["groups"]], [1, 4])
        self.assertEqual([self.row(result, index)["record"]["store_id"] for index in (0, 1)], ["B", "C"])

    def test_unknown_metric_or_section_holds_entire_document(self):
        for bad in ("营业额100", "搜索曝光次数10", "成交金额变化100", "新报表："):
            with self.subTest(bad=bad):
                self.held(self.parse(DAY + METRICS + bad + "\n支付金额99\n" + DAY.replace("B", "C") + METRICS))

    def test_duplicate_field_and_repeated_window_are_not_selected(self):
        for suffix in ("成交金额200\n", DAY + METRICS, "时间范围2026-09-08至2026-09-08\n成交金额2\n"):
            self.held(self.parse(DAY + METRICS + suffix))

    def test_excluded_values_are_rejected_everywhere_without_quoted_copies(self):
        for prefix in ("", DAY + METRICS, DAY + "新报表：\n", "店铺：Z\n"):
            for label in ("有效订单数", "无效订单数"):
                data = (prefix + label + "998877\n").encode()
                with self.assertRaisesRegex(ValueError, "no bytes were archived"):
                    reject_excluded_text(data)
                result = parse_document(ROOT, data, "manual_text_v3")
                self.held(result)
                self.assertEqual(result["groups"], [])
                self.assertNotIn("998877", preview.preview_json(result))

    def test_invalid_utf8_cannot_bypass_prearchive_exclusion_check(self):
        with self.assertRaisesRegex(ValueError, "invalid UTF-8"):
            reject_excluded_text(b"\xff\xfe")
        self.held(parse_document(ROOT, b"\xff\xfe", "manual_text_v3"))

    def test_unregistered_persistent_text_profile_is_held(self):
        for version in ("manual_text_v1", "manual_text_v4", "canonical_csv_v2", None, []):
            self.held(parse_document(ROOT, (DAY + METRICS).encode(), version))

    def test_model_proposals_cannot_change_values_or_routes(self):
        text = DAY + AMOUNT
        result = self.parse(text)
        group = result["groups"][0]
        proposals = [{"dataset_id": group["dataset_id"], "grain": group["context"]["grain"],
                      "ranking_basis": group["context"]["ranking_basis"], "record": deepcopy(entry["record"])}
                     for entry in group["preview"]["validated_records"]]
        self.assertEqual(self.parse(text, proposals=proposals)["status"], "validated")
        altered = deepcopy(proposals)
        altered[0]["record"]["sku_transaction_amount"] = Decimal("999")
        self.held(self.parse(text, proposals=altered))
        altered = deepcopy(proposals)
        altered[0]["ranking_basis"] = "sales_volume"
        self.held(self.parse(text, proposals=altered))
        self.held(self.parse(text, proposals=proposals[:-1]))

    def test_locator_checks_original_scope_line_end_and_known_fields(self):
        row = self.row(self.parse())
        for mutate in (
            lambda loc: loc.update(unregistered=True),
            lambda loc: loc.update(source_block_line=True),
            lambda loc: loc["store"].update(text="店铺：C"),
            lambda loc: loc["values"][0]["span"].update(column_end=99),
            lambda loc: loc["values"][0].update(fields=["gross_revenue"]),
            lambda loc: loc.update(item_ordinal=1),
        ):
            locator = deepcopy(row["source_locator"])
            mutate(locator)
            with self.assertRaises(ValueError):
                validate_text_locator(locator, row["record"], row["source_line_end"])
        with self.assertRaises(ValueError):
            validate_text_locator(row["source_locator"], row["record"], row["source_line_end"] + 1)

    def test_locator_allows_requested_projection_without_fake_values(self):
        row = self.row(self.parse())
        record = {field: value if field in {"store_id", "period_start", "period_end", "period_month", "transaction_amount"}
                  else None for field, value in row["record"].items()}
        validate_text_locator(row["source_locator"], record, row["source_line_end"])
        record["payment_amount"] = Decimal("100")
        with self.assertRaises(ValueError):
            validate_text_locator(row["source_locator"], record, row["source_line_end"])


if __name__ == "__main__":
    unittest.main()
