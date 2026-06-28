"""贝叶斯优化封装(spec §4.4)。

仅服务 change_type == "tunable_search":在 LLM 圈定的参数子集 + 范围上 minimize(score)。
封装 skopt.gp_minimize,把 [{key,range,type}] 映射成 skopt 维度,evaluate(point_dict)->float
由调用方注入(写 tunables→试玩→诊断→objective)。小预算(<8)退化为随机采样。
"""
import random

from skopt import gp_minimize
from skopt.space import Integer, Real

# 预算低于此值时 gp_minimize 高斯过程拟合不可靠,退化为随机采样
MIN_GP_CALLS = 8


def _make_dimensions(search_space):
    """[{key,range,type}] → (skopt dimensions, keys)。"""
    dims = []
    keys = []
    for spec in search_space:
        key = spec["key"]
        lo, hi = spec["range"]
        dtype = spec.get("type", "float")
        if dtype == "int":
            dims.append(Integer(int(lo), int(hi), name=key))
        else:
            dims.append(Real(float(lo), float(hi), name=key))
        keys.append(key)
    return dims, keys


def _scalar(value):
    """把 evaluate 的返回值压成 GP 用的标量(越小越好)。

    支持两种返回类型:
      - 旧:float(直接用);
      - 新:evaluation.EvaluationResult(取 .mean_score)。
    """
    mean_score = getattr(value, "mean_score", None)
    if mean_score is not None:
        return float(mean_score)
    return float(value)


def optimize(search_space, evaluate, n_calls=12, random_state=0):
    """在 search_space 上最小化 evaluate,返回 (best_point_dict, best_value)。

    search_space: [{"key": str, "range": [lo, hi], "type": "int"|"float"}]
    evaluate:     callable(point_dict) -> float | EvaluationResult(越小越好)
    n_calls:      评估预算;<8 退化为随机采样。

    返回的 best_value 是 evaluate 在最优点处的**原始返回对象**:
    evaluate 返回 EvaluationResult 时返回该 EvaluationResult(供接受门做
    配对改善);返回 float 时返回 float(向后兼容)。GP 内部只用 mean_score
    /float 标量(见 _scalar),但每个已评估点的完整结果会被保留以供选最优。
    """
    if not search_space:
        raise ValueError("search_space 不能为空")

    dims, keys = _make_dimensions(search_space)

    # 已评估点的完整返回值缓存:key 为 dims 同序的 tuple(int 已归一化)。
    evaluated = {}

    def _point_dict(point):
        out = {}
        for key, val, spec in zip(keys, point, search_space):
            if spec.get("type", "float") == "int":
                val = int(val)
            out[key] = val
        return out

    def objective_fn(point):
        # skopt 传入与 dims 同序的 list;映射回 dict 给调用方的 evaluate。
        point_dict = _point_dict(point)
        value = evaluate(point_dict)
        evaluated[tuple(point_dict[k] for k in keys)] = value
        return _scalar(value)

    if n_calls < MIN_GP_CALLS:
        # 小预算:GP 拟合不可靠,直接在范围内随机采样取最优(避免 skopt
        # dummy_minimize 与新版 scikit-learn __sklearn_tags__ 不兼容的坑)。
        rng = random.Random(random_state)
        best_point, best_score = None, float("inf")
        for _ in range(n_calls):
            point = []
            for spec in search_space:
                lo, hi = spec["range"]
                if spec.get("type", "float") == "int":
                    point.append(rng.randint(int(lo), int(hi)))
                else:
                    point.append(rng.uniform(float(lo), float(hi)))
            s = objective_fn(point)
            if s < best_score:
                best_score, best_point = s, point
        result_x = best_point
    else:
        # n_initial_points 取若干随机点暖启,其余交给 GP。
        n_initial = min(max(3, n_calls // 3), n_calls)
        result = gp_minimize(objective_fn, dims, n_calls=n_calls,
                             n_initial_points=n_initial,
                             acq_func="EI", random_state=random_state)
        result_x = result.x

    best_point = _point_dict(result_x)
    # 从缓存取最优点对应的原始返回对象(EvaluationResult 或 float)。
    best_key = tuple(best_point[k] for k in keys)
    best_value = evaluated.get(best_key)
    if best_value is None:
        # 兜底:理论上 result_x 必在 evaluated 中;万一缺失则重评一次。
        best_value = evaluate(best_point)
    return best_point, best_value
