"""Shared-scope gate: a fact about the committing host must not be stored."""

import json
from pathlib import Path

import pytest
from harness import VALID_RETRIEVAL, commit_args

from hippocampus import constants as C, shareability

# The fact that reached the shared bank and prompted this gate.
HOST_LOCAL_FACT = (
    "本机默认 Python 3 版本为 3.8，且未安装 LibreOffice、poppler 和 qpdf；"
    "docx、pptx、pdf、xlsx 技能的 scripts/office 使用 Python 3.9 以上语法，因此会崩溃。"
)
# retrieval_text carries a 100-token floor, so append rather than replace.
HOST_LOCAL_RETRIEVAL = VALID_RETRIEVAL + HOST_LOCAL_FACT


@pytest.mark.parametrize("text,expected", [
    (HOST_LOCAL_FACT, ["本机"]),
    ("本地磁盘只剩 2 GB", ["本地"]),
    ("这台机器没有装 Docker", ["这台机器"]),
    ("Python 3.8 is what this machine ships", ["this machine"]),
    ("THIS  Machine has no qpdf", ["this  machine"]),   # case- and space-insensitive
    ("我的电脑和本机都需要升级", ["我的电脑", "本机"]),
    # general-purpose compounds that merely start with a marker
    ("完成了界面的本地化工作", []),
    ("按本地时间 08:00 触发", []),
    ("把结果存进本地变量再返回", []),
    # a fact that names its host stays shareable
    ("pi5 (192.168.2.41) 默认 Python 版本为 3.11", []),
    ("mac-claude 的 Python 是 3.8", []),
])
def test_marker_detection(text, expected):
    assert shareability.markers_in(text) == expected


def test_host_local_commit_is_rejected(lab, key):
    status, data, is_error = lab.call(
        "memory_commit", commit_args(key(), retrieval_text=HOST_LOCAL_RETRIEVAL))
    assert is_error and status == 200
    assert data["code"] == C.E_NOT_SHAREABLE
    assert data["stored"] is False and data["indexed"] is False
    assert data["fields"] == ["retrieval_text"] and data["markers"] == ["本机"]


def test_rejection_stores_nothing_and_audits_the_reason(lab, key):
    lab.call("memory_commit", commit_args(key(), summary=HOST_LOCAL_FACT))
    assert list(Path(lab.vault).rglob("*.md")) == []
    conn = lab.db()
    try:
        assert conn.execute(
            "SELECT COUNT(*) c FROM idempotency_reservation").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"] == 0
        row = conn.execute(
            "SELECT state, outcome_code, redacted_fields, redacted_categories"
            " FROM audit ORDER BY rowid DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert row["state"] == "rejected"
    assert row["outcome_code"] == C.OUTCOME_NOT_SHAREABLE
    assert json.loads(row["redacted_fields"]) == ["summary"]
    assert json.loads(row["redacted_categories"]) == [shareability.CATEGORY]


def test_every_business_field_is_gated(lab, key):
    for field in ("title", "summary", "retrieval_text", "detail_body"):
        args = commit_args(key(), **{field: f"这台电脑 {commit_args('x')[field]}"})
        _, data, is_error = lab.call("memory_commit", args)
        assert is_error and data["code"] == C.E_NOT_SHAREABLE, field
        assert data["fields"] == [field]


def test_shareable_commit_still_passes(lab, key):
    _, data, is_error = lab.call(
        "memory_commit",
        commit_args(key(), retrieval_text=VALID_RETRIEVAL
                    + "pi5 192.168.2.41 的默认 Python 版本是 3.11，本地化文案已经全部完成，"
                      "按本地时间 08:00 生成报表，结果先存进本地变量再返回。"))
    assert not is_error and data["stored"] is True


def test_secret_takes_precedence_over_locality(lab, key):
    _, data, is_error = lab.call(
        "memory_commit",
        commit_args(key(), retrieval_text=VALID_RETRIEVAL
                    + "本机的密钥是 AKIACANARY0EXAMPLE99，请勿外传。"))
    assert is_error and data["code"] == C.E_SECRET_REJECTED
