from body_code_merge_runtime import MERGES, code_hashes, digest, semantic_text


def test_registered_slug_split_merges_into_established_short_codes():
    assert MERGES == {
        "chandler-planning-zoning-commission": "chandler-pz",
        "mesa-planning-zoning": "mesa-pz",
    }


def test_known_title_variants_are_semantically_equal():
    pairs = [
        ("Kitchen &amp; Cocktails", "Kitchen & Cocktails"),
        ("located 1 / 2  mile south", "located 1/2 mile south"),
        ("Ray Road and 56 th  Street", "Ray Road and 56th Street"),
    ]
    for left, right in pairs:
        assert semantic_text(left) == semantic_text(right)


def test_semantic_normalization_does_not_hide_real_text_changes():
    assert semantic_text("Approved") != semantic_text("Denied")
    assert semantic_text("Item 4") != semantic_text("Item 5")


def test_plan_digest_is_order_stable_but_content_sensitive():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert digest({"a": 1}) != digest({"a": 2})


def test_plan_binds_every_mutation_module():
    """A reviewed plan is bound to the bytes of all mutating modules."""
    hashes = code_hashes()
    assert set(hashes) == {
        "body_code_merge.py",
        "body_code_merge_runtime.py",
        "body_code_merge_prod.py",
    }
    assert all(len(value) == 64 for value in hashes.values())
