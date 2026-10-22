# 2026-10-11 画像模块 `_validate_iso_date` 重名遮蔽导致任务/目标路由回归

## 错误现象
新增 1.3.1 画像模块后，单跑 `tests/test_profile_items.py` 全绿；但全量 `pytest -q` 出现 6 个既有测试失败：

```
FAILED tests/test_work.py::test_goal_crud_and_filters - TypeError: _validate_...
FAILED tests/test_work.py::test_delete_goal_nulls_task_link - TypeError: _validate_...
FAILED tests/test_work.py::test_task_crud_status_and_filters - TypeError: _validate_...
FAILED tests/test_work.py::test_search_work_items_ranking_status - TypeError: _validate_...
FAILED tests/test_work.py::test_chat_includes_work_items - TypeError: _validate_...
```

报错点：创建任务/目标调用日期校验时 `TypeError`（参数数量不符）。

## 发生场景
在 `app/main.py` 插入画像路由块时，块内新定义了一个单参数辅助函数：

```python
def _validate_iso_date(value: str | None) -> str | None:   # 我新加的
```

而 main.py 第 1071 行**早已存在**一个同名、双参数的函数：

```python
def _validate_iso_date(value: str | None, field: str) -> str | None:  # 3.4 已存在
```

Python 后定义覆盖先定义。我的画像块插入位置在任务/目标路由**之后**，模块加载完成后全局名最终指向单参数版本；任务/目标路由运行时仍按双参数调用 `_validate_iso_date(x, "due_date")`，于是 TypeError。

单跑画像测试不触发任务/目标路由，所以隔离运行时被掩盖；只有全量运行才暴露。

## 根因分析
1. 在同一单文件模块里新增顶层辅助函数前，没有先检查是否已有同名函数。
2. 只跑了新模块的测试就以为没问题，没有及时跑全量回归。

## 解决方案及验证
- 删除画像块里重复的 `_validate_iso_date`，统一复用既有双参数版本；画像创建/更新处改为 `_validate_iso_date(v, "valid_from" | "valid_until")`，非法日期沿用项目既有约定返回 400（同步把画像测试中相应预期从 422 改为 400）。
- 验证：`pytest -q` → **74 passed**（原 60 + 新增 14），test_work 回归全部消失。

## 预防
- 往 main.py 这种集中式大文件加顶层函数前，先 `Select-String 'def <名字>'` 查重；新功能辅助函数尽量用模块专属前缀（如 `_profile_*`）或放进独立模块。
- 每完成一个功能切片就跑一次**全量**测试，而不仅是新增测试。

---

## 附：同一轮画像测试自身的两处小修正（非产品代码缺陷）
1. 辅助函数 `_make_pending` 两次返回相同 content 的待确认条目，第二条被画像提议的「非 rejected 去重」正确跳过，导致 `items[0]` 取空报 IndexError。改为每次生成不同内容。
2. 删除用例中误用普通字符串 `"/api/profile-items/{item_id}"`（未 f-string 插值），FastAPI 把字面 `{item_id}` 判为非法 int 路径参数返回 422 而非预期 404。改为 f-string。
