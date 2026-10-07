"""覆盖缺口补测样本（Coverage Gap Samples）引擎与 HTTP API 测试。"""
import hashlib
import io
import json
import os
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def _zip_bytes(folder: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(folder):
            for fn in files:
                full = os.path.join(root, fn)
                z.write(full, os.path.relpath(full, folder))
    return buf.getvalue()


def _ndjson(rows) -> bytes:
    return ("\n".join(
        r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
        for r in rows)).encode()


@pytest.fixture(scope="module")
def analysis_id(client):
    r = client.post(
        "/api/analyses",
        files={
            "baseline": ("b.zip", _zip_bytes("examples/baseline"),
                         "application/zip"),
            "candidate": ("c.zip", _zip_bytes("examples/candidate"),
                          "application/zip"),
        })
    assert r.status_code == 201, r.text
    return r.json()["analysis_id"]


def _analysis_from_docs(client, baseline: dict, candidate=None):
    candidate = candidate or baseline
    r = client.post(
        "/api/analyses",
        files={
            "baseline": ("b.json", json.dumps(baseline).encode(),
                         "application/json"),
            "candidate": ("c.json", json.dumps(candidate).encode(),
                          "application/json"),
        })
    assert r.status_code == 201, r.text
    return r.json()["analysis_id"]


def _spec(paths):
    return {"openapi": "3.0.3", "info": {"title": "b", "version": "1"},
            "paths": paths}


def _json_resp(desc="ok", schema=None):
    """一个 application/json 响应；schema 默认 {}（任意对象）。"""
    return {"description": desc,
            "content": {"application/json": {
                "schema": {} if schema is None else schema}}}


def _upload(client, aid, side, rows):
    return client.post(
        f"/api/analyses/{aid}/coverage/{side}",
        files={"file": ("calls.ndjson", _ndjson(rows),
                        "application/x-ndjson")})


def _gaps(client, aid, side="baseline"):
    return client.get(
        f"/api/analyses/{aid}/coverage/{side}/gap-samples")


def _reupload_sample_covers_target(client, aid, s):
    rd = _upload(client, aid, "baseline", [s["sample"]]).json()
    covered = [row for row in rd["rows"] if row["outcome"] == "covered"]
    assert len(covered) == 1, (s["cell"], rd["rows"])
    assert covered[0]["operation"] == s["operation"]
    assert covered[0]["cell"] == s["cell"], \
        (s["cell"], covered[0]["cell"])
    return covered[0]


# ============================================================== 示例文档链路
def test_example_packages_generate_samples_for_real_gaps(client, analysis_id):
    rows = [
        {"method": "GET", "path": "/pets",
         "query": {"status": "available", "limit": "10"}, "request": {},
         "response": {"status": 200, "media_type": "application/json",
                      "body": [{"id": 1, "name": "Rex",
                                "status": "available"}]}},
        {"method": "POST", "path": "/pets",
         "request": {"media_type": "application/json",
                     "body": {"name": "Luna"}},
         "response": {"status": 201, "media_type": "application/json",
                      "body": {"id": 2, "name": "Luna",
                               "status": "pending"}}},
        {"method": "GET", "path": "/pets/7", "request": {},
         "response": {"status": 200, "media_type": "application/json",
                      "body": {"id": 7, "name": "Coco", "status": "sold"}}},
        {"method": "GET", "path": "/pets/7", "request": {},
         "response": {"status": 404}},
        {"method": "GET", "path": "/customers/c-100", "request": {},
         "response": {"status": 200, "media_type": "application/json",
                      "body": {"id": "c-100", "displayName": "Alice"}}},
        {"method": "POST", "path": "/legacy/echo",
         "request": {"media_type": "application/json",
                     "body": {"message": "hi"}},
         "response": {"status": 200, "media_type": "application/json",
                      "body": {"message": "hi"}}},
    ]
    up = _upload(client, analysis_id, "baseline", rows)
    assert up.status_code == 201, up.text
    cov = up.json()
    gap_cells = {g["key"] for op in cov["operations"] for g in op["gaps"]}

    r = _gaps(client, analysis_id)
    assert r.status_code == 200, r.text
    doc = r.json()
    by_cell = {s["cell"]: s for s in doc["samples"]}
    un_cells = {u["cell"] for u in doc["unavailable"]}
    assert doc["summary"]["gaps_total"] == len(gap_cells)
    assert set(by_cell) | un_cells == gap_cells

    # POST /pets 的 400 无实体格：样本带必填请求体、响应无 body 键
    s400 = by_cell["400|(no content)"]
    sample = s400["sample"]
    assert sample["response"]["status"] == 400
    assert "body" not in sample["response"]
    assert sample["request"]["body"]["name"]  # 必填请求体有最小有效值

    # /legacy/decide 200 格：必填请求体 schema 含未支持的 not，逐格说明
    decide = next(u for u in doc["unavailable"]
                  if u["operation"] == "POST /legacy/decide")
    assert decide["cell"] == "200|application/json"
    assert any("not" in x for x in decide["reasons"])

    # 只读接口不修改已保存覆盖：再次读取结果与首次生成时一致
    doc_again = _gaps(client, analysis_id).json()
    assert doc_again["summary"] == doc["summary"]
    assert doc_again["ndjson"] == doc["ndjson"]

    # 每个生成的样本重新喂给覆盖引擎，必须确实覆盖其声称的格
    # （重复上传会替换已保存覆盖，属调用方显式操作，与只读生成无关）
    for s in doc["samples"]:
        _reupload_sample_covers_target(client, analysis_id, s)

    # 恢复干净状态：重新上传原始记录
    _upload(client, analysis_id, "baseline", rows)


def test_generated_ndjson_reupload_closes_generatable_gaps(
        client, analysis_id):
    rows = [
        {"method": "GET", "path": "/pets",
         "query": {"status": "available"}, "request": {},
         "response": {"status": 200, "media_type": "application/json",
                      "body": [{"id": 1, "name": "Rex",
                                "status": "available"}]}},
    ]
    _upload(client, analysis_id, "candidate", rows)
    r = _gaps(client, analysis_id, "candidate")
    doc = r.json()
    assert doc["summary"]["samples"] >= 1
    lines = [ln for ln in doc["ndjson"].splitlines() if ln.strip()]
    assert len(lines) == doc["summary"]["samples"]
    for ln, s in zip(lines, doc["samples"]):
        assert json.loads(ln) == s["sample"]

    merged = _ndjson(rows) + b"\n" + doc["ndjson"].encode()
    up = client.post(
        f"/api/analyses/{analysis_id}/coverage/candidate",
        files={"file": ("merged.ndjson", merged,
                        "application/x-ndjson")})
    merged_doc = up.json()
    covered_cells = {
        f"{op['method']} {op['path']}|{c['key']}"
        for op in merged_doc["operations"] for c in op["cells"]}
    for s in doc["samples"]:
        assert f"{s['operation']}|{s['cell']}" in covered_cells
    _upload(client, analysis_id, "candidate", rows)


# ============================================================== 状态码归属
def test_status_selection_exact_range_default(client):
    op = {"responses": {"200": _json_resp(), "2XX": _json_resp("range"),
                        "default": _json_resp("def")}}
    aid = _analysis_from_docs(client, _spec({"/p": {"get": op}}))
    _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/p",
         "response": {"status": 200, "media_type": "application/json",
                      "body": {}}}])
    doc = _gaps(client, aid).json()
    chosen = {s["declared_status"]: s["concrete_status"]
              for s in doc["samples"]}
    # 范围键选最小的未抢占同族码；default 选 404（不在任何精确/范围内）
    assert chosen == {"2XX": 201, "default": 404}
    for s in doc["samples"]:
        row = _reupload_sample_covers_target(client, aid, s)
        assert row["resolved_status"] == s["declared_status"]


def test_default_skips_preempting_range_keys(client):
    # 5XX 范围存在时，default 不能选 5xx 族码；500 又是精确码
    op = {"responses": {
        "200": {"description": "ok"},
        "500": _json_resp("srv"),
        "5XX": _json_resp("range"),
        "default": {"description": "def"}}}
    aid = _analysis_from_docs(client, _spec({"/p": {"get": op}}))
    _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/p", "response": {"status": 200}},
        {"method": "GET", "path": "/p",
         "response": {"status": 500, "media_type": "application/json",
                      "body": {}}},
        {"method": "GET", "path": "/p",
         "response": {"status": 503, "media_type": "application/json",
                      "body": {}}},
    ])
    doc = _gaps(client, aid).json()
    s = next(x for x in doc["samples"]
             if x["declared_status"] == "default")
    assert str(s["concrete_status"])[0] != "5"
    assert s["concrete_status"] != 500
    row = _reupload_sample_covers_target(client, aid, s)
    assert row["resolved_status"] == "default"


def test_range_cell_unavailable_when_all_exact(client):
    # 同族 100 个码全是精确声明（极端文档）→ 范围格无可用码
    responses = {str(n): {"description": "x"} for n in range(200, 300)}
    responses["2XX"] = {"description": "range"}
    aid = _analysis_from_docs(
        client, _spec({"/p": {"get": {"responses": responses}}}))
    _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/p", "response": {"status": 200}}])
    doc = _gaps(client, aid).json()
    u = next(x for x in doc["unavailable"]
             if x["declared_status"] == "2XX")
    assert "范围键" in u["reasons"][0]


# ============================================================== 路径抢占 / 歧义
def _template_get_op(extra_param_schema=None):
    params = [{"name": "x", "in": "path", "required": True,
               "schema": extra_param_schema or {"type": "integer"}}]
    return {"parameters": params,
            "responses": {"200": _json_resp("tpl")}}


def test_exact_path_preemption_uses_other_concrete_path(client):
    paths = {
        "/p": {"get": {"responses": {"404": {"description": "exact"}}}},
        "/{x}": {"get": _template_get_op()}}
    aid = _analysis_from_docs(client, _spec(paths))
    _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/p", "response": {"status": 404}}])
    doc = _gaps(client, aid).json()
    s = doc["samples"][0]
    assert s["sample"]["path"] != "/p"
    row = _reupload_sample_covers_target(client, aid, s)
    assert row["matched_template"] == "/{x}"
    assert row["cell"] == "200|application/json"


def test_template_cannot_escape_exact_path_reports_reason(client):
    # 路径参数枚举只有 "p"，任何具体路径都被精确 /p 抢占
    paths = {
        "/p": {"get": {"responses": {"404": {"description": "exact"}}}},
        "/{x}": {"get": _template_get_op(
            {"type": "string", "enum": ["p"]})}}
    aid = _analysis_from_docs(client, _spec(paths))
    _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/p", "response": {"status": 404}}])
    doc = _gaps(client, aid).json()
    assert doc["summary"]["samples"] == 0
    reason = doc["unavailable"][0]["reasons"][0]
    assert "归属" in reason or "抢占" in reason


def test_ambiguous_template_paths_reported(client):
    # 枚举把 /{a}/x 固定成 /z/x（精确路径存在），无法唯一归属
    a_op = {
        "parameters": [{"name": "a", "in": "path", "required": True,
                        "schema": {"type": "string", "enum": ["z"]}}],
        "responses": {"200": _json_resp("a")}}
    paths = {
        "/z/x": {"get": {"responses": {"204": {"description": "exact"}}}},
        "/{a}/x": {"get": a_op}}
    aid = _analysis_from_docs(client, _spec(paths))
    _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/z/x", "response": {"status": 204}}])
    doc = _gaps(client, aid).json()
    assert doc["summary"]["samples"] == 0
    reason = doc["unavailable"][0]["reasons"][0]
    assert "归属" in reason


def test_undeclared_template_variable_is_unavailable(client):
    paths = {
        "/p": {"get": {"responses": {"404": {"description": "exact"}}}},
        "/{x}": {"get": {"responses": {"200": _json_resp("tpl")}}}}
    aid = _analysis_from_docs(client, _spec(paths))
    _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/p", "response": {"status": 404}}])
    doc = _gaps(client, aid).json()
    assert doc["summary"]["samples"] == 0
    assert "in:path" in doc["unavailable"][0]["reasons"][0]


# ============================================================== 必填值 / 待定
def test_required_octet_stream_body_is_unavailable(client):
    op = {
        "requestBody": {"required": True,
                        "content": {"application/octet-stream": {}}},
        "responses": {"200": {"description": "ok"}}}
    aid = _analysis_from_docs(client, _spec({"/p": {"post": op}}))
    _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/nope", "response": {"status": 200}}])
    doc = _gaps(client, aid).json()
    assert "octet-stream" in doc["unavailable"][0]["reasons"][0]


def test_response_schema_with_unsupported_pattern_is_unavailable(client):
    schema = {"type": "string", "pattern": "^a$"}
    op = {"responses": {"200": _json_resp(schema=schema)}}
    aid = _analysis_from_docs(client, _spec({"/p": {"get": op}}))
    _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/nope", "response": {"status": 200}}])
    doc = _gaps(client, aid).json()
    assert doc["summary"]["samples"] == 0
    assert "pattern" in doc["unavailable"][0]["reasons"][0]


def test_required_query_param_value_is_constructed(client):
    op = {
        "parameters": [{"name": "q", "in": "query", "required": True,
                        "schema": {"type": "string", "enum": ["a", "b"]}}],
        "responses": {"200": {"description": "ok"},
                      "2XX": _json_resp("r")}}
    aid = _analysis_from_docs(client, _spec({"/p": {"get": op}}))
    _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/p", "query": {"q": "a"},
         "response": {"status": 200}}])
    doc = _gaps(client, aid).json()
    s = doc["samples"][0]
    assert s["sample"]["query"]["q"] in ("a", "b")
    assert s["concrete_status"] == 201


def test_required_header_param_case_insensitive(client):
    op = {
        "parameters": [{"name": "X-Token", "in": "header",
                        "required": True,
                        "schema": {"type": "string", "minLength": 3}}],
        "responses": {"200": {"description": "ok"}}}
    aid = _analysis_from_docs(client, _spec({"/p": {"get": op}}))
    _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/nope", "response": {"status": 200}}])
    doc = _gaps(client, aid).json()
    s = doc["samples"][0]
    assert s["sample"]["headers"]["X-Token"]
    rd = _upload(client, aid, "baseline", [s["sample"]]).json()
    assert rd["rows"][0]["outcome"] == "covered"


def test_response_oneof_selects_definitely_passing_branch(client):
    # 根 oneOf：第一支要求必填 a（{} 确定失败）、第二支 {} 确定通过
    # → {} 恰好一支确定通过，可生成；不会输出归错格的样本
    schema = {"oneOf": [
        {"type": "object", "required": ["a"],
         "properties": {"a": {"type": "integer"}}},
        {"type": "object", "properties": {"a": {"type": "integer"}}}]}
    op = {"responses": {"200": _json_resp(schema=schema)}}
    aid = _analysis_from_docs(client, _spec({"/p": {"get": op}}))
    _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/nope", "response": {"status": 200}}])
    doc = _gaps(client, aid).json()
    assert doc["summary"]["samples"] == 1
    s = doc["samples"][0]
    assert s["sample"]["response"]["body"] == {}
    rd = _upload(client, aid, "baseline", [s["sample"]]).json()
    assert rd["rows"][0]["outcome"] == "covered"


# ============================================================== 无缺口 / 前置条件
def test_no_gaps_returns_empty_ndjson(client):
    op = {"responses": {"200": _json_resp()}}
    aid = _analysis_from_docs(client, _spec({"/p": {"get": op}}))
    _upload(client, aid, "baseline", [
        {"method": "GET", "path": "/p",
         "response": {"status": 200, "media_type": "application/json",
                      "body": {}}}])
    r = _gaps(client, aid)
    doc = r.json()
    assert doc["summary"] == {"gaps_total": 0, "samples": 0,
                              "unavailable": 0}
    assert doc["ndjson"] == ""
    d = client.get(
        f"/api/analyses/{aid}/coverage/baseline/gap-samples/ndjson")
    assert d.status_code == 200
    assert d.content == b""
    assert d.headers["content-type"].startswith("application/x-ndjson")
    assert d.headers["x-gap-samples-count"] == "0"
    assert "coverage-gap-samples" in d.headers["content-disposition"]


def test_missing_coverage_returns_404(client):
    aid = _analysis_from_docs(
        client, _spec({"/p": {"get": {"responses": {
            "200": {"description": "o"}}}}}))
    r = _gaps(client, aid)
    assert r.status_code == 404
    assert "覆盖" in r.json()["detail"]
    d = client.get(
        f"/api/analyses/{aid}/coverage/baseline/gap-samples/ndjson")
    assert d.status_code == 404


def test_unknown_side_rejected(client, analysis_id):
    r = client.get(
        f"/api/analyses/{analysis_id}/coverage/else/gap-samples")
    assert r.status_code == 400


def test_unknown_analysis_returns_404(client):
    r = client.get(
        "/api/analyses/deadbeefdeadbeef/coverage/baseline/gap-samples")
    assert r.status_code == 404


def test_gap_samples_do_not_mutate_coverage(client, analysis_id):
    cov_before = client.get(
        f"/api/analyses/{analysis_id}/coverage").json()
    _gaps(client, analysis_id).json()
    cov_after = client.get(
        f"/api/analyses/{analysis_id}/coverage").json()
    assert cov_before == cov_after


def test_ndjson_download_matches_document_and_has_fingerprint(
        client, analysis_id):
    rows = [
        {"method": "GET", "path": "/pets",
         "query": {"status": "available"}, "request": {},
         "response": {"status": 200, "media_type": "application/json",
                      "body": [{"id": 1, "name": "Rex",
                                "status": "available"}]}},
    ]
    _upload(client, analysis_id, "candidate", rows)
    doc = _gaps(client, analysis_id, "candidate").json()
    d = client.get(
        f"/api/analyses/{analysis_id}/coverage/candidate/gap-samples/ndjson")
    assert d.status_code == 200
    assert d.text == doc["ndjson"]
    assert int(d.headers["x-gap-samples-count"]) == \
        doc["summary"]["samples"]
    assert d.headers["x-gap-samples-fingerprint"] == \
        hashlib.sha256(doc["ndjson"].encode()).hexdigest()
    _upload(client, analysis_id, "candidate", rows)
