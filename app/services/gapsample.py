"""覆盖缺口补测样本（Coverage Gap Samples）生成。

在一份**已保存的调用覆盖结果**上，为每个**未覆盖响应格**综合一行符合现有
NDJSON 调用格式的补测样本：它请求的是该侧文档下的合法请求，且响应状态码 /
媒体类型 / 载荷会**确实落入目标格**。综合过程严格复用调用覆盖引擎自身的
归属与三态判定，而不是另写一套“看起来合法”的启发式：

* **状态码选择**遵循上传时的同一优先级——精确码抢占范围键、范围键抢占
  ``default``：精确格直接用该码；范围键格（``2XX`` 等）在同族 100 个码中
  挑选**未被精确码抢占**的最小具体码；``default`` 格在 100–599 中挑选既
  不是精确码、也不落入任何已声明范围键的具体码（优先常见错误码）。找不到
  可用具体码时不输出样本，逐格说明原因。
* **路径选择**必须让具体路径**唯一归属**目标模板：先精确路径、再唯一模板。
  当模板路径被其他端点的精确路径抢占、精确路径存在但不支持该方法、或具体
  路径同时命中多个模板时，会尝试其它满足路径参数约束的取值；所有取值都被
  抢占 / 歧义时不输出归错格的样本。
* **必填值构造**：只带必填的 query/header/cookie 参数（可选项按旧客户端
  惯例省略），必填请求体与响应载荷都由 schema 综合最小取值，并经覆盖引擎
  的三态 ``assess`` 确认**确定通过**——必填值无法可靠构造、schema 待定
  （未支持关键字 / 循环引用 / 不可解析节点）或没有可用状态码时，该格记入
  ``unavailable`` 并给出具体原因，绝不输出校验失败 / 无法判定的样本。

结果确定性：格按已保存覆盖文档的顺序排列，相同输入（文档包 + 覆盖结果）
永远产生字节一致的 NDJSON。只读——不修改原覆盖结论、迁移评估或发布裁决。
"""
from __future__ import annotations

import itertools
import json
import re
from typing import Any, Optional

from . import pointer as ptr
from .coverage import (
    COVERED,
    _Call,
    _coerce_scalar,
    _evaluate,
    _format_hits,
    _index_operations,
    _is_required,
    _is_json_mt,
    _norm_mt,
    _resolve_response_status,
    _schema_node,
)
from .endpointrequest import _http_text
from .instance import TRI_FAIL, TRI_PASS, TRI_PENDING, assess, minimal_instance
from .schemagraph import SchemaGraph, UNSET

SCHEMA_VERSION = "1.0"
KIND = "coverage_gap_samples"

# 组合搜索上限：路径变量取值笛卡尔积的最大尝试次数。
MAX_PATH_COMBOS = 256
MAX_CANDIDATE_VALUES = 24


class GapSampleError(ValueError):
    """覆盖结果 / 文档包不具备生成补测样本的前置条件。"""


# ============================================================ public API
def build_gap_samples(pkg, side: str, coverage_doc: dict) -> dict:
    """依据已保存覆盖结果中的缺口，逐格综合补测样本。

    返回文档含 ``samples``（每格一条已通过引擎自检的调用样本）、
    ``unavailable``（无法生成的格及原因）与可直接下载的 ``ndjson`` 文本。
    """
    if not isinstance(coverage_doc, dict) or \
            not isinstance(coverage_doc.get("operations"), list):
        raise GapSampleError("覆盖结果缺少 operations 矩阵，无法定位缺口")

    graph = SchemaGraph(pkg.files, pkg.entry)
    operations = _index_operations(graph)
    op_index = {(op.method.upper(), op.path): op for op in operations}
    reenter = _reenter(graph)

    samples: list[dict] = []
    unavailable: list[dict] = []

    for op_doc in coverage_doc["operations"]:
        method = op_doc.get("method")
        path = op_doc.get("path")
        op = op_index.get((method, path))
        gaps = op_doc.get("gaps") if isinstance(op_doc.get("gaps"), list) \
            else []
        if op is None:
            for gap in gaps:
                unavailable.append(_unavailable_doc(
                    method, path, gap,
                    "覆盖结果中的操作在当前文档侧不存在"
                    "（文档包与覆盖结果不匹配）"))
            continue
        for gap in gaps:
            cell = next((c for c in op.cells
                         if c.key == gap.get("key")), None)
            if cell is None:
                unavailable.append(_unavailable_doc(
                    method, path, gap,
                    f"覆盖结果中的缺口格 {gap.get('key')!r} 在当前文档侧"
                    "不存在（文档包与覆盖结果不匹配）"))
                continue
            result = _build_for_cell(graph, operations, op, cell, reenter)
            if result.get("sample") is not None:
                samples.append({
                    "operation": f"{op.method.upper()} {op.path}",
                    "method": op.method.upper(),
                    "path_template": op.path,
                    "cell": cell.key,
                    "declared_status": cell.status,
                    "concrete_status": result["concrete_status"],
                    "kind": cell.kind,
                    "media_type": cell.media_type,
                    "sample": result["sample"],
                })
            else:
                unavailable.append({
                    "operation": f"{op.method.upper()} {op.path}",
                    "method": op.method.upper(),
                    "path_template": op.path,
                    "cell": cell.key,
                    "declared_status": cell.status,
                    "kind": cell.kind,
                    "media_type": cell.media_type,
                    "reasons": result["reasons"],
                })

    ndjson = render_ndjson([s["sample"] for s in samples])
    cov_summary = coverage_doc.get("summary") \
        if isinstance(coverage_doc.get("summary"), dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "generated_by": "OpenAPI Contract Evolution Workbench",
        "analysis_side": side,
        "source_coverage": {
            "source_file": coverage_doc.get("source_file"),
            "cells_total": cov_summary.get("cells_total"),
            "cells_covered": cov_summary.get("cells_covered"),
            "cells_gap": cov_summary.get("cells_gap"),
        },
        "summary": {
            "gaps_total": len(samples) + len(unavailable),
            "samples": len(samples),
            "unavailable": len(unavailable),
        },
        "samples": samples,
        "unavailable": unavailable,
        "ndjson": ndjson,
    }


def render_ndjson(samples: list[dict]) -> str:
    """确定性 NDJSON：键排序、紧凑分隔，每行一个调用对象。"""
    return "".join(
        json.dumps(sample, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")) + "\n"
        for sample in samples)


def _unavailable_doc(method, path, gap, reason: str) -> dict:
    return {
        "operation": f"{method} {path}",
        "method": method,
        "path_template": path,
        "cell": gap.get("key") if isinstance(gap, dict) else None,
        "declared_status": gap.get("status") if isinstance(gap, dict) else None,
        "kind": gap.get("kind") if isinstance(gap, dict) else None,
        "media_type": gap.get("media_type") if isinstance(gap, dict) else None,
        "reasons": [reason],
    }


# ============================================================ per-cell build
def _build_for_cell(graph: SchemaGraph, operations, op, cell,
                    reenter) -> dict:
    """为单个缺口格构造样本；失败返回 ``{"reasons": [...]}``。"""
    # ---- 1) 选一个会精确归到本格的具体状态码
    concrete, reason = _concrete_status(cell.status, op.responses)
    if concrete is None:
        return {"reasons": [reason]}
    # 双保险：引擎的解析结果必须就是该格声明键
    resolved = _resolve_response_status(op, str(concrete))
    if resolved is None or resolved[0] != cell.status:
        return {"reasons": [
            f"状态码 {concrete} 不会归属到格 {cell.status}"
            "（精确码 / 范围键 / default 优先级冲突）"]}

    # ---- 2) 必填的非路径参数（query/header/cookie）
    fixed, preasons = _required_fixed_params(graph, op, reenter)
    if preasons:
        return {"reasons": preasons}

    # ---- 3) 必填请求体
    request_part, breasons = _request_part(graph, op, reenter)
    if breasons:
        return {"reasons": breasons}

    # ---- 4) 响应实体（无 content 的格不带 body 键）
    response_part, rreasons = _response_part(graph, op, cell, concrete,
                                             reenter)
    if rreasons:
        return {"reasons": rreasons}

    # ---- 5) 路径变量取值：具体路径必须唯一归属目标模板
    var_names = re.findall(r"\{([^}]+)\}", op.path)
    text_options: list[list[str]] = []
    for name in var_names:
        param, node = op.params.get(name, (None, None))
        if param is None or param.get("in") != "path":
            return {"reasons": [
                f"路径模板 {op.path} 的占位符 {{{name}}} 未声明为 in:path "
                "参数，无法构造能唯一归属的请求"]}
        texts, preason = _passing_path_texts(graph, name, node, reenter)
        if not texts:
            return {"reasons": [preason]}
        text_options.append(texts)

    tried: list[dict] = []
    combos = itertools.islice(itertools.product(*text_options),
                              MAX_PATH_COMBOS)
    for combo in combos:
        path_values = dict(zip(var_names, combo))
        concrete_path = op.path
        for name, text in path_values.items():
            concrete_path = concrete_path.replace("{" + name + "}", text)
        call = _Call(
            line=0, method=op.method, path=concrete_path,
            query=dict(fixed["query"]), headers=dict(fixed["header"]),
            cookies=dict(fixed["cookie"]),
            request=dict(request_part),
            response=dict(response_part))
        row = _evaluate(graph, operations, call)
        if row.get("outcome") == COVERED \
                and row.get("operation") == f"{op.method.upper()} {op.path}" \
                and row.get("cell") == cell.key:
            return {"concrete_status": concrete,
                    "sample": _serializable_call(call)}
        tried.append(row)

    return {"reasons": _failure_reasons(cell, concrete, tried)}


def _serializable_call(call: _Call) -> dict:
    """把内部 _Call 还原成上传 NDJSON 的调用对象（status 用整数）。"""
    response = dict(call.response)
    status = response.pop("status", None)
    try:
        status = int(status)
    except (TypeError, ValueError):
        pass
    return {
        "method": call.method.upper(),
        "path": call.path,
        "query": call.query,
        "headers": call.headers,
        "cookies": call.cookies,
        "request": call.request,
        "response": {**response, "status": status},
    }


def _failure_reasons(cell, concrete: int, tried: list[dict]) -> list[str]:
    """路径组合全部失败时，给出最能说明问题的逐格原因。

    优先引用“已归属到目标操作但仍未覆盖”的那一行（参数 / 请求体 / 响应
    载荷问题与换路径无关）；否则引用最后一次匹配结果（被精确路径 / 其它
    模板抢占或歧义）。
    """
    if not tried:
        return [f"找不到能归属到目标操作的具体路径，无法补测格 {cell.key}"]

    def routing_failure(row: dict) -> list[str]:
        reasons = list(row.get("reasons") or [])
        op_name = row.get("operation")
        target = None
        if op_name:
            target = f"（被操作 {op_name} 抢占）"
        if row.get("outcome") in ("unmatched", "ambiguous") and reasons:
            # 路径抢占 / 歧义时，匹配原因就是根因（后续状态码错误是连带的）
            return [f"具体路径无法唯一归属目标模板{target or ''}："
                    f"{reasons[0]}"]
        if target:
            # 已归属到别的操作：状态码/载荷错误是抢占的连带结果
            return [f"具体路径归属{target}而非目标模板，无法构造只命中"
                    "目标格的请求："
                    + ("；".join(reasons[:2]) if reasons else "操作被抢占")]
        prefix = {
            "unmatched": "具体路径未匹配到目标模板",
            "ambiguous": "具体路径命中多个模板、无法唯一归属",
            "validation_failed": "补测请求仍被判定校验失败",
            "undetermined": "补测请求的三态裁决仍为无法判定",
            "excluded_media": "补测请求只命中非 JSON 媒体",
        }.get(row.get("outcome"), "补测请求未覆盖目标格")
        detail = "；".join(reasons[:3]) if reasons else \
            f"未覆盖目标格 {cell.key}"
        return [f"{prefix}（尝试状态码 {concrete}）: {detail}"]

    matched = [r for r in tried if r.get("operation")]
    if matched:
        return routing_failure(matched[-1])
    # 所有取值都被精确路径 / 其它模板抢占：直接报告路由原因
    return routing_failure(tried[-1])


# ============================================================ status codes
_DEFAULT_STATUS_PREFERENCE = (
    404, 500, 400, 403, 409, 422, 503, 502, 401, 405,
    429, 418, 408, 410, 501, 504, 406, 402, 412, 416,
)


def _concrete_status(declared_key: str,
                     responses: dict) -> tuple[Optional[int], Optional[str]]:
    """按 精确 → 范围 → default 的抢占关系选具体状态码。"""
    declared = set(responses)
    if declared_key.isdigit():
        return int(declared_key), None

    m = re.fullmatch(r"([1-5])XX", declared_key, re.IGNORECASE)
    if m:
        family = m.group(1)
        # 范围格：同族码只要没被精确码抢占就会归到该范围键（同族范围键
        # 只有本格自己，不算抢占）。
        for n in range(int(f"{family}00"), int(f"{family}99") + 1):
            if str(n) not in declared:
                return n, None
        return None, (
            f"范围键 {declared_key} 下 100 个具体状态码均已被精确响应"
            "声明抢占，无法选码")

    if declared_key == "default":
        def free(code: int) -> bool:
            text = str(code)
            return text not in declared \
                and f"{text[0]}XX" not in declared
        for code in _DEFAULT_STATUS_PREFERENCE:
            if free(code):
                return code, None
        for code in range(100, 600):
            if free(code):
                return code, None
        return None, (
            "100–599 中没有不被精确码或范围键抢占的具体状态码，"
            "无法为 default 格选码")

    return None, f"无法识别的响应声明键 {declared_key!r}"


# ============================================================ parameters
_LOC_ZH = {"query": "查询", "header": "请求头", "cookie": "Cookie"}


def _required_fixed_params(graph: SchemaGraph, op, reenter):
    """必填 query/header/cookie 参数的固定取值（可选项一律省略）。"""
    out = {"query": {}, "header": {}, "cookie": {}}
    for name in sorted(op.params):
        param, node = op.params[name]
        location = param.get("in")
        if location not in out or not _is_required(param):
            continue
        value, why = _passing_param_value(node, reenter, graph.entry)
        if why is not None:
            return None, [
                f"必填{_LOC_ZH[location]}参数 {name!r} 无法构造可靠取值: "
                f"{why}"]
        out[location][name] = value
    return out, None


def _passing_path_texts(graph: SchemaGraph, name: str, node,
                        reenter) -> tuple[list[str], Optional[str]]:
    """路径变量候选：只保留以 HTTP 文本代入后**确定通过**的非空字符串。"""
    if node is None:
        return ["0", "1", "v1", "v2"], None
    if node.invalid:
        return [], f"路径参数 {name!r} 的模式无法判定（{node.invalid}）"
    if node.is_cycle:
        return [], f"路径参数 {name!r} 的模式为循环引用，无法可靠构造取值"
    texts: list[str] = []
    pending = fail_detail = None
    for value in _candidate_values(node):
        if not isinstance(value, (str, int, float)) or \
                isinstance(value, bool):
            continue
        text = _http_text(value)
        if not text or text in texts:
            continue
        state, errors, hits = _text_verdict(node, text, reenter)
        if state == TRI_PASS:
            texts.append(text)
        elif pending is None and state == TRI_PENDING:
            pending = f"取值能否通过无法判定: {_format_hits(hits, graph.entry)}"
        elif fail_detail is None:
            fail_detail = "不满足契约约束: " + "; ".join(errors[:2])
    if texts:
        return texts, None
    why = pending or fail_detail or "参数类型无法作为 HTTP 路径段传输"
    return [], f"路径参数 {name!r} 无法构造满足约束的取值: {why}"


def _passing_param_value(node, reenter, entry: Optional[str] = None
                         ) -> tuple[Any, Optional[str]]:
    """query/header/cookie 必填参数：优先原生 JSON 值，其次 HTTP 文本。"""
    if node is None:
        # 声明了参数但无 schema —— 无取值约束
        return "x", None
    if node.invalid:
        return None, f"模式无法判定（{node.invalid}）"
    if node.is_cycle:
        return None, "模式为循环引用"
    pending = fail_detail = None
    for value in _candidate_values(node):
        state, errors, hits = _verdict_like_coverage(node, value, reenter)
        if state == TRI_PASS:
            return value, None
        if state == TRI_PENDING and pending is None:
            pending = "取值能否通过无法判定: " \
                f"{_format_hits(hits, entry or '')}"
        elif state == TRI_FAIL and fail_detail is None:
            fail_detail = "不满足契约约束: " + "; ".join(errors[:2])
    return None, pending or fail_detail or "所有候选取值均不满足约束"


def _verdict_like_coverage(node, value: Any, reenter):
    """镜像覆盖引擎对非路径参数的校验：先原生值，文本失败再尝试 coerce。"""
    result = assess(node, value, reenter=reenter)
    if result.state == TRI_FAIL and isinstance(value, str):
        coerced, ok = _coerce_scalar(value, node)
        if ok:
            coerced_result = assess(node, coerced, reenter=reenter)
            # coerce 成功后无论结果如何都以代入值的裁决为准（与覆盖引擎一致）
            return coerced_result.state, coerced_result.errors, \
                coerced_result.hits
    return result.state, result.errors, result.hits


def _text_verdict(node, text: str, reenter):
    """路径段（必然是字符串）的三态裁决，含与上传一致的标量 coerce 回退。"""
    result = assess(node, text, reenter=reenter)
    if result.state == TRI_FAIL:
        coerced, ok = _coerce_scalar(text, node)
        if ok:
            coerced_result = assess(node, coerced, reenter=reenter)
            return coerced_result.state, coerced_result.errors, \
                coerced_result.hits
    return result.state, result.errors, result.hits


def _candidate_values(node) -> list:
    """确定性的小型候选值表：常量/枚举优先，其次最小实例与同类型备选。"""
    values: list = []

    def add(v):
        if v is None or isinstance(v, (dict, list)):
            return
        try:
            key = json.dumps(v, sort_keys=True, ensure_ascii=False,
                             default=str)
        except TypeError:
            return
        if any(json.dumps(x, sort_keys=True, ensure_ascii=False,
                          default=str) == key for x in values):
            return
        if len(values) < MAX_CANDIDATE_VALUES:
            values.append(v)

    if node.const is not UNSET and node.const is not None:
        add(node.const)
    for enum_value in (node.enum or [])[:MAX_CANDIDATE_VALUES]:
        add(enum_value)
    try:
        add(minimal_instance(node))
    except Exception:
        pass
    for branch in (node.one_of or node.any_of)[:8]:
        try:
            add(minimal_instance(branch))
        except Exception:
            pass

    types = set(node.type or [])
    if node.type is None:
        types.update(["string", "integer", "boolean"])
    if "boolean" in types:
        add(False)
        add(True)
    if "integer" in types or "number" in types:
        for n in (0, 1, 2, 3, 7, 42, -1):
            add(n)
        if node.minimum is not None:
            add(node.minimum)
            add(node.minimum + 1)
        if node.maximum is not None:
            add(node.maximum)
        if "number" in types:
            add(0.5)
    if "string" in types:
        for s in ("x", "a", "v1", "v2", "s1", "s2"):
            add(s)
        if node.min_length:
            add("x" * node.min_length)
            add("s" * (node.min_length + 1))
    return values


# ============================================================ bodies
def _json_media_choice(content: dict) -> Optional[str]:
    """在 content 中选一个 JSON 媒体类型：application/json 优先，其次 +json。"""
    normalized = {_norm_mt(k): k for k in content}
    if "application/json" in normalized:
        return "application/json"
    vendors = sorted(mt for mt in normalized if _is_json_mt(mt)
                     and mt != "application/json")
    return vendors[0] if vendors else None


def _request_part(graph: SchemaGraph, op, reenter):
    """构造 request 描述：必填请求体给最小 JSON，可选 / 未声明则无实体。"""
    site = op.body_site
    if site is None:
        return {}, None
    content = site[2].get("content")
    content = content if isinstance(content, dict) else {}
    required = bool(site[2].get("required"))
    if not required:
        # 可选请求体按旧客户端惯例省略（缺省 body 键即无实体）
        return {}, None
    if not content:
        return None, ["必填 requestBody 未声明 content，无法构造请求实体"]
    mt = _json_media_choice(content)
    if mt is None:
        non_json = sorted({_norm_mt(k) for k in content if _norm_mt(k)})
        return None, [
            "必填 requestBody 仅声明非 JSON 媒体类型 "
            f"{non_json}，无法构造会通过 JSON 覆盖校验的请求体"]
    media = next(v for k, v in content.items() if _norm_mt(k) == mt)
    if not isinstance(media, dict) or not isinstance(media.get("schema"),
                                                     dict):
        # 媒体条目没有 schema：引擎不做载荷校验，任意 JSON 实体都通过
        return {"media_type": mt, "body": {}}, None
    node = _schema_node(graph, site, mt)
    value, why = _passing_body_value(graph, node, "必填请求体", mt, reenter)
    if why is not None:
        return None, [why]
    return {"media_type": mt, "body": value}, None


def _response_part(graph: SchemaGraph, op, cell, concrete: int,
                   reenter):
    """构造 response 描述：无 content 格无实体键；content 格带最小 JSON。"""
    site = op.responses[cell.status]
    content = site[2].get("content")
    content = content if isinstance(content, dict) else {}
    base = {"status": str(concrete)}
    if cell.kind == "no_content":
        return base, None
    mt = cell.media_type
    if mt not in {_norm_mt(k) for k in content}:
        return None, [f"响应格 {cell.key} 的媒体类型 {mt} 不在声明的 content 中"]
    media = next(v for k, v in content.items() if _norm_mt(k) == mt)
    if not isinstance(media, dict) or not isinstance(media.get("schema"),
                                                     dict):
        return {**base, "media_type": mt, "body": {}}, None
    node = graph.build_at(
        site[0], ptr.join(site[1], "content", mt, "schema"))
    value, why = _passing_body_value(
        graph, node, f"响应 {cell.status}", mt, reenter)
    if why is not None:
        return None, [why]
    return {**base, "media_type": mt, "body": value}, None


def _passing_body_value(graph: SchemaGraph, node, where: str, mt: str,
                        reenter):
    """综合一个对该 schema **确定通过** 的最小 JSON 值。

    先取 ``minimal_instance``，再对根 ``oneOf`` / ``anyOf`` 逐支取最小
    实例——根组合可能出现“一支确定通过、另一支待定”，只有在根节点
    ``assess`` 判定确定通过时才采用该值。
    """
    if node.invalid:
        return None, f"{where} {mt} 的 schema 无法判定（{node.invalid}）"
    if node.is_cycle:
        return None, f"{where} {mt} 的 schema 为循环引用，三态裁决无法计覆盖"

    candidates: list = []
    seen: set[str] = set()

    def add(v):
        try:
            key = json.dumps(v, sort_keys=True, ensure_ascii=False,
                             default=str)
        except TypeError:
            return
        if key not in seen and len(candidates) < MAX_CANDIDATE_VALUES:
            seen.add(key)
            candidates.append(v)

    try:
        add(minimal_instance(node))
    except Exception:
        pass
    for branch in (node.one_of or node.any_of)[:8]:
        try:
            add(minimal_instance(branch))
        except Exception:
            pass
    if not candidates:
        add({})

    pending = fail_detail = None
    for value in candidates:
        result = assess(node, value, reenter=reenter)
        if result.state == TRI_PASS:
            return value, None
        if result.state == TRI_PENDING and pending is None:
            pending = (
                f"{where} {mt} 的最小载荷能否通过无法判定: "
                + _format_hits(result.hits, graph.entry))
        elif result.state == TRI_FAIL and fail_detail is None:
            fail_detail = (
                f"{where} {mt} 的最小载荷不满足 schema: "
                + "; ".join(result.errors[:3]))
    return None, pending or fail_detail \
        or f"{where} {mt} 无法构造确定通过的最小载荷"


def _reenter(graph: SchemaGraph):
    def _build(file: str, pointer: str):
        try:
            return graph.build_at(file, pointer)
        except (KeyError, IndexError, ValueError):
            return None
    return _build
