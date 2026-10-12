"""Read-only evidence shared by exclusion suggestions and their statistics."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .catalog import ImageCatalog
from .config import WayperConfig
from .model_review import load_model_review_state
from .pool import (
    ORIENTATIONS,
    ImageMetadata,
    favorite_filenames,
    list_blacklist,
    list_images,
    load_metadata,
    pool_dir,
)
from .preference_model import (
    _latest_feedback_by_filename,
    load_preference_feedback,
    load_preference_historical_bans,
)
from .state import ALL_PURITIES


def load_suggestion_catalog(
    config: WayperConfig,
    *,
    purities: Iterable[str] | None = None,
    metadata_snapshot: Mapping[str, ImageMetadata] | None = None,
) -> ImageCatalog:
    """Resolve durable bans and corrections without treating missing files as kept.

    Exclusion-rule evidence includes deliberate exact-image bans as before; it
    is separate from the preference model's curated training labels. Automatic
    quarantine/queue clearing alone is neither positive nor negative feedback.
    """
    metadata = metadata_snapshot if metadata_snapshot is not None else load_metadata(config)
    favorites = favorite_filenames(config)
    retained = {
        image.name
        for purity in ALL_PURITIES
        for orientation in ORIENTATIONS
        for image in list_images(pool_dir(config, purity, orientation))
    }
    banned = set(load_preference_historical_bans(config))
    banned.update(filename for _, filename in list_blacklist(config))

    review_items = load_model_review_state(config)["items"]
    neutral = {
        str(record["name"]): max(
            (
                value
                for key in ("created_at", "resolved_at")
                if isinstance(value := record.get(key), int | float)
            ),
            default=0,
        )
        for record in review_items.values()
        if record.get("status") in {"pending", "filter"}
    }
    banned.difference_update(neutral)
    retained.difference_update(neutral)

    # The ledger outlives blacklist expiration. Later human decisions override
    # historical evidence; unblocking alone does not label a missing file Keep.
    latest = _latest_feedback_by_filename(load_preference_feedback(config)["events"])
    for filename, event in latest.items():
        if filename in neutral and int(event["timestamp"]) <= neutral[filename]:
            continue
        action = event["action"]
        if action in {"ban", "dislike"}:
            banned.add(filename)
        else:
            banned.discard(filename)
            if action in {"keep", "favorite"}:
                retained.add(filename)
            if action == "favorite":
                favorites.add(filename)

    # A current favorite always vetoes a broader exclusion, even if an old
    # blacklist entry survived a restore or a remote favorites sync.
    banned.difference_update(favorites)
    retained.update(favorites)
    known = banned | retained
    return ImageCatalog(
        {filename: meta for filename, meta in metadata.items() if filename in known},
        banned,
        favorites,
        purities=purities,
    )
