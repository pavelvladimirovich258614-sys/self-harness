"""Methodological firewall: training pool and frozen benchmark suites must
be disjoint. A failure here invalidates every reported result."""

from self_harness.evaluation.benchmarks.base import get_suite


def test_training_pool_disjoint_from_frozen_suite():
    training_ids = {t.task_id for t in get_suite("training")}
    frozen_ids = {t.task_id for t in get_suite("longhorizon")}
    assert training_ids, "training pool must not be empty"
    assert frozen_ids, "frozen suite must not be empty"
    assert not (training_ids & frozen_ids)


def test_namespacing_convention():
    assert all(t.task_id.startswith("training/") for t in get_suite("training"))
    assert all(t.task_id.startswith("longhorizon/") for t in get_suite("longhorizon"))
