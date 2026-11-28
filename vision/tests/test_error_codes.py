"""Error-taxonomy invariants across the Node contract and the Python daemon.

Two regressions are guarded here:

1. ``ProtocolError`` used to downgrade any code missing from ``ERROR_CODES`` to
   ``E_INTERNAL``. Several codes the daemon genuinely raised (clipboard, audio
   device, focus lost, macro failed, memory watermark) were silently rewritten,
   so the model saw ``E_INTERNAL`` instead of an actionable code. Those codes
   are now whitelisted and this file proves the round-trip is lossless.

2. The whitelist and ``contract/lib/contract.js`` are maintained by hand in two
   languages. A literal scan catches a new ``E_*`` string that was added to the
   daemon without being registered, and the cross-language check catches drift
   between the Python and Node enums.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from vhcu_daemon.protocol import ERROR_CODES, LOCAL_ONLY_CODES, ProtocolError

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "vhcu_daemon"
CONTRACT_JS = Path(__file__).resolve().parents[2] / "contract" / "lib" / "contract.js"

# Codes the daemon raised before the 2026-11-28 fix but never registered.
RECOVERED_CODES = (
    "E_CLIPBOARD",
    "E_AUDIO_DEVICE",
    "E_FOCUS_LOST",
    "E_MACRO_FAILED",
    "E_MEMORY",
)

# Node-only codes: they belong to the host half and never cross the bridge.
NODE_ONLY_CODES = frozenset(
    {"E_MACRO_GENERATION_DISABLED", "E_MACRO_NOT_REUSABLE", "E_SCRIPT_WRITE_DENIED"}
)

_CODE_LITERAL = re.compile(r'"(E_[A-Z_]+)"')


class ProtocolErrorPreservationTest(unittest.TestCase):
    def test_previously_degraded_codes_survive(self):
        for code in RECOVERED_CODES:
            with self.subTest(code=code):
                error = ProtocolError(code, "boom")
                self.assertEqual(
                    error.code,
                    code,
                    f"{code} was downgraded to {error.code} by the whitelist",
                )

    def test_unknown_code_still_falls_back_to_internal(self):
        self.assertEqual(ProtocolError("E_NOT_A_REAL_CODE", "boom").code, "E_INTERNAL")

    def test_local_only_codes_are_not_wire_codes(self):
        self.assertEqual(
            ERROR_CODES & LOCAL_ONLY_CODES,
            set(),
            "a code cannot be both wire-visible and local-only",
        )


class DaemonErrorCodeInventoryTest(unittest.TestCase):
    @staticmethod
    def _literals():
        found = {}
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for match in _CODE_LITERAL.finditer(text):
                found.setdefault(match.group(1), set()).add(path.name)
        return found

    def test_every_error_literal_is_declared(self):
        """A new E_* literal must be registered or explicitly local-only."""
        unknown = {
            code: sorted(files)
            for code, files in self._literals().items()
            if code not in ERROR_CODES and code not in LOCAL_ONLY_CODES
        }
        self.assertEqual(
            unknown,
            {},
            "daemon raises codes that are neither in ERROR_CODES nor LOCAL_ONLY_CODES: "
            f"{unknown}",
        )

    def test_recovered_codes_are_still_raised_somewhere(self):
        """Guard the list from rotting into dead entries."""
        literals = self._literals()
        for code in RECOVERED_CODES:
            with self.subTest(code=code):
                self.assertIn(code, literals, f"{code} is whitelisted but no longer raised")


class ContractParityTest(unittest.TestCase):
    @staticmethod
    def _contract_codes():
        text = CONTRACT_JS.read_text(encoding="utf-8")
        return set(re.findall(r"^\s{2}(E_[A-Z_]+):\s*\{", text, re.M))

    def test_contract_file_is_discoverable(self):
        self.assertTrue(CONTRACT_JS.is_file(), f"contract file not found: {CONTRACT_JS}")
        self.assertGreater(len(self._contract_codes()), 30)

    def test_python_codes_are_a_subset_of_the_contract(self):
        contract = self._contract_codes()
        missing = sorted(ERROR_CODES - contract)
        self.assertEqual(
            missing,
            [],
            f"Python raises codes the Node contract does not declare: {missing}",
        )

    def test_contract_minus_python_is_exactly_the_node_only_set(self):
        contract = self._contract_codes()
        self.assertEqual(
            contract - ERROR_CODES,
            set(NODE_ONLY_CODES),
            "contract/Python enums drifted beyond the known host-only codes",
        )

    def test_local_only_code_is_not_in_the_contract(self):
        self.assertEqual(LOCAL_ONLY_CODES & self._contract_codes(), set())


if __name__ == "__main__":
    unittest.main()
