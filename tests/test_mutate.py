"""tests/test_mutate.py — TDD for harness/mutate.py 纯函数部分。

覆盖:
  allowed(plan, protected_globs) -> bool
  apply_tunable(path, key, value) 写回 + clamp
"""
import json
import tempfile
import os
import sys

# 确保能 import harness/mutate.py（conftest.py 已加 harness 到 sys.path）
import pytest

# --------------------------------------------------------------------------- #
# 辅助工具                                                                     #
# --------------------------------------------------------------------------- #

def _make_tunables(tmp_path, params: dict) -> str:
    """在 tmp_path 下写一个临时 tunables.json，返回路径。"""
    data = {"version": 1, "params": params}
    p = os.path.join(tmp_path, "tunables.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return p


# --------------------------------------------------------------------------- #
# allowed() 测试                                                                #
# --------------------------------------------------------------------------- #

class TestAllowed:
    """allowed(plan, protected_globs) -> bool"""

    def _plan(self, files=None, change_type="tunable_search", field=None):
        """构造最小 plan dict。"""
        p = {"change_type": change_type, "files": files or []}
        if field:
            p["field"] = field
        return p

    def test_default_protected_harness(self):
        """目标文件命中 harness/** 返回 False。"""
        from mutate import allowed
        plan = self._plan(files=["harness/mutate.py"])
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is False

    def test_default_protected_git(self):
        """目标文件命中 .git/** 返回 False。"""
        from mutate import allowed
        plan = self._plan(files=[".git/HEAD"])
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is False

    def test_default_protected_tests(self):
        """目标文件命中 tests/** 返回 False。"""
        from mutate import allowed
        plan = self._plan(files=["tests/test_mutate.py"])
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is False

    def test_default_protected_docs(self):
        """目标文件命中 docs/** 返回 False。"""
        from mutate import allowed
        plan = self._plan(files=["docs/README.md"])
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is False

    def test_allowed_game_file(self):
        """目标文件不在 protected 范围内,返回 True。"""
        from mutate import allowed
        plan = self._plan(files=["example_platformer/level.gd"])
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is True

    def test_allowed_tunable_value_field(self):
        """plan 改的是 tunables.json 的 value 字段 → 允许。"""
        from mutate import allowed
        plan = self._plan(files=["rl/tunables.json"], field="value")
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is True

    def test_blocked_tunable_range_field(self):
        """plan 改 tunables.json 的 range 字段 → 拒绝。"""
        from mutate import allowed
        plan = self._plan(files=["rl/tunables.json"], field="range")
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is False

    def test_blocked_tunable_type_field(self):
        """plan 改 tunables.json 的 type 字段 → 拒绝。"""
        from mutate import allowed
        plan = self._plan(files=["rl/tunables.json"], field="type")
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is False

    def test_blocked_tunable_desc_field(self):
        """plan 改 tunables.json 的 desc 字段 → 拒绝。"""
        from mutate import allowed
        plan = self._plan(files=["rl/tunables.json"], field="desc")
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is False

    def test_multiple_files_one_protected(self):
        """多个目标文件，任意一个命中 protected → 返回 False。"""
        from mutate import allowed
        plan = self._plan(files=["example_platformer/level.gd", "harness/mutate.py"])
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is False

    def test_empty_files_allowed(self):
        """无目标文件（纯 tunables search）且无 field 限制 → 允许。"""
        from mutate import allowed
        plan = self._plan(files=[])
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is True


# --------------------------------------------------------------------------- #
# apply_tunable() 测试                                                         #
# --------------------------------------------------------------------------- #

class TestApplyTunable:
    """apply_tunable(path, key, value) 写回 + clamp 到 range。"""

    def test_write_value_in_range(self, tmp_path):
        """在 range 内的值直接写入。"""
        from mutate import apply_tunable
        path = _make_tunables(tmp_path, {
            "gap_width": {"value": 120, "range": [60, 200], "type": "float", "desc": "缺口宽度"}
        })
        apply_tunable(path, "gap_width", 150.0)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["params"]["gap_width"]["value"] == 150.0

    def test_clamp_above_max(self, tmp_path):
        """超过 range 上限时 clamp 到上限。"""
        from mutate import apply_tunable
        path = _make_tunables(tmp_path, {
            "gap_width": {"value": 120, "range": [60, 200], "type": "float", "desc": "缺口宽度"}
        })
        apply_tunable(path, "gap_width", 999.0)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["params"]["gap_width"]["value"] == 200

    def test_clamp_below_min(self, tmp_path):
        """低于 range 下限时 clamp 到下限。"""
        from mutate import apply_tunable
        path = _make_tunables(tmp_path, {
            "gap_width": {"value": 120, "range": [60, 200], "type": "float", "desc": "缺口宽度"}
        })
        apply_tunable(path, "gap_width", 10.0)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["params"]["gap_width"]["value"] == 60

    def test_int_param(self, tmp_path):
        """整数参数写回。"""
        from mutate import apply_tunable
        path = _make_tunables(tmp_path, {
            "enemy_hp": {"value": 3, "range": [1, 8], "type": "int", "desc": "敌人血量"}
        })
        apply_tunable(path, "enemy_hp", 5)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["params"]["enemy_hp"]["value"] == 5

    def test_other_params_untouched(self, tmp_path):
        """只改目标 key，其他参数原样保留。"""
        from mutate import apply_tunable
        path = _make_tunables(tmp_path, {
            "gap_width": {"value": 120, "range": [60, 200], "type": "float", "desc": "缺口宽度"},
            "enemy_hp":  {"value": 3,   "range": [1, 8],    "type": "int",   "desc": "敌人血量"}
        })
        apply_tunable(path, "gap_width", 100.0)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        # enemy_hp 原样
        assert data["params"]["enemy_hp"]["value"] == 3

    def test_range_fields_preserved(self, tmp_path):
        """apply_tunable 不得修改 range/type/desc 字段。"""
        from mutate import apply_tunable
        path = _make_tunables(tmp_path, {
            "jump_force": {"value": 400, "range": [300, 600], "type": "float", "desc": "跳跃力"}
        })
        apply_tunable(path, "jump_force", 500.0)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        p = data["params"]["jump_force"]
        assert p["range"] == [300, 600]
        assert p["type"] == "float"
        assert p["desc"] == "跳跃力"

    def test_key_not_found_raises(self, tmp_path):
        """key 不存在时应抛 KeyError。"""
        from mutate import apply_tunable
        path = _make_tunables(tmp_path, {
            "gap_width": {"value": 120, "range": [60, 200], "type": "float", "desc": "缺口宽度"}
        })
        with pytest.raises(KeyError):
            apply_tunable(path, "nonexistent_key", 100.0)


# --------------------------------------------------------------------------- #
# Task 5: 定向 Git 与路径 containment 集成测试                                  #
# --------------------------------------------------------------------------- #

def _init_tmp_git_repo(base: str) -> str:
    """在 base 下初始化一个最小 git 仓，返回仓根路径。"""
    import subprocess
    repo = base
    subprocess.run(["git", "init", repo], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo, check=True, capture_output=True,
    )
    # 初始提交，让 HEAD 存在
    init_file = os.path.join(repo, "README.md")
    with open(init_file, "w") as f:
        f.write("init\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo, check=True, capture_output=True,
    )
    return repo


class TestGitTargeted:
    """Task 5: snapshot/rollback/commit 定向 git 操作 + 路径 containment。"""

    def test_snapshot_returns_bytes_dict(self, tmp_path):
        """snapshot(paths) 返回 dict[str, bytes]，key 为 repo-relative 路径。"""
        from mutate import snapshot
        repo = _init_tmp_git_repo(str(tmp_path))
        allowed_file = os.path.join(repo, "allowed.json")
        with open(allowed_file, "w") as f:
            f.write('{"v": 1}')
        snap = snapshot(["allowed.json"], repo_root=repo)
        assert isinstance(snap, dict)
        assert "allowed.json" in snap
        assert snap["allowed.json"] == b'{"v": 1}'

    def test_rollback_only_restores_whitelisted_file(self, tmp_path):
        """rollback 只恢复白名单文件，仓内其他文件的改动仍保留。"""
        from mutate import snapshot, rollback
        repo = _init_tmp_git_repo(str(tmp_path))

        # 创建两个文件
        allowed_file = os.path.join(repo, "allowed.json")
        other_file = os.path.join(repo, "developer.txt")
        with open(allowed_file, "w") as f:
            f.write("original")
        with open(other_file, "w") as f:
            f.write("original-dev")

        # 对 allowed.json 快照
        snap = snapshot(["allowed.json"], repo_root=repo)

        # 同时修改两个文件
        with open(allowed_file, "w") as f:
            f.write("modified")
        with open(other_file, "w") as f:
            f.write("modified-dev")

        # rollback 只恢复 allowed.json
        rollback(snap, repo_root=repo)

        with open(allowed_file) as f:
            assert f.read() == "original", "allowed.json 应被还原"
        with open(other_file) as f:
            assert f.read() == "modified-dev", "developer.txt 的改动应仍在"

    def test_rollback_removes_file_that_did_not_exist_at_snapshot(self, tmp_path):
        """rollback 时，若文件在 snapshot 时不存在，应删除该文件（还原为不存在）。"""
        from mutate import snapshot, rollback
        repo = _init_tmp_git_repo(str(tmp_path))

        # snapshot 时 new_file.json 尚不存在
        snap = snapshot(["new_file.json"], repo_root=repo)
        assert snap["new_file.json"] is None  # 记录为不存在

        # 之后创建了该文件
        new_file = os.path.join(repo, "new_file.json")
        with open(new_file, "w") as f:
            f.write("should-be-deleted")

        # rollback 应删除该文件
        rollback(snap, repo_root=repo)
        assert not os.path.exists(new_file), "快照时不存在的文件应被删除"

    def test_commit_only_stages_whitelisted_file(self, tmp_path):
        """commit(msg, paths) 只暂存并提交白名单路径，其余改动不入 commit。"""
        import subprocess
        from mutate import commit
        repo = _init_tmp_git_repo(str(tmp_path))

        allowed_file = os.path.join(repo, "allowed.json")
        other_file = os.path.join(repo, "developer.txt")
        with open(allowed_file, "w") as f:
            f.write('{"v": 2}')
        with open(other_file, "w") as f:
            f.write("side-change")

        commit("test commit", ["allowed.json"], repo_root=repo)

        # 检查 HEAD commit 包含 allowed.json，不包含 developer.txt
        committed_files = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()
        assert "allowed.json" in committed_files, "allowed.json 应在 commit 中"
        assert "developer.txt" not in committed_files, "developer.txt 不应在 commit 中"

        # developer.txt 仍处于未暂存状态
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout
        assert "developer.txt" in status, "developer.txt 应仍是未暂存改动"

    def test_path_traversal_raises(self, tmp_path):
        """../outside.json 等仓外相对路径应抛 ValueError。"""
        from mutate import snapshot
        repo = _init_tmp_git_repo(str(tmp_path / "repo"))
        with pytest.raises(ValueError, match="仓库根目录之外"):
            snapshot(["../outside.json"], repo_root=repo)

    def test_absolute_outside_path_raises(self, tmp_path):
        """绝对仓外路径应抛 ValueError。"""
        from mutate import snapshot
        repo = _init_tmp_git_repo(str(tmp_path / "repo"))
        outside = str(tmp_path / "outside.json")
        with pytest.raises(ValueError, match="仓库根目录之外"):
            snapshot([outside], repo_root=repo)

    def test_symlink_outside_repo_raises(self, tmp_path):
        """指向仓外的 symlink 应抛 ValueError。"""
        from mutate import snapshot
        repo = _init_tmp_git_repo(str(tmp_path / "repo"))
        outside_target = tmp_path / "secret.json"
        outside_target.write_text("secret")
        symlink_in_repo = os.path.join(repo, "link.json")
        os.symlink(str(outside_target), symlink_in_repo)
        with pytest.raises(ValueError, match="仓库根目录之外"):
            snapshot(["link.json"], repo_root=repo)

    def test_no_reset_hard_in_source(self):
        """mutate.py 源码中不得出现 reset --hard（精确子串匹配）。"""
        import inspect
        import mutate
        src = inspect.getsource(mutate)
        assert "reset --hard" not in src, \
            "mutate.py 不得包含 git reset --hard"

    def test_no_add_dash_a_in_source(self):
        """mutate.py 源码中不得出现 add -A（精确子串匹配）。"""
        import inspect
        import mutate
        src = inspect.getsource(mutate)
        assert "add -A" not in src, \
            "mutate.py 不得包含 git add -A"
