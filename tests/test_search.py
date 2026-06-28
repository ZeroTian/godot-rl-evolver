"""search.optimize 贝叶斯封装冒烟测试(spec §4.4)。

用 mock evaluate(简单二次函数)断言能收敛到近最优点。
"""
import search


def test_optimize_converges_quadratic_float():
    # f(x) = (x-3)^2,最优在 x=3,score 最小
    space = [{"key": "x", "range": [0.0, 6.0], "type": "float"}]

    def evaluate(point):
        return (point["x"] - 3.0) ** 2

    best, best_score = search.optimize(space, evaluate, n_calls=15)
    assert abs(best["x"] - 3.0) < 0.6
    assert best_score < 0.5


def test_optimize_integer_dimension():
    space = [{"key": "hp", "range": [1, 8], "type": "int"}]

    def evaluate(point):
        # 最优整数点 hp=5
        assert isinstance(point["hp"], int)
        return abs(point["hp"] - 5)

    best, best_score = search.optimize(space, evaluate, n_calls=12)
    assert best["hp"] == 5
    assert best_score == 0


def test_optimize_two_dims():
    space = [
        {"key": "a", "range": [0.0, 10.0], "type": "float"},
        {"key": "b", "range": [0.0, 10.0], "type": "float"},
    ]

    def evaluate(point):
        return (point["a"] - 2.0) ** 2 + (point["b"] - 8.0) ** 2

    best, best_score = search.optimize(space, evaluate, n_calls=20)
    assert best_score < 2.0


def test_small_budget_falls_back_to_random():
    # n_calls<8 走随机采样,仍应返回评估过的最优点
    space = [{"key": "x", "range": [0.0, 10.0], "type": "float"}]
    seen = []

    def evaluate(point):
        v = (point["x"] - 5.0) ** 2
        seen.append((point["x"], v))
        return v

    best, best_score = search.optimize(space, evaluate, n_calls=5)
    assert len(seen) == 5
    # best_score 应等于实际评估过的最小值
    assert abs(best_score - min(v for _, v in seen)) < 1e-9
    assert "x" in best
