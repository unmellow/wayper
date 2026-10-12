from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from wayper.config import WayperConfig
from wayper.preference.training import DECISION_CALIBRATION_VERSION
from wayper.preference_model import (
    PreferenceExample,
    PreferenceModel,
    PreferencePrediction,
    PreferenceTrainingSnapshot,
    _auto_retrain_lease_path,
    _bootstrap_historical_preference_bans,
    _claim_or_touch_auto_retrain_worker,
    _release_auto_retrain_worker,
    _save_automatic_preference_model,
    _save_manual_preference_model,
    _training_data_signature,
    auto_filter_prediction,
    auto_filter_status,
    build_neighbor_prototypes,
    build_training_examples,
    collect_preference_training_snapshot,
    load_preference_feedback,
    load_preference_historical_bans,
    load_preference_model,
    preference_decision_threshold,
    preference_deletion_suggestions,
    preference_learning_status,
    record_preference_feedback,
    run_scheduled_preference_model_retrain,
    save_preference_model,
    schedule_preference_model_retrain,
    select_preference_examples,
    train_local_preference_model,
    train_preference_model,
)


def _examples(
    prefix: str,
    count: int,
    tags: tuple[str, ...],
    label: int,
    *,
    start: int = 1_700_000_000,
) -> list[PreferenceExample]:
    return [
        PreferenceExample(
            filename=f"{prefix}{index}.jpg",
            tags=tags,
            label=label,
            base_weight=1.0,
            timestamp=start + index,
            is_explicit_ban=label == 1,
            is_explicit_keep=label == 0,
        )
        for index in range(count)
    ]


def _write_cold_start_library(config: WayperConfig) -> dict[str, dict[str, object]]:
    pool = config.download_dir / "sfw" / "landscape"
    pool.mkdir(parents=True)
    metadata: dict[str, dict[str, object]] = {}
    for index in range(10):
        filename = f"dislike{index}.jpg"
        metadata[filename] = {"tags": ["bad", "detail"], "downloaded_at": 1_700_000_000}
        record_preference_feedback(
            config,
            "dislike",
            filename,
            source="core",
            context="manual_dislike",
            timestamp=1_700_000_000 + index,
        )
    for index in range(10):
        filename = f"keep{index}.jpg"
        metadata[filename] = {"tags": ["good", "detail"], "downloaded_at": 1_700_001_000}
        (pool / filename).touch()
    config.metadata_file.write_text(json.dumps(metadata))
    return metadata


def _mark_semantic_model_ready(model: PreferenceModel) -> PreferenceModel:
    """Give status/scheduler tests a dependency-free current semantic head."""
    if not model.neighbor_head_ready:
        examples = [
            *_examples("status-ban", 10, ("bad", "detail"), 1),
            *_examples("status-keep", 10, ("good", "detail"), 0, start=1_700_001_000),
        ]
        model.neighbor_prototypes = build_neighbor_prototypes(examples)
    model.semantic_model = "fake-model"
    model.semantic_weights = (0.0,)
    model.training_summary["decision_threshold"] = 0.5
    model.training_summary["decision_calibration"] = {
        "available": True,
        "version": DECISION_CALIBRATION_VERSION,
        "threshold": 0.5,
    }
    return model


class PreferenceModelTest(unittest.TestCase):
    def test_filter_fails_open_without_semantic_neighbor_runtime(self) -> None:
        examples = [
            *_examples("ban", 20, ("bad subject", "shared context"), 1),
            *_examples("keep", 20, ("good subject", "shared context"), 0, start=1_700_001_000),
        ]
        model = train_preference_model(examples, semantic_model=None)
        model.tag_weights["unseen risk"] = 2.0

        candidate, prediction = auto_filter_prediction(
            model,
            {"tags": ["unseen risk"]},
        )

        self.assertTrue(model.neighbor_head_ready)
        self.assertFalse(model.semantic_enabled)
        self.assertFalse(prediction.neighbor_available)
        self.assertFalse(candidate)

    def test_stale_neighbor_calibration_cannot_enable_auto_filtering(self) -> None:
        examples = [
            *_examples("ban", 20, ("bad", "shared"), 1),
            *_examples("keep", 20, ("good", "shared"), 0, start=1_700_001_000),
        ]
        model = train_preference_model(examples)
        model.semantic_model = "fake-model"
        model.semantic_weights = (0.0,)
        model.training_summary["decision_calibration"] = {
            "available": True,
            "version": DECISION_CALIBRATION_VERSION - 1,
            "threshold": 0.5,
        }

        with tempfile.TemporaryDirectory() as td:
            config = WayperConfig(download_dir=Path(td))
            status = auto_filter_status(config, model)
            learning = preference_learning_status(
                config,
                model,
                PreferenceTrainingSnapshot(
                    examples=tuple(examples),
                    feedback_revision=0,
                    data_signature=_training_data_signature(examples),
                    favorite_files=0,
                ),
            )

        self.assertFalse(status["ready"])
        self.assertEqual(status["status"], "calibration_pending")
        self.assertTrue(learning["decision_boundary_upgrade_due"])
        self.assertTrue(learning["due"])

    def test_fitting_preserves_older_purities_and_recency_within_each_class(self) -> None:
        examples = [
            replace(example, context_features=(f"purity:{purity}",))
            for purity, start in (("nsfw", 1_000), ("sketchy", 2_000), ("sfw", 10_000))
            for label in (0, 1)
            for example in _examples(f"{purity}-{label}-", 100, ("detail",), label, start=start)
        ]

        working = select_preference_examples(examples, limit=60)

        self.assertEqual(len(working), 60)
        self.assertEqual(
            Counter((example.context_features[0], example.label) for example in working),
            {
                (f"purity:{purity}", label): 10
                for purity in ("sfw", "sketchy", "nsfw")
                for label in (0, 1)
            },
        )
        self.assertTrue(
            all(int(example.filename.rsplit("-", 1)[1][:-4]) >= 90 for example in working)
        )
        self.assertEqual(working, select_preference_examples(reversed(examples), limit=60))

    def test_fitting_redistributes_small_purity_and_class_budgets(self) -> None:
        examples = [
            replace(example, context_features=(f"purity:{purity}",))
            for purity, count in (("sfw", 50), ("nsfw", 3))
            for example in _examples(purity, count, ("detail",), 1)
        ] + _examples("keep", 5, ("calm",), 0)

        working = select_preference_examples(examples, limit=21)

        self.assertEqual(len(working), 21)
        self.assertEqual(len({example.filename for example in working}), 21)
        self.assertEqual(sum(example.label == 0 for example in working), 5)
        self.assertEqual(
            sum(example.context_features == ("purity:nsfw",) for example in working), 3
        )
        self.assertEqual(select_preference_examples(examples, limit=1), ())
        self.assertEqual(select_preference_examples(examples[:-5]), ())

    def test_model_loader_reuses_unchanged_file_and_invalidates_after_save(self) -> None:
        model = train_preference_model(
            [
                *_examples("ban", 10, ("bad",), 1),
                *_examples("keep", 10, ("good",), 0, start=1_700_001_000),
            ],
            semantic_model=None,
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "model.json"
            save_preference_model(model, path)
            first = load_preference_model(path)
            second = load_preference_model(path)
            self.assertIs(first, second)
            self.assertIsNotNone(first)
            self.assertAlmostEqual(
                first.predict(["bad"]).probability, model.predict(["bad"]).probability
            )

            model.bias += 1.0
            save_preference_model(model, path)
            changed = load_preference_model(path)

        self.assertIsNotNone(first)
        self.assertIsNotNone(changed)
        self.assertIsNot(first, changed)
        assert changed is not None
        self.assertAlmostEqual(changed.bias, model.bias)

    def test_two_stage_semantic_neighbors_match_related_unseen_tags(self) -> None:
        examples = [
            *_examples("forest-dislike", 12, ("forest", "fog"), 1),
            *_examples("ocean-keep", 12, ("ocean", "sunlight"), 0, start=1_700_001_000),
        ]
        model = train_preference_model(examples, semantic_model=None)
        model.semantic_model = "fake-model"
        model.semantic_weights = (0.0, 0.0, 0.0)

        def fake_embed(texts, *, model_name, batch_size=64):
            del model_name, batch_size

            def vector(text: str) -> tuple[float, float, float]:
                lowered = text.casefold()
                if "forest" in lowered or "woodland" in lowered:
                    return (1.0, 0.0, 0.0)
                if "fog" in lowered or "mist" in lowered:
                    return (0.8, 0.6, 0.0)
                if "ocean" in lowered or "seaside" in lowered:
                    return (-1.0, 0.0, 0.0)
                if "sunlight" in lowered or "sunshine" in lowered:
                    return (-0.8, 0.6, 0.0)
                return (0.0, 0.0, 1.0)

            return [vector(text) for text in texts]

        with patch("wayper.preference.semantic.embed_texts", side_effect=fake_embed):
            disliked = model.predict(
                ["woodland", "mist"],
                _semantic_embedding=(0.0, 0.0, 0.0),
            )
            kept = model.predict(
                ["seaside", "sunshine"],
                _semantic_embedding=(0.0, 0.0, 0.0),
            )
        self.assertEqual(disliked.neighbor_exact_max_similarity, 0.0)
        self.assertGreater(disliked.neighbor_semantic_max_similarity, 0.9)
        self.assertGreater(disliked.neighbor_probability or 0.0, 0.9)
        self.assertIsNotNone(kept.neighbor_probability)
        assert kept.neighbor_probability is not None
        self.assertLess(kept.neighbor_probability, 0.1)
        self.assertTrue(disliked.neighbor_nearest_dislike)
        assert disliked.neighbor_nearest_dislike is not None
        matched = {
            (item["query"], item["prototype"])
            for item in disliked.neighbor_nearest_dislike["tag_matches"]
        }
        self.assertIn(("woodland", "forest"), matched)
        self.assertIn(("mist", "fog"), matched)

    def test_review_only_treats_favorite_as_explicit_keep(self) -> None:
        metadata = {
            "gallery-ban.jpg": {"tags": ["ordinary-ban"]},
            "manual-dislike.jpg": {"tags": ["missed-by-model"]},
            "review-ban.jpg": {"tags": ["review-ban"]},
            "review-keep.jpg": {"tags": ["review-keep"]},
            "favorite.jpg": {"tags": ["favorite"]},
        }
        events = [
            {
                "schema_version": 2,
                "revision": index,
                "timestamp": 100 + index,
                "filename": filename,
                "action": action,
                "source": source,
                "context": context,
            }
            for index, (filename, action, source, context) in enumerate(
                [
                    ("gallery-ban.jpg", "ban", "core", "core"),
                    ("manual-dislike.jpg", "dislike", "core", "manual_dislike"),
                    ("review-ban.jpg", "ban", "core", "model_review"),
                    ("review-keep.jpg", "keep", "model_suggestion", "model_review"),
                ],
                1,
            )
        ]

        examples = build_training_examples(
            metadata,
            [(99, "gallery-ban.jpg")],
            {"favorite.jpg"},
            retained_files={"favorite.jpg"},
            feedback_events=events,
            now=200,
            review_only=True,
        )

        self.assertEqual(
            {example.filename for example in examples},
            {"manual-dislike.jpg", "review-ban.jpg", "review-keep.jpg", "favorite.jpg"},
        )
        by_name = {example.filename: example for example in examples}
        self.assertEqual(by_name["manual-dislike.jpg"].label, 1)
        self.assertTrue(by_name["manual-dislike.jpg"].is_explicit_ban)
        self.assertEqual(by_name["review-ban.jpg"].label, 1)
        self.assertTrue(by_name["review-ban.jpg"].is_explicit_ban)
        self.assertEqual(by_name["review-keep.jpg"].label, 0)
        self.assertTrue(by_name["review-keep.jpg"].is_explicit_keep)
        self.assertEqual(by_name["favorite.jpg"].label, 0)
        self.assertTrue(by_name["favorite.jpg"].is_favorite)
        self.assertTrue(by_name["favorite.jpg"].is_explicit_keep)
        self.assertFalse(any(example.is_control for example in examples))

    def test_manual_dislike_stays_in_legacy_until_curated_ready_and_unban_clears_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = WayperConfig(download_dir=Path(td))
            config.metadata_file.write_text(
                json.dumps({"missed.jpg": {"tags": ["missed-by-model"]}})
            )

            record_preference_feedback(
                config,
                "dislike",
                "missed.jpg",
                context="manual_dislike",
                timestamp=100,
            )
            disliked = collect_preference_training_snapshot(config)
            record_preference_feedback(config, "unban", "missed.jpg", timestamp=101)
            undone = collect_preference_training_snapshot(config)

        self.assertEqual(disliked.label_source, "legacy")
        self.assertEqual([example.filename for example in disliked.examples], ["missed.jpg"])
        self.assertEqual(disliked.examples[0].label, 1)
        self.assertTrue(disliked.examples[0].is_explicit_ban)
        self.assertEqual(undone.label_source, "legacy")
        self.assertEqual(undone.examples, ())

    def test_review_boundary_is_learned_from_recent_explicit_holdout(self) -> None:
        from wayper.preference import training as training_module

        examples = [
            replace(example, context_features=(f"purity:{purity}",))
            for purity, start in (("nsfw", 1_000), ("sketchy", 2_000), ("sfw", 10_000))
            for label in (0, 1)
            for example in _examples(
                f"{purity}-{label}-",
                30,
                ("bad" if label else "good", "detail"),
                label,
                start=start,
            )
        ]

        def fake_embed(texts, *, model_name, batch_size=64):
            del model_name, batch_size
            return [
                (1.0, 0.0)
                if "bad" in text.casefold()
                else (-1.0, 0.0)
                if "good" in text.casefold()
                else (0.0, 1.0)
                for text in texts
            ]

        blend_partitions: list[tuple[set[str], set[str]]] = []
        calibration_partitions: list[tuple[set[str], set[str]]] = []

        def record_partition(original, destination):
            def wrapped(fitted, holdout):
                destination.append(
                    (
                        {prototype.filename for prototype in fitted.neighbor_prototypes},
                        {example.filename for example in holdout},
                    )
                )
                return original(fitted, holdout)

            return wrapped

        with (
            patch("wayper.preference.semantic.embed_texts", side_effect=fake_embed),
            patch(
                "wayper.preference.training._learn_decision_blend",
                side_effect=record_partition(
                    training_module._learn_decision_blend, blend_partitions
                ),
            ),
            patch(
                "wayper.preference.training._calibrate_decision_boundary",
                side_effect=record_partition(
                    training_module._calibrate_decision_boundary, calibration_partitions
                ),
            ),
        ):
            model = train_preference_model(examples, epochs=8, semantic_model="fake-model")
            calibration = model.training_summary["decision_calibration"]
            for purity in ("sfw", "sketchy", "nsfw"):
                with self.subTest(purity=purity):
                    held, _ = auto_filter_prediction(
                        model, {"tags": ["bad", "detail"], "purity": purity}
                    )
                    kept, _ = auto_filter_prediction(
                        model, {"tags": ["good", "detail"], "purity": purity}
                    )
                    self.assertTrue(held)
                    self.assertFalse(kept)

        self.assertTrue(calibration["available"])
        self.assertEqual(calibration["source"], "stratified_recent_holdout")
        self.assertEqual(calibration["method"], "two_stage_tag_semantic_knn")
        self.assertGreaterEqual(calibration["precision"], 0.8)
        self.assertGreaterEqual(calibration["threshold"], 0.5)
        self.assertEqual(
            preference_decision_threshold(model),
            calibration["threshold"],
        )
        self.assertTrue(model.training_summary["decision_blend"]["available"])
        self.assertEqual(len(blend_partitions), 1)
        self.assertEqual(len(calibration_partitions), 1)
        blend_train, blend_holdout = blend_partitions[0]
        calibration_train, calibration_holdout = calibration_partitions[0]
        self.assertFalse(blend_train & blend_holdout)
        self.assertFalse(calibration_train & calibration_holdout)
        self.assertTrue((blend_train | blend_holdout) <= calibration_train)
        self.assertFalse((blend_train | blend_holdout) & calibration_holdout)

    def test_ledger_ban_survives_blacklist_pruning_until_later_positive_feedback(self) -> None:
        metadata = {"expired-ban.jpg": {"tags": ["bad"], "downloaded_at": 1}}
        ledger_ban = {
            "revision": 1,
            "timestamp": 2_000,
            "action": "ban",
            "filename": "expired-ban.jpg",
        }

        after_ttl = build_training_examples(
            metadata,
            [],
            set(),
            set(),
            feedback_events=[ledger_ban],
            now=3_000,
        )
        self.assertEqual(
            [(item.filename, item.label) for item in after_ttl], [("expired-ban.jpg", 1)]
        )

        reversed_label = build_training_examples(
            metadata,
            [],
            set(),
            {"expired-ban.jpg"},
            historical_bans=[(1_000, "expired-ban.jpg")],
            feedback_events=[
                ledger_ban,
                {
                    "revision": 2,
                    "timestamp": 2_100,
                    "action": "unban",
                    "filename": "expired-ban.jpg",
                },
            ],
            now=3_000,
        )
        self.assertEqual(
            [(item.filename, item.label) for item in reversed_label], [("expired-ban.jpg", 0)]
        )
        self.assertTrue(reversed_label[0].temporal_label_known)

    def test_historical_blacklist_bootstrap_survives_ttl_without_feedback_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = WayperConfig(download_dir=Path(td))
            config.metadata_file.write_text(
                json.dumps({"pre-ledger-ban.jpg": {"tags": ["bad"], "downloaded_at": 1}})
            )
            config.blacklist_file.write_text("1000 pre-ledger-ban.jpg\n")

            self.assertEqual(_bootstrap_historical_preference_bans(config), 1)
            self.assertEqual(load_preference_historical_bans(config), {"pre-ledger-ban.jpg": 1_000})
            self.assertFalse(config.preference_feedback_file.exists())

            # Simulate normal TTL pruning after the model feature was enabled.
            config.blacklist_file.write_text("")
            snapshot = collect_preference_training_snapshot(config)

        self.assertEqual(
            [(item.filename, item.label) for item in snapshot.examples], [("pre-ledger-ban.jpg", 1)]
        )

    def test_review_candidates_are_live_nonfavorite_and_need_positive_evidence(self) -> None:
        training = [
            *_examples("ban", 30, ("bad", "detail"), 1),
            *_examples("keep", 30, ("good", "detail"), 0, start=1_700_001_000),
        ]
        model = train_preference_model(training, max_combo_features=100, epochs=12)
        model.semantic_model = "fake-model"
        model.semantic_weights = (0.0,)
        model.training_summary["decision_threshold"] = 0.5
        model.training_summary["decision_calibration"] = {
            "available": True,
            "version": DECISION_CALIBRATION_VERSION,
            "threshold": 0.5,
        }

        def fake_predict_many(_model, records, *, top_n=8):
            del top_n
            predictions = []
            for tags, _, _ in records:
                is_bad = "bad" in tags
                predictions.append(
                    PreferencePrediction(
                        probability=0.9 if is_bad else 0.5,
                        score=1.0 if is_bad else 0.0,
                        feature_score=1.0 if is_bad else 0.0,
                        contributions=(
                            {
                                "type": "tag",
                                "feature": "bad",
                                "weight": 1.0,
                                "direction": "dislike",
                            },
                        )
                        if is_bad
                        else (),
                        neighbor_probability=0.9 if is_bad else None,
                        neighbor_available=is_bad,
                        neighbor_count=1 if is_bad else 0,
                        neighbor_dislike_count=1 if is_bad else 0,
                        neighbor_max_similarity=1.0 if is_bad else 0.0,
                        neighbor_nearest_dislike={"filename": "ban0.jpg"} if is_bad else None,
                    )
                )
            return tuple(predictions)

        with tempfile.TemporaryDirectory() as td:
            config = WayperConfig(download_dir=Path(td))
            pool_dir = config.download_dir / "sfw" / "landscape"
            favorite_dir = config.download_dir / "favorites" / "sfw" / "landscape"
            pool_dir.mkdir(parents=True)
            favorite_dir.mkdir(parents=True)
            for filename in ("bad-candidate.jpg", "unknown.jpg"):
                (pool_dir / filename).touch()
            (favorite_dir / "favorite-bad.jpg").touch()
            config.metadata_file.write_text(
                '{"bad-candidate.jpg":{"tags":["bad","detail"]},'
                '"unknown.jpg":{"tags":["unknown"]},'
                '"favorite-bad.jpg":{"tags":["bad","detail"]}}'
            )
            save_preference_model(model, config.preference_model_file)

            with patch.object(PreferenceModel, "predict_many", new=fake_predict_many):
                suggestions = preference_deletion_suggestions(
                    config, purities=("sfw",), orientation="landscape"
                )
            self.assertEqual([item["name"] for item in suggestions["items"]], ["bad-candidate.jpg"])
            self.assertTrue(suggestions["items"][0]["contributions"])
            self.assertEqual(suggestions["items"][0]["rank"], 1)
            self.assertIn("percentile", suggestions["items"][0])
            self.assertIn("dislike_evidence", suggestions["items"][0])
            self.assertIn("keep_evidence", suggestions["items"][0])

            record_preference_feedback(config, "keep", "bad-candidate.jpg")
            with patch.object(PreferenceModel, "predict_many", new=fake_predict_many):
                kept = preference_deletion_suggestions(
                    config, purities=("sfw",), orientation="landscape"
                )
            self.assertEqual(kept["items"], [])

    def test_snapshot_switches_only_after_model_review_has_both_classes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = WayperConfig(download_dir=Path(td))
            pool = config.download_dir / "sfw" / "landscape"
            pool.mkdir(parents=True)
            (pool / "ordinary-keep.jpg").touch()
            metadata = {
                "ordinary-ban.jpg": {"tags": ["ordinary-ban"]},
                "ordinary-keep.jpg": {"tags": ["ordinary-keep"]},
                "review-ban.jpg": {"tags": ["review-ban"]},
            }
            config.metadata_file.write_text(json.dumps(metadata))
            config.blacklist_file.write_text("100 ordinary-ban.jpg\n")

            legacy = collect_preference_training_snapshot(config)
            self.assertEqual(legacy.label_source, "legacy")
            self.assertEqual(
                {example.filename for example in legacy.examples},
                {"ordinary-ban.jpg", "ordinary-keep.jpg"},
            )

            record_preference_feedback(
                config,
                "ban",
                "review-ban.jpg",
                source="model_suggestion",
                context="model_review",
                timestamp=200,
            )
            reviewed = collect_preference_training_snapshot(config)

            for index in range(9):
                filename = f"review-ban-{index}.jpg"
                metadata[filename] = {"tags": [f"review-ban-{index}"]}
                record_preference_feedback(
                    config,
                    "ban",
                    filename,
                    source="model_suggestion",
                    context="model_review",
                    timestamp=201 + index,
                )
            for index in range(10):
                filename = f"review-keep-{index}.jpg"
                metadata[filename] = {"tags": [f"review-keep-{index}"]}
                record_preference_feedback(
                    config,
                    "keep",
                    filename,
                    source="model_suggestion",
                    context="model_review",
                    timestamp=220 + index,
                )
            config.metadata_file.write_text(json.dumps(metadata))
            curated = collect_preference_training_snapshot(config)

        self.assertEqual(reviewed.label_source, "legacy")
        self.assertEqual(
            {example.filename for example in reviewed.examples},
            {"ordinary-ban.jpg", "ordinary-keep.jpg", "review-ban.jpg"},
        )
        self.assertEqual(curated.label_source, "model_review")
        self.assertEqual(len(curated.examples), 20)
        self.assertEqual(sum(example.label == 1 for example in curated.examples), 10)
        self.assertEqual(sum(example.label == 0 for example in curated.examples), 10)
        self.assertFalse(any(example.is_control for example in curated.examples))

    def test_untrained_feedback_bootstraps_with_legacy_keeps(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = WayperConfig(download_dir=Path(td))
            metadata = _write_cold_start_library(config)

            explicit_snapshot = collect_preference_training_snapshot(config)
            learning = preference_learning_status(config)
            token = _claim_or_touch_auto_retrain_worker(config)
            self.assertIsNotNone(token)
            assert token is not None
            run_scheduled_preference_model_retrain(config, token, delay_seconds=0)
            model = load_preference_model(config.preference_model_file)

            candidate = config.download_dir / "sfw" / "landscape" / "candidate.jpg"
            candidate.touch()
            metadata[candidate.name] = {
                "tags": ["bad", "detail"],
                "downloaded_at": 1_700_002_000,
            }
            config.metadata_file.write_text(json.dumps(metadata))
            suggestions = preference_deletion_suggestions(
                config,
                purities=("sfw",),
                orientation="landscape",
            )

        self.assertEqual(explicit_snapshot.label_source, "legacy")
        self.assertEqual(
            {example.label for example in explicit_snapshot.examples},
            {0, 1},
        )
        self.assertEqual(learning["label_source"], "legacy")
        self.assertTrue(learning["training_ready"])
        self.assertTrue(learning["due"])
        self.assertIsNotNone(model)
        assert model is not None
        self.assertEqual(model.training_summary["label_source"], "legacy")
        self.assertEqual(model.training_summary["banned"], 10)
        self.assertEqual(model.training_summary["retained"], 10)
        self.assertEqual(suggestions["status"], "learning")
        self.assertEqual(suggestions["items"], [])
        self.assertFalse(model.neighbor_head_ready)

    def test_scheduler_starts_frozen_worker_for_first_trainable_model(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = WayperConfig(download_dir=Path(td))
            _write_cold_start_library(config)
            with (
                patch.object(sys, "frozen", True, create=True),
                patch("wayper.preference_model.subprocess.Popen") as popen,
            ):
                popen.return_value.pid = os.getpid()
                schedule_preference_model_retrain(config)

            self.assertEqual(popen.call_count, 1)
            command = popen.call_args.args[0]
            self.assertEqual(command[0], sys.executable)
            self.assertEqual(command[1:3], ["model", "refresh"])
            self.assertNotIn("wayper.cli", command)
            lease = json.loads(_auto_retrain_lease_path(config).read_text())
            _release_auto_retrain_worker(config, lease["token"])

    def test_scheduler_detaches_one_worker_for_short_lived_callers(self) -> None:
        examples = [
            *_examples("ban", 10, ("bad", "detail"), 1),
            *_examples("keep", 10, ("good", "detail"), 0, start=1_700_001_000),
        ]
        model = _mark_semantic_model_ready(train_preference_model(examples, max_combo_features=100))
        with tempfile.TemporaryDirectory() as td:
            config = WayperConfig(download_dir=Path(td))
            save_preference_model(model, config.preference_model_file)
            with patch("wayper.preference_model.subprocess.Popen") as popen:
                popen.return_value.pid = os.getpid()
                schedule_preference_model_retrain(config)
                self.assertEqual(popen.call_count, 0)
                for index in range(10):
                    record_preference_feedback(config, "keep", f"keep{index}.jpg")
                schedule_preference_model_retrain(config)
                schedule_preference_model_retrain(config)

            self.assertEqual(popen.call_count, 1)
            command = popen.call_args.args[0]
            self.assertEqual(command[0], sys.executable)
            self.assertIn("wayper.cli", command)
            self.assertIn("model", command)
            self.assertIn("refresh", command)
            if os.name != "nt":
                self.assertTrue(popen.call_args.kwargs["start_new_session"])

            lease = json.loads(_auto_retrain_lease_path(config).read_text())
            _release_auto_retrain_worker(config, lease["token"])
            self.assertFalse(_auto_retrain_lease_path(config).exists())

    def test_automatic_commit_does_not_overwrite_matching_manual_fit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = WayperConfig(download_dir=Path(td))
            _write_cold_start_library(config)

            manual, snapshot = train_local_preference_model(
                config,
                max_combo_features=100,
            )
            save_preference_model(manual, config.preference_model_file)
            automatic = train_preference_model(
                list(snapshot.examples),
                max_combo_features=20,
                feedback_revision=snapshot.feedback_revision,
                retrain_mode="automatic",
            )

            self.assertTrue(_save_automatic_preference_model(config, automatic, snapshot))
            saved = load_preference_model(config.preference_model_file)

        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.training_summary["retrain_mode"], "manual")
        self.assertEqual(saved.max_combo_features, 100)

    def test_manual_commit_refuses_a_snapshot_changed_during_fit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = WayperConfig(download_dir=Path(td))
            _write_cold_start_library(config)

            model, snapshot = train_local_preference_model(
                config,
                max_combo_features=100,
            )
            record_preference_feedback(config, "keep", "keep0.jpg")

            committed = _save_manual_preference_model(config, model, snapshot)

        self.assertFalse(committed)

    def test_concurrent_model_saves_serialize_the_model_write(self) -> None:
        from wayper.util import atomic_write as real_atomic_write

        examples = [
            *_examples("ban", 10, ("bad", "detail"), 1),
            *_examples("keep", 10, ("good", "detail"), 0, start=1_700_001_000),
        ]
        first = train_preference_model(examples, max_combo_features=100)
        second = train_preference_model(examples, max_combo_features=20)
        active_writes = 0
        maximum_active_writes = 0
        counter_lock = threading.Lock()
        start = threading.Barrier(3)
        errors: list[Exception] = []

        def measured_write(path: Path, content: str) -> None:
            nonlocal active_writes, maximum_active_writes
            with counter_lock:
                active_writes += 1
                maximum_active_writes = max(maximum_active_writes, active_writes)
            try:
                time.sleep(0.01)
                real_atomic_write(path, content)
            finally:
                with counter_lock:
                    active_writes -= 1

        def save_from_thread(model) -> None:
            try:
                start.wait()
                save_preference_model(model, path)
            except Exception as exc:  # pragma: no cover - asserted after joining threads
                errors.append(exc)

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "model.json"
            with patch("wayper.preference_model.atomic_write", side_effect=measured_write):
                first_thread = threading.Thread(target=save_from_thread, args=(first,))
                second_thread = threading.Thread(target=save_from_thread, args=(second,))
                first_thread.start()
                second_thread.start()
                start.wait()
                first_thread.join()
                second_thread.join()

            saved = load_preference_model(path)

        self.assertEqual(errors, [])
        self.assertEqual(maximum_active_writes, 1)
        self.assertIsNotNone(saved)

    def test_feedback_append_reads_only_the_tail_revision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = WayperConfig(download_dir=Path(td))
            record_preference_feedback(config, "keep", "first.jpg", timestamp=100)
            with patch(
                "wayper.preference_model.load_preference_feedback",
                side_effect=AssertionError("full ledger read"),
            ):
                revision = record_preference_feedback(
                    config,
                    "dislike",
                    "second.jpg",
                    timestamp=101,
                )
            feedback = load_preference_feedback(config)

        self.assertEqual(revision, 2)
        self.assertEqual(feedback["revision"], 2)
        self.assertEqual([event["action"] for event in feedback["events"]], ["keep", "dislike"])


if __name__ == "__main__":
    unittest.main()
