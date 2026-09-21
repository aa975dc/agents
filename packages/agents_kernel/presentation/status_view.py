"""状态视图分页与新鲜度（Z13 呈现半 + Z21 前半 + C14）。

分页只切 features 列表；计数/进度/下一步等汇总字段每页都带全量，翻页不丢上下文。
total_estimated 是当前视图的 features 总数（本地视图内是精确值；字段名保留估计语义，
未来接入索引后可为部分建成的索引估计）。

新鲜度三态（C14：用户能理解实时性）：
- live：本次请求现算，含从源码树现算的指纹（stale=false）。
- cached：同一请求作用域内复用先前计算结果（stale=true——不是本次现读）。
- store：快照超限降级（Z21），只有本地台账数据、指纹不可核验
  （fingerprint_status=unavailable，stale=true，绝不冒充最新）。
"""
from agents_kernel.process import now
from agents_kernel.services.request_cache import RequestCache
from agents_kernel.validation import CompanionError

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 100

_STATUS_VIEW_KEY = "status-view"


def _clamp_page(value, label, low, high=None):
    """分页参数必须是整数；越界按钳制处理（page 下限 1，page_size 截断到 100）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise CompanionError(label + "必须是整数")
    return min(max(value, low), high) if high is not None else max(value, low)


def paginate(features, page=1, page_size=DEFAULT_PAGE_SIZE):
    """把 features 列表切成一页；page_size 上限截断为 100，page 从 1 计（下限 1）。"""
    page = _clamp_page(page, "page", 1)
    page_size = _clamp_page(page_size, "page_size", 1, MAX_PAGE_SIZE)
    start = (page - 1) * page_size
    items = list(features[start:start + page_size])
    return {"total_estimated": len(features), "items": items, "page": page,
            "page_size": page_size, "has_more": start + len(items) < len(features)}


def build_status_page(project, page=1, page_size=DEFAULT_PAGE_SIZE, evidence_freshness=None):
    """从只读状态视图产出一页分页结果，附新鲜度标注。

    在调用方显式打开的请求作用域内，底层视图同进程只算一次：首页 live，
    同作用域内的后续页标 cached/stale=true；每次独立调用（各自新作用域）都是 live。

    evidence_freshness：可选 {feature_id: EvidenceStatus}（services.evidence_freshness
    的产出，ST07：只标注不落库）。提供时每个功能项附 evidence_status
    （current/stale/unknown），非 current 另附 stale_reason 摘要；功能项以副本标注，
    绝不改写底层视图（含缓存中的同一份）。不提供时不加任何字段（向后兼容）。
    """
    with RequestCache.with_scope():
        key = (_STATUS_VIEW_KEY, str(project.root))
        view = RequestCache.get(key, project.status)
        cached = RequestCache.hit(key)
        result = paginate(view.get("features", []), page=page, page_size=page_size)
        if evidence_freshness is not None:
            annotated = []
            for item in result["items"]:
                record = evidence_freshness.get(item.get("id"))
                if record is not None:
                    item = dict(item, evidence_status=record.status)
                    if record.status != "current":
                        item["evidence_stale_reason"] = record.reason()
                annotated.append(item)
            result["items"] = annotated
        unavailable = view.get("fingerprint_status") == "unavailable"
        source = "store" if unavailable else ("cached" if cached else "live")
        result.update({"generated_at": now(), "source": source, "stale": source != "live",
                       "title": view.get("title"), "revision": view.get("revision"),
                       "counts": view.get("counts"), "overall_percent": view.get("overall_percent"),
                       "next_step": view.get("next_step"),
                       "fingerprint_status": "unavailable" if unavailable else "available"})
        return result


def summarize_for_report(page):
    """有限报告视图（CV06 尾）：面向 board.html/报告的摘要，只给计数和行清单。

    每功能一行仅含 id/任务状态/证据状态；绝不携带证据原文（verification、
    acceptance 等全量内容会把报告灌成全表），原文以指针代替：按台账
    revision 在项目台账/黑板中定位。只汇总传入页内的功能行；
    证据状态未计算（调用方未传 evidence_freshness）时为 None。
    """
    rows = [{"id": item.get("id"), "status": item.get("status"),
             "evidence_status": item.get("evidence_status")}
            for item in page.get("items", [])]
    return {"title": page.get("title"), "generated_at": page.get("generated_at"),
            "total": page.get("total_estimated"), "counts": page.get("counts"),
            "overall_percent": page.get("overall_percent"), "rows": rows,
            "evidence_pointer": "证据原文不在本报告中；按项目台账 revision=%s 定位各功能的验收与发布记录"
                                % (page.get("revision"),)}
