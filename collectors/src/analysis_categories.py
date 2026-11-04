"""Content-type vocabulary shared by Gemini validation and analysis projections.

Only confirmed aliases are merged. Historical values outside this vocabulary
remain visible in read-only projections instead of being silently discarded.
"""
from typing import Literal, get_args


ContentType = Literal[
    "lookbook", "fashion_recommendation",
    "styling_inspiration", "product_demo", "quote_aesthetic",
    "parenting_item_review", "fashion_review", "mirror_selfie_fashion_review",
    "street_fashion_portrait", "life_hack_tutorial", "fashion_tips",
    "short_form_transition", "try_on_haul", "store_tour",
    "comedy_skit", "sketch_comedy", "reaction", "meme",
    "talking_head", "daily_vlog", "fashion_humor", "couple_vlog",
    "fashion_try_on", "product_review", "fashion_tutorial", "vlog",
    "product_demo_lookbook", "styling_tips", "product_recommendation_lookbook",
    "product_showcase_and_lookbook", "fashion_tips_talking_head", "other",
]
CONTENT_TYPES = get_args(ContentType)


def normalize_content_type(value):
    """Canonicalize confirmed lookbook aliases without guessing other types."""
    if not isinstance(value, str):
        return value
    key = "_".join(value.strip().casefold().replace("-", " ").replace("_", " ").split())
    if key in {"lookbook", "fashion_lookbook", "daily_lookbook", "outfit_lookbook",
               "streetwear_lookbook", "룩북", "패션_룩북", "패션룩북",
               "데일리_룩북", "데일리룩북", "착장_룩북", "착장룩북",
               "스트리트웨어_룩북", "스트리트웨어룩북"}:
        return "lookbook"
    return key if key in CONTENT_TYPES else value.strip()
