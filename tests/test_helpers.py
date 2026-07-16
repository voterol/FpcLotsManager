import unittest
import ast
import re
from html import escape
from math import isfinite
from pathlib import Path
from urllib.parse import urlparse
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None


PLUGIN_PATH = Path(__file__).parents[1] / "LotsManager.py"
HELPERS = {
    "html_text",
    "sanitize_lot_fields",
    "parse_optional_bool",
    "validate_price_value",
    "lot_title",
    "lot_price_number",
    "apply_lot_filters",
    "build_lot_link_messages",
    "parse_lot_select_fields",
    "discover_lot_create_url",
    "parse_lot_form_defaults",
    "unsupported_required_lot_fields",
    "parse_subcategory_ids_input",
    "version_tuple",
    "validate_update_source",
    "parse_version_document",
    "update_action",
    "normalize_pending_restart",
    "pending_restart_token",
    "pending_restart_is_actionable",
    "auto_update_allowed",
    "normalize_recipient_ids",
    "startup_notice_plan",
    "build_update_urls",
    "normalize_updater_settings",
}


def load_helpers():
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"), filename=str(PLUGIN_PATH))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id in {"LIMITS", "SYSTEM_LOT_FIELD_NAMES"} for target in node.targets):
                selected.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in HELPERS:
            selected.append(node)
    namespace = {
        "escape": escape, "isfinite": isfinite, "BeautifulSoup": BeautifulSoup,
        "urlparse": urlparse, "ast": ast, "re": re,
        "NAME": "LotsManager", "UUID": "5693f220-bcc6-4f6e-9745-9dee8664cbb2",
        "UPDATER_OWNER": "voterol", "UPDATER_REPO": "FpcLotsManager", "UPDATER_SOURCE_PATH": "LotsManager.py",
        "UPDATER_VERSION_PATH": "VERSION", "UPDATER_SETTINGS_SCHEMA": 2,
        "updater_settings": {"schema": 2, "enabled": True, "last_checked_at": 0, "last_commit": None,
                             "last_version": None, "startup_notice_version": None,
                             "startup_notice_recipients": [], "startup_notice_recipients_version": None,
                             "pending_restart": None},
    }
    module = ast.Module(body=selected, type_ignores=[])
    module.body.insert(0, ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0))
    module = ast.fix_missing_locations(module)
    exec(compile(module, str(PLUGIN_PATH), "exec"), namespace)
    return namespace


class HelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = load_helpers()

    def test_html_text_escapes_untrusted_markup(self):
        self.assertEqual(self.helpers["html_text"]('<b>x</b> & "y"'), "&lt;b&gt;x&lt;/b&gt; &amp; &quot;y&quot;")

    def test_sanitizer_removes_secrets_without_mutating_input(self):
        fields = {"price": "10", "csrf_token": "x", "offer_id": "1", "golden_key": "g", "secrets": "s", "auto_delivery": True}
        result = self.helpers["sanitize_lot_fields"](fields)
        self.assertEqual(result, {"price": "10"})
        self.assertIn("secrets", fields)

    def test_sanitizer_can_keep_delivery_data_only(self):
        fields = {"csrf_token": "x", "offer_id": "1", "secrets": "s", "auto_delivery": True}
        result = self.helpers["sanitize_lot_fields"](fields, include_delivery_secrets=True)
        self.assertEqual(result, {"secrets": "s", "auto_delivery": True})

    def test_optional_bool_is_strict(self):
        parse = self.helpers["parse_optional_bool"]
        self.assertFalse(parse("false"))
        self.assertTrue(parse("да"))
        with self.assertRaises(ValueError):
            parse("sometimes")

    def test_price_rejects_non_finite_and_out_of_range_values(self):
        validate = self.helpers["validate_price_value"]
        for value in ("nan", "inf", "-inf", "0", "1000000"):
            self.assertFalse(validate(value)[0], value)
        self.assertEqual(validate("99,99"), (True, "", 99.99))

    def test_lot_filters_support_ranges_status_and_sorting(self):
        class Lot:
            def __init__(self, lot_id, title, price, active):
                self.id = lot_id
                self.description = title
                self.price = price
                self.active = active

        lots = [Lot(1, "Zulu", "20", True), Lot(2, "Alpha", "10", False), Lot(3, "Medium", "15", True)]
        defaults = {"status": "all", "title_query": None, "price_min": None, "price_max": None, "title_len_min": None, "title_len_max": None, "sort": "default"}
        apply = self.helpers["apply_lot_filters"]
        filters = {**defaults, "status": "active", "price_min": 16, "sort": "price_desc"}
        self.assertEqual([lot.id for lot in apply(lots, filters)], [1])
        filters = {**defaults, "title_len_min": 6, "sort": "alpha_asc"}
        self.assertEqual([lot.id for lot in apply(lots, filters)], [3])
        filters = {**defaults, "status": "inactive", "price_max": 10}
        self.assertEqual([lot.id for lot in apply(lots, filters)], [2])
        filters = {**defaults, "price_min": 100}
        self.assertEqual(apply(lots, filters), [])

    def test_lot_filters_search_titles_case_insensitively(self):
        class Lot:
            def __init__(self, lot_id, title, price="10", active=True):
                self.id = lot_id
                self.description = title
                self.price = price
                self.active = active

        lots = [Lot(1, "Телефон Samsung"), Lot(2, "Alpha Product", active=False), Lot(3, "Другое")]
        apply = self.helpers["apply_lot_filters"]
        defaults = {"status": "all", "title_query": None, "price_min": None, "price_max": None, "title_len_min": None, "title_len_max": None, "sort": "default"}
        self.assertEqual([lot.id for lot in apply(lots, {**defaults, "title_query": "ТЕЛЕФОН"})], [1])
        self.assertEqual([lot.id for lot in apply(lots, {**defaults, "title_query": "product"})], [2])
        self.assertEqual([lot.id for lot in apply(lots, {**defaults, "title_query": "alpha", "status": "active"})], [])
        self.assertEqual([lot.id for lot in apply(lots, {**defaults, "title_query": "   "})], [1, 2, 3])

    def test_lot_link_messages_include_every_link_and_escape_titles(self):
        class Lot:
            def __init__(self, lot_id, title):
                self.id = lot_id
                self.description = title

        lots = [Lot(1, "Телефон <тест>"), Lot(2, "Alpha & Beta"), Lot(3, "Другой лот")]
        messages = self.helpers["build_lot_link_messages"](lots, max_length=256)
        combined = "\n".join(messages)
        self.assertTrue(messages)
        self.assertTrue(all(len(message) <= 256 for message in messages))
        self.assertIn("Телефон &lt;тест&gt;", combined)
        self.assertIn("Alpha &amp; Beta", combined)
        for lot in lots:
            self.assertEqual(combined.count(f"https://funpay.com/lots/offer?id={lot.id}"), 1)

    def test_lot_link_messages_respect_telegram_utf16_limit(self):
        class Lot:
            def __init__(self, lot_id):
                self.id = lot_id
                self.description = "😀" * 100

        messages = self.helpers["build_lot_link_messages"]([Lot(i) for i in range(1, 30)])
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message.encode("utf-16-le")) // 2 <= 3900 for message in messages))

    def test_parse_lot_select_fields_extracts_visible_allowed_options(self):
        if BeautifulSoup is None:
            self.skipTest("BeautifulSoup is provided by the Cardinal runtime, but is not installed locally")
        parse = self.helpers["parse_lot_select_fields"]
        html = """
        <form class="form-offer-editor">
          <div class="form-group"><label for="type">Type</label><select id="type" name="fields[type]">
            <option value="nitro">With Nitro</option><option value="none" selected>Without Nitro</option>
            <option value="old" disabled>Unavailable</option>
          </select></div>
          <div class="form-group"><span class="control-label">Method of obtaining</span>
            <select name="fields[method]"><option value="login" selected>By logging in to the account</option></select>
          </div>
          <div class="form-group hidden"><select name="fields[secret]"><option selected value="x">Secret</option></select></div>
          <div class="form-group"><select name="node_id"><option selected value="123">Node</option></select></div>
        </form>
        """
        fields = parse(html)
        self.assertEqual([(item["label"], item["value_label"]) for item in fields], [
            ("Type", "Without Nitro"),
            ("Method of obtaining", "By logging in to the account"),
        ])
        self.assertEqual(fields[0]["options"], [
            {"value": "nitro", "label": "With Nitro"},
            {"value": "none", "label": "Without Nitro"},
        ])

    def test_parse_lot_select_fields_rejects_missing_form(self):
        if BeautifulSoup is None:
            self.skipTest("BeautifulSoup is provided by the Cardinal runtime, but is not installed locally")
        self.assertEqual(self.helpers["parse_lot_select_fields"]("<html></html>"), [])

    def test_parse_lot_select_fields_defaults_to_first_option(self):
        if BeautifulSoup is None:
            self.skipTest("BeautifulSoup is provided by the Cardinal runtime, but is not installed locally")
        fields = self.helpers["parse_lot_select_fields"]("""
            <form class="form-offer-editor"><div class="form-group">
            <label>Type</label><select name="fields[type]"><option value="first">First</option></select>
            </div></form>
        """)
        self.assertEqual(fields[0]["value"], "first")
        self.assertEqual(fields[0]["value_label"], "First")

    def test_discovers_safe_create_url_and_form_defaults(self):
        if BeautifulSoup is None:
            self.skipTest("BeautifulSoup is provided by the Cardinal runtime, but is not installed locally")
        html = """
          <a href="https://evil.example/lots/offerEdit?offer=0">bad</a>
          <a data-href="/lots/offerEdit?offer=0&amp;node=123">create</a>
          <form class="form-offer-editor">
            <input name="node_id" value="123"><input name="active" type="checkbox" checked>
            <input name="ignored" type="checkbox"><textarea name="fields[desc][ru]">Text</textarea>
            <select name="fields[type]"><option value="one">One</option></select>
          </form>
        """
        self.assertEqual(self.helpers["discover_lot_create_url"](html), "/lots/offerEdit?offer=0&node=123")
        self.assertEqual(self.helpers["parse_lot_form_defaults"](html), {
            "node_id": "123", "active": "on", "fields[desc][ru]": "Text", "fields[type]": "one"
        })

    def test_detects_unsupported_empty_required_fields(self):
        if BeautifulSoup is None:
            self.skipTest("BeautifulSoup is provided by the Cardinal runtime, but is not installed locally")
        html = """
          <form class="form-offer-editor">
            <input name="required_code" required value="">
            <input name="filled" required value="ok">
            <textarea name="known" required></textarea>
          </form>
        """
        self.assertEqual(
            self.helpers["unsupported_required_lot_fields"](html, {"known"}),
            ["required_code"],
        )

    def test_parse_lot_select_fields_supports_ru_and_en_forms(self):
        if BeautifulSoup is None:
            self.skipTest("BeautifulSoup is provided by the Cardinal runtime, but is not installed locally")
        parse = self.helpers["parse_lot_select_fields"]
        fixtures = [
            ("Способ получения", "Вход в аккаунт"),
            ("Method of obtaining", "By logging in to the account"),
        ]
        for field_label, option_label in fixtures:
            with self.subTest(field_label=field_label):
                schema = parse(f"""
                    <form class="form-offer-editor"><div class="form-group">
                    <label for="method">{field_label}</label>
                    <select id="method" name="fields[method]">
                    <option value="login" selected>{option_label}</option></select>
                    </div></form>
                """)
                self.assertEqual(schema[0]["name"], "fields[method]")
                self.assertEqual(schema[0]["label"], field_label)
                self.assertEqual(schema[0]["value"], "login")
                self.assertEqual(schema[0]["value_label"], option_label)

    def test_form_defaults_preserve_both_language_fields(self):
        if BeautifulSoup is None:
            self.skipTest("BeautifulSoup is provided by the Cardinal runtime, but is not installed locally")
        fields = self.helpers["parse_lot_form_defaults"]("""
          <form class="form-offer-editor">
            <input name="fields[summary][ru]" value="Русское название">
            <input name="fields[summary][en]" value="English title">
            <textarea name="fields[desc][ru]">Русское описание</textarea>
            <textarea name="fields[desc][en]">English description</textarea>
          </form>
        """)
        self.assertEqual(fields["fields[summary][ru]"], "Русское название")
        self.assertEqual(fields["fields[summary][en]"], "English title")
        self.assertEqual(fields["fields[desc][ru]"], "Русское описание")
        self.assertEqual(fields["fields[desc][en]"], "English description")

    def test_subcategory_id_parser_validates_ids(self):
        parse = self.helpers["parse_subcategory_ids_input"]
        self.assertEqual(parse("123, 456"), ([123, 456], []))
        self.assertEqual(parse("0, -1, 1234567890123, Steam"), ([], ["0", "-1", "1234567890123", "Steam"]))

    def test_update_source_validation_checks_identity_and_syntax(self):
        validate = self.helpers["validate_update_source"]
        source = '''
NAME = "LotsManager"
VERSION = "1.2.3"
UUID = "5693f220-bcc6-4f6e-9745-9dee8664cbb2"
BIND_TO_PRE_INIT = []
BIND_TO_DELETE = None
'''
        self.assertEqual(validate(source)["VERSION"], "1.2.3")
        with self.assertRaises(ValueError):
            validate(source.replace('NAME = "LotsManager"', 'NAME = "Other"'))
        with self.assertRaises(SyntaxError):
            validate(source + "if")
        current_source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertEqual(validate(current_source)["VERSION"], "1.3.0")

    def test_root_version_matches_plugin_metadata(self):
        root_version = (PLUGIN_PATH.parent / "VERSION").read_text(encoding="utf-8")
        self.assertEqual(self.helpers["parse_version_document"](root_version), "1.3.0")
        self.assertEqual(self.helpers["validate_update_source"](PLUGIN_PATH.read_text(encoding="utf-8"))["VERSION"], "1.3.0")

    def test_version_document_and_update_direction(self):
        parse = self.helpers["parse_version_document"]
        self.assertEqual(parse("1.2.0\n"), "1.2.0")
        for invalid in ("v1.2.0", "1.2.0\nextra", " 1.2.0\n"):
            with self.assertRaises(ValueError):
                parse(invalid)
        action = self.helpers["update_action"]
        self.assertEqual(action("1.2.0", "1.2.0"), "current")
        self.assertEqual(action("1.2.0", "1.3.0"), "install")
        self.assertEqual(action("1.2.0", "1.1.9"), "newer-local")

    def test_update_urls_require_immutable_commit(self):
        build = self.helpers["build_update_urls"]
        sha = "a" * 40
        api_url, version_url, source_url = build(sha)
        self.assertEqual(api_url, "https://api.github.com/repos/voterol/FpcLotsManager/commits?path=VERSION&sha=main&per_page=1")
        self.assertEqual(version_url, f"https://raw.githubusercontent.com/voterol/FpcLotsManager/{sha}/VERSION")
        self.assertEqual(source_url, f"https://raw.githubusercontent.com/voterol/FpcLotsManager/{sha}/LotsManager.py")
        with self.assertRaises(ValueError):
            build("main")

    def test_updater_settings_migration_defaults_on_and_preserves_explicit_opt_out(self):
        normalize = self.helpers["normalize_updater_settings"]
        self.assertTrue(normalize({}, 100)["enabled"])
        self.assertTrue(normalize({"schema": 1, "consent": "unknown", "enabled": False}, 100)["enabled"])
        self.assertFalse(normalize({"schema": 1, "consent": "declined", "enabled": False}, 100)["enabled"])
        self.assertTrue(normalize({"schema": 1, "consent": "accepted", "enabled": True}, 100)["enabled"])
        current = normalize({"schema": 2, "enabled": False}, 100)
        self.assertFalse(current["enabled"])

    def test_pending_restart_validation_is_strict_and_forward_only(self):
        normalize = self.helpers["normalize_pending_restart"]
        valid = {"deadline": 123, "from_version": "1.2.0", "to_version": "1.3.0"}
        self.assertEqual(normalize(valid), valid)
        invalid = [
            None,
            {**valid, "deadline": 0},
            {**valid, "deadline": 1.5},
            {**valid, "deadline": True},
            {**valid, "to_version": "1.2.0"},
            {**valid, "to_version": "1.1.0"},
            {**valid, "extra": "x"},
            {**valid, "from_version": "v1.2.0"},
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(normalize(value))

    def test_pending_token_and_auto_generation_are_deterministic(self):
        pending = {"deadline": 123, "from_version": "1.2.0", "to_version": "1.3.0"}
        self.assertEqual(self.helpers["pending_restart_token"](pending), ("1.3.0", 123))
        actionable = self.helpers["pending_restart_is_actionable"]
        self.assertTrue(actionable(pending, "1.2.0"))
        self.assertFalse(actionable(pending, "1.3.0"))
        self.assertFalse(actionable(pending, "1.4.0"))
        allowed = self.helpers["auto_update_allowed"]
        self.assertTrue(allowed(True, 4, 4))
        self.assertFalse(allowed(False, 4, 4))
        self.assertFalse(allowed(True, 4, 5))

    def test_startup_recipient_state_is_strict_and_deduplicated(self):
        normalize = self.helpers["normalize_recipient_ids"]
        self.assertEqual(normalize([3, 1, 3, True, 0, -1, "2", 2]), [3, 1, 2])
        self.assertEqual(normalize((1, 2)), [])
        plan = self.helpers["startup_notice_plan"]
        self.assertEqual(plan((3, 1, 3, "bad", 2), [1, 9]), ((3, 1, 2), (3, 2)))
        self.assertEqual(plan([], [1]), ((), ()))

    def test_updater_settings_validates_startup_recipient_progress(self):
        normalize = self.helpers["normalize_updater_settings"]
        result = normalize({
            "schema": 2,
            "enabled": True,
            "startup_notice_version": "1.2.0",
            "startup_notice_recipients_version": "1.2.0",
            "startup_notice_recipients": [10, 10, True, -1, 20],
        }, 100)
        self.assertEqual(result["startup_notice_recipients"], [10, 20])
        self.assertEqual(result["startup_notice_recipients_version"], "1.2.0")
        invalid = normalize({
            "schema": 2, "enabled": True,
            "startup_notice_recipients_version": "current",
            "startup_notice_recipients": [10],
        }, 100)
        self.assertIsNone(invalid["startup_notice_recipients_version"])

    def test_version_tuple_uses_numeric_comparison(self):
        parse = self.helpers["version_tuple"]
        self.assertGreater(parse("1.10.0"), parse("1.9.0"))
        with self.assertRaises(ValueError):
            parse("v1.0")


if __name__ == "__main__":
    unittest.main()
