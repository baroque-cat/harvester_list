#!/usr/bin/env python3

"""
Q2b pins: DEF-3 — transient errors (NETWORK_ERROR, TIMEOUT, SERVER_ERROR)
go to WAIT_CHECK instead of INVALID.

Tests cover:
- _result_status: NETWORK_ERROR/TIMEOUT/SERVER_ERROR → wait_check
- _result_status: regression — NO_QUOTA/RATE_LIMITED/NO_MODEL/NO_ACCESS unchanged
- _result_status: BAD_REQUEST/UNKNOWN/INVALID_KEY still → invalid
"""

import pytest

from core.enums import ErrorReason
from core.models import CheckResult
from stage.definition import CheckStage


class TestDef3TransientToWaitCheck:
    """DEF-3: transient errors → WAIT_CHECK (not INVALID)."""

    def test_network_error_is_wait_check(self):
        result = CheckResult.fail(ErrorReason.NETWORK_ERROR)
        assert CheckStage._result_status(result) == "wait_check"

    def test_timeout_is_wait_check(self):
        result = CheckResult.fail(ErrorReason.TIMEOUT)
        assert CheckStage._result_status(result) == "wait_check"

    def test_server_error_is_wait_check(self):
        result = CheckResult.fail(ErrorReason.SERVER_ERROR)
        assert CheckStage._result_status(result) == "wait_check"


class TestDef3Regression:
    """DEF-3: existing classifications unchanged."""

    def test_no_quota_still_no_quota(self):
        result = CheckResult.fail(ErrorReason.NO_QUOTA)
        assert CheckStage._result_status(result) == "no_quota"

    def test_rate_limited_still_wait_check(self):
        result = CheckResult.fail(ErrorReason.RATE_LIMITED)
        assert CheckStage._result_status(result) == "wait_check"

    def test_no_model_still_wait_check(self):
        result = CheckResult.fail(ErrorReason.NO_MODEL)
        assert CheckStage._result_status(result) == "wait_check"

    def test_no_access_still_wait_check(self):
        result = CheckResult.fail(ErrorReason.NO_ACCESS)
        assert CheckStage._result_status(result) == "wait_check"

    def test_bad_request_still_invalid(self):
        """BAD_REQUEST stays INVALID (generic-400 fallback preserves semantics)."""
        result = CheckResult.fail(ErrorReason.BAD_REQUEST)
        assert CheckStage._result_status(result) == "invalid"

    def test_unknown_still_invalid(self):
        result = CheckResult.fail(ErrorReason.UNKNOWN)
        assert CheckStage._result_status(result) == "invalid"

    def test_invalid_key_still_invalid(self):
        result = CheckResult.fail(ErrorReason.INVALID_KEY)
        assert CheckStage._result_status(result) == "invalid"

    def test_available_is_valid(self):
        result = CheckResult.success()
        assert CheckStage._result_status(result) == "valid"
