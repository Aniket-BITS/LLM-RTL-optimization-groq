import pytest
from build_request import canonicalize_primary_focus, BuildRequestError


def test_absolute_diagnostic_path_uses_verified_bundle_owner():
    focus = {"file": "/checkout/rtl/top.v", "module": "top", "original_sha256": "a" * 64}
    bundle = {"module_to_file": {"top": "rtl/top.v"},
              "sources": [{"path": "rtl/top.v", "sha256": "a" * 64}]}
    canonicalize_primary_focus(focus, bundle)
    assert focus["file"] == "rtl/top.v"
    focus["original_sha256"] = "b" * 64
    with pytest.raises(BuildRequestError, match="source hash differs"):
        canonicalize_primary_focus(focus, bundle)
