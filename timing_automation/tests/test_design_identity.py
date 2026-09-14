#!/usr/bin/env python3
"""
Regression tests for nEBULA design identity resolution.

These tests intentionally capture current behavior before introducing
DesignManifest.

Important distinction:

    mapper_reconvergent_test
        design_id / ORFS design = mapper_reconvergent_test
        RTL top               = mapper_reconvergent_test

    DR.RTL ticket_machine
        upstream design       = ticket_machine
        ORFS design           = dr_ticket_machine
        RTL top               = ticket_machine

The future DesignManifest implementation must preserve this distinction.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


# ============================================================
# Repository paths
# ============================================================

TEST_DIR = Path(__file__).resolve().parent
TIMING_AUTOMATION_DIR = TEST_DIR.parent
FLOW_DIR = TIMING_AUTOMATION_DIR.parent


# Make timing_automation importable when this file is run directly:
#
#     python3 timing_automation/tests/test_design_identity.py
#
if str(TIMING_AUTOMATION_DIR) not in sys.path:
    sys.path.insert(
        0,
        str(TIMING_AUTOMATION_DIR),
    )


from run_analysis import read_design_top
from run_dr_rtl_one import parse_design_metadata, slugify


def _orfs_tree_available() -> bool:
    return all(
        path.exists()
        for path in (
            FLOW_DIR / "designs",
            FLOW_DIR / "platforms",
            FLOW_DIR / "Makefile",
        )
    )


ORFS_TREE_AVAILABLE = _orfs_tree_available()

MAPPER_CONFIG = (
    FLOW_DIR
    / "designs"
    / "nangate45"
    / "mapper_reconvergent_test"
    / "config.mk"
)

DR_TICKET_CONFIG = (
    FLOW_DIR
    / "designs"
    / "nangate45"
    / "dr_ticket_machine"
    / "config.mk"
)

MAPPER_DESIGN_AVAILABLE = (
    ORFS_TREE_AVAILABLE
    and MAPPER_CONFIG.is_file()
)

DR_TICKET_DESIGN_AVAILABLE = (
    ORFS_TREE_AVAILABLE
    and DR_TICKET_CONFIG.is_file()
)


# ============================================================
# Tests
# ============================================================

@unittest.skipUnless(
    MAPPER_DESIGN_AVAILABLE,
    "optional mapper_reconvergent_test benchmark is unavailable",
)
class TestMapperReconvergentIdentity(unittest.TestCase):
    """
    Regression behavior for the native nEBULA test design.
    """

    def test_mapper_config_exists(self) -> None:
        config = MAPPER_CONFIG

        self.assertTrue(
            config.is_file(),
            f"Mapper config.mk not found: {config}",
        )

    def test_mapper_top_is_design_name(self) -> None:
        config = MAPPER_CONFIG

        top = read_design_top(config)

        self.assertEqual(
            top,
            "mapper_reconvergent_test",
            (
                "mapper_reconvergent_test regression failed: "
                "DESIGN_NAME/top changed unexpectedly"
            ),
        )


class TestDesignTopParserFixture(unittest.TestCase):
    """Always-run parser coverage independent of a surrounding ORFS checkout."""

    def test_read_design_top_from_fixture_config(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula_design_identity_") as td:
            config = Path(td) / "config.mk"
            config.write_text("export DESIGN_NAME = fixture_top\n", encoding="utf-8")
            self.assertEqual(read_design_top(config), "fixture_top")


class TestDrRtlTicketMachineIdentity(unittest.TestCase):
    """
    Regression behavior for the staged DR.RTL ticket_machine design.
    """

    def test_ticket_machine_slug(self) -> None:
        self.assertEqual(
            slugify("ticket_machine"),
            "dr_ticket_machine",
        )

    def test_ticket_machine_metadata_conversion(self) -> None:
        """
        Capture the old DR.RTL native metadata interpretation:

            [top, clock, reset, extension]

        This behavior will later move behind the DR.RTL DesignManifest
        adapter.
        """

        raw_metadata = [
            "ticket_machine",
            "clk",
            "rst",
            "v",
        ]

        parsed = parse_design_metadata(
            "ticket_machine",
            raw_metadata,
        )

        self.assertEqual(
            parsed["name"],
            "ticket_machine",
        )

        self.assertEqual(
            parsed["top"],
            "ticket_machine",
        )

        self.assertEqual(
            parsed["clock"],
            "clk",
        )

        self.assertEqual(
            parsed["reset"],
            "rst",
        )

        self.assertEqual(
            parsed["extension"],
            "v",
        )

    @unittest.skipUnless(
        DR_TICKET_DESIGN_AVAILABLE,
        "optional dr_ticket_machine benchmark is unavailable",
    )
    def test_staged_ticket_machine_config_exists(self) -> None:
        config = DR_TICKET_CONFIG

        self.assertTrue(
            config.is_file(),
            (
                "Expected staged DR.RTL ticket_machine config "
                f"not found: {config}"
            ),
        )

    @unittest.skipUnless(
        DR_TICKET_DESIGN_AVAILABLE,
        "optional dr_ticket_machine benchmark is unavailable",
    )
    def test_ticket_machine_top_differs_from_orfs_design_id(self) -> None:
        """
        This is the critical regression case.

        ORFS design directory:
            dr_ticket_machine

        Actual Verilog top:
            ticket_machine
        """

        orfs_design_id = "dr_ticket_machine"

        config = DR_TICKET_CONFIG

        top = read_design_top(config)

        self.assertEqual(
            top,
            "ticket_machine",
        )

        self.assertNotEqual(
            orfs_design_id,
            top,
            (
                "Regression test expects the ORFS design identifier "
                "and Verilog top to remain distinct for DR.RTL designs"
            ),
        )


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    unittest.main(
        verbosity=2,
    )
