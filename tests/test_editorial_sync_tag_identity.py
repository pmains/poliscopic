from scripts.editorial_sync import remap_article_tags


def test_article_tags_use_production_tag_ids_and_collapse_duplicates():
    rows = [
        {"article_id": 194, "tag_id": 24},
        {"article_id": 194, "tag_id": 45},
        {"article_id": 193, "tag_id": 7},
    ]

    assert remap_article_tags(rows, {7: 7, 24: 24, 45: 24}) == [
        {"article_id": 193, "tag_id": 7},
        {"article_id": 194, "tag_id": 24},
    ]
