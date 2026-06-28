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


def optimize(search_space, evaluate, n_calls=12, random_state=0):
    """在 search_space 上最小化 evaluate,返回 (best_point_dict, best_score)。

    search_space: [{"key": str, "range": [lo, hi], "type": "int"|"float"}]
    evaluate:     callable(point_dict) -> float(越小越好)
    n_calls:      评估预算;<8 退化为 dummy_minimize(随机采样)。
    """
    if not search_space:
        raise ValueError("search_space 不能为空")

    dims, keys = _make_dimensions(search_space)

    def objective_fn(point):
        # skopt 传入与 dims 同序的 list;映射回 dict 给调用方的 evaluate。
        point_dict = {}
        for key, val, spec in zip(keys, point, search_space):
            if spec.get("type", "float") == "int":
                val = int(val)
            point_dict[key] = val
        return float(evaluate(point_dict))

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
        result_x, result_fun = best_point, best_score
    else:
        # n_initial_points 取若干随机点暖启,其余交给 GP。
        n_initial = min(max(3, n_calls // 3), n_calls)
        result = gp_minimize(objective_fn, dims, n_calls=n_calls,
                             n_initial_points=n_initial,
                             acq_func="EI", random_state=random_state)
        result_x, result_fun = result.x, result.fun

    best_point = {}
    for key, val, spec in zip(keys, result_x, search_space):
        if spec.get("type", "float") == "int":
            val = int(val)
        best_point[key] = val
    return best_point, float(result_fun)
