"""Credential checks use synthetic values and never contact providers."""
import hashlib
import json
import unittest
from unittest.mock import patch

from dianstar.security.content_guard import ContentScanner, SecurityViolation, MAX_FILE, protected_path
from dianstar.hooks import HookRunner


TOKEN = "ghp_" + "A1b2C3d4" * 5


class SecurityHookTests(unittest.TestCase):
    def setUp(self):
        self.scanner = ContentScanner(known=("synthetic-application-secret",), fingerprints=[])
        self.hooks = HookRunner(self.scanner)

    def test_ordinary_configuration_and_templates_are_allowed(self):
        for path, content in (("config.json", '{"token": "", "port": 8080}'),
                              ("config.json", '{"token": "${SERVICE_TOKEN}"}'),
                              (".env.example", "API_KEY=your-key-here\nPASSWORD=change-me\n"),
                              ("package.json", '{"name": "test", "version": "1.0.0"}')):
            self.assertFalse(self.hooks.run("before_write", [(path, content.encode())])["findings"])
            self.assertFalse(self.hooks.run("before_commit", [(path, content.encode())])["findings"])

    def test_env_variants_and_private_paths_are_protected_but_templates_are_not(self):
        for path in (".env", "nested/.ENV.production", "settings.env", ".local/config.json", "key.pem", "credentials.json"):
            self.assertTrue(protected_path(path))
            with self.assertRaises(SecurityViolation):
                self.hooks.run("before_stage", [(path, b"harmless")])
        self.assertFalse(protected_path("nested/.env.example"))

    def test_provider_and_known_credentials_are_detected_without_reporting_values(self):
        for content in (TOKEN, "prefix synthetic-application-secret suffix"):
            with self.assertRaises(SecurityViolation) as error:
                self.hooks.run("before_stage", [("ordinary.txt", content.encode())])
            self.assertNotIn(content, json.dumps(error.exception.result()))
            self.assertTrue(error.exception.result()["findings"])
            self.assertNotIn(content, self.scanner.redact(content))

    def test_credential_source_modules_are_allowed_but_still_content_scanned(self):
        unknown = "unregistered-credential-value"
        for path in ("dianstar/config/credentials.py", "src/secrets.ts", "src/credentials.rs"):
            self.assertFalse(protected_path(path))
            for event in ("before_write", "before_stage", "before_commit", "before_push"):
                with self.subTest(path=path, event=event):
                    self.hooks.run(event, [(path, b"# credential management source\n")])
                    for content in (TOKEN, "synthetic-application-secret", json.dumps({"access_token": unknown})):
                        with self.assertRaises(SecurityViolation):
                            self.hooks.run(event, [(path, content.encode())])

    def test_source_suffix_cannot_exempt_private_directories_or_credential_documents(self):
        for path in (".aws/credentials.py", ".ssh/secrets.ts", ".local/credentials.py",
                     "credentials/secrets.py", "secrets/manager.py", "credentials.py/normal.txt",
                     "src/credentials.json", "src/secrets.yaml", "credentials", "credentials.py.json",
                     "credentials.py.pem", ".env.py", "nested\\.aws\\credentials.py"):
            with self.subTest(path=path):
                self.assertTrue(protected_path(path))
                with self.assertRaises(SecurityViolation):
                    self.hooks.run("before_stage", [(path, b"harmless")])

    def test_explicit_fixture_values_do_not_exempt_real_tokens_in_tests(self):
        for name in ("UNRELATED_SECRET", "UNRELATED_MARKER"):
            self.hooks.run("before_commit", [("tests/test_mcp.py",
                           ('patch.dict(os.environ, {"' + name + '": "fixture-must-not-inherit"})').encode())])
            with self.assertRaises(SecurityViolation):
                self.hooks.run("before_commit", [("tests/test_mcp.py",
                               ('patch.dict(os.environ, {"' + name + '": "fixture-' + TOKEN + '"})').encode())])

    def test_stateless_github_token_is_masked_through_all_jwt_segments(self):
        value = "ghs_12345_" + ".".join(["AbC_def-1234567890"] * 3)
        self.assertTrue(self.scanner.scan("config.json", json.dumps({"token": value}).encode()))
        masked = self.scanner.redact(value)
        self.assertNotIn(value, masked)
        self.assertNotIn("AbC_def-1234567890", masked)

    def test_unknown_credential_can_be_saved_only_as_explicit_local_configuration(self):
        value = "unregistered-credential-value"
        content = json.dumps({"access_token": value}).encode()
        with self.assertRaises(SecurityViolation):
            self.hooks.run("before_write", [("config.json", content)])
        self.hooks.run("before_write", [("config.json", content)], local_only=True)
        self.hooks.run("before_write", [(".env", content)], local_only=True)
        for event in ("before_stage", "before_commit", "before_push"):
            with self.assertRaises(SecurityViolation):
                self.hooks.run(event, [("config.json", content)], local_only=True)
        with self.assertRaises(SecurityViolation):
            self.hooks.run("before_write", [("main.py", content)], local_only=True)

    def test_local_only_cannot_copy_application_keys_or_hide_a_secret_in_template(self):
        for path, value in (("config.json", "synthetic-application-secret"), (".env.example", TOKEN)):
            with self.assertRaises(SecurityViolation):
                self.hooks.run("before_write", [(path, value.encode())], local_only=True)
        for path in (".aws/.env", ".aws\\.env", "secrets\\.env", ".local\\config.json"):
            with self.assertRaises(SecurityViolation):
                self.hooks.run("before_write", [(path, b"harmless")], local_only=True)

    def test_fingerprints_do_not_require_exporting_raw_credentials(self):
        value = "synthetic-hidden-application-secret"
        scanner = ContentScanner(fingerprints=[hashlib.sha256(value.encode()).hexdigest()])
        self.assertTrue(scanner.scan("readme.md", ("credential: " + value).encode()))
        self.assertNotIn(value, scanner.redact('"' + value + '"'))

    def test_private_keys_and_utf16_files_are_checked(self):
        header = "-----BEGIN " + "RSA PRIVATE KEY-----"
        content = header + "\nnot-a-real-key\n-----END RSA PRIVATE KEY-----"
        self.assertTrue(self.scanner.scan("notes.txt", content.encode("utf-16")))
        redacted = self.scanner.redact(content)
        self.assertNotIn("not-a-real-key", redacted)

    def test_field_names_variable_references_and_random_strings_are_not_credentials(self):
        for content in ('token = user_provided_value', '{"token": ""}', 'digest = "a9b72cf384e5102d79860ab34e518cd2"'):
            self.assertEqual(self.scanner.scan("main.py", content.encode()), [])

    def test_malformed_fingerprint_metadata_does_not_disable_the_scanner(self):
        from dianstar.security.content_guard import FINGERPRINT_ENV
        with patch.dict("os.environ", {FINGERPRINT_ENV: "not valid json"}):
            with self.assertRaises(SecurityViolation):
                ContentScanner()

    def test_large_identifiers_do_not_trigger_unbounded_assignment_backtracking(self):
        self.assertEqual(self.scanner.scan("main.py", b"a" * 200000), [])

    def test_repeated_findings_have_correct_lines_and_are_all_redacted(self):
        text = ("synthetic-application-secret\n" * 2000)
        findings = self.scanner.scan("notes.txt", text.encode())
        self.assertEqual(len(findings), 2000)
        self.assertEqual(findings[-1].line, 2000)
        masked = self.scanner.redact(text)
        self.assertNotIn("synthetic-application-secret", masked)
        self.assertEqual(masked.count("[已遮挡凭据]"), 2000)

    def test_unclosed_private_key_blocks_do_not_leak_or_cause_repeated_tail_searches(self):
        header = "-----BEGIN " + "RSA PRIVATE KEY-----\n"
        text = (header + "unclosed-key-body\n") * 2000
        masked = self.scanner.redact(text)
        self.assertNotIn("unclosed-key-body", masked)
        self.assertNotIn(header, masked)

    def test_scan_limits_and_scanner_errors_fail_closed(self):
        with self.assertRaises(SecurityViolation) as error:
            self.hooks.run("before_commit", [("large.txt", b"x" * (MAX_FILE + 1))])
        self.assertEqual(error.exception.code, "security_scan_incomplete")
        with patch.object(self.scanner, "scan", side_effect=RuntimeError("must not be exposed")):
            with self.assertRaises(SecurityViolation) as error:
                self.hooks.run("before_push", [("a.txt", b"a")])
        self.assertNotIn("must not be exposed", str(error.exception))

    def test_post_operation_reports_findings_without_claiming_to_prevent_side_effects(self):
        report = self.hooks.run("after_command", [(".env", TOKEN.encode())])
        self.assertTrue(report["findings"])
        self.assertEqual(report["event"], "after_command")
        self.assertNotIn(TOKEN, json.dumps(report))
