from self_harness.evaluation.gating import GateConfig, PromotionGate
from self_harness.evaluation.metrics import GenerationMetrics


def metrics(version: str, scores: dict[str, float]) -> GenerationMetrics:
    return GenerationMetrics(harness_version=version, per_task_scores=scores)


def test_clear_improvement_is_promoted():
    gate = PromotionGate(GateConfig(bootstrap_samples=2000))
    incumbent = metrics("a", {f"t{i}": 0.4 for i in range(10)})
    candidate = metrics("b", {f"t{i}": 0.8 for i in range(10)})
    assert gate.should_promote(incumbent, candidate)


def test_regression_is_rejected():
    gate = PromotionGate(GateConfig(bootstrap_samples=2000))
    incumbent = metrics("a", {f"t{i}": 0.8 for i in range(10)})
    candidate = metrics("b", {f"t{i}": 0.5 for i in range(10)})
    assert not gate.should_promote(incumbent, candidate)


def test_sub_threshold_gain_is_rejected():
    gate = PromotionGate(GateConfig(min_relative_gain=0.05, bootstrap_samples=2000))
    incumbent = metrics("a", {f"t{i}": 0.50 for i in range(10)})
    candidate = metrics("b", {f"t{i}": 0.51 for i in range(10)})  # +2% relative
    assert not gate.should_promote(incumbent, candidate)


def test_disjoint_task_sets_never_promote():
    gate = PromotionGate()
    assert not gate.should_promote(metrics("a", {"x": 1.0}), metrics("b", {"y": 1.0}))
