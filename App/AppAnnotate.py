# -*- coding: utf-8 -*-
"""
App/AppAnnotate.py —— 标注功能域
=========================================================================
按业务能力拆分（阶段 8 重设计）：图表右键标注相关操作。

本模块收纳：
  - get_annotations（/api/annotations GET）
  - handle_annotation_action（/api/annotations POST，40 行校验逻辑下沉）

依赖方向：AppAnnotate.py → AppData（单向）
"""
# ═══════════════════════════════════════════════════════════════════════
# 标注
# ═══════════════════════════════════════════════════════════════════════

def get_annotations(code, freq):
    """获取标注数据（/api/annotations GET）"""
    from App.AppData import app_data
    return app_data.get_annotations_for(code, freq)


def handle_annotation_action(body):
    """标注增删改统一入口（/api/annotations POST，40 行校验逻辑下沉）

    body: {action, code, freq, date, text, y_offset, old_text}
    返回 (result_dict, status_code)，语义与原路由逐分支一致。
    """
    from App.AppData import app_data

    action = body.get("action", "")
    code = body.get("code", "")
    freq = body.get("freq", "d")
    date_str = body.get("date", "")
    text = body.get("text", "")
    y_offset = body.get("y_offset", 0)

    if not code:
        return {"error": "缺少code参数"}, 400

    if action == "add":
        if not date_str or not text:
            return {"error": "缺少date或text参数"}, 400
        success = app_data.add_annotation(code, freq, date_str, text, y_offset)
        return {"ok": success, "duplicate": not success}, 200
    elif action == "delete":
        if not date_str or not text:
            return {"error": "缺少date或text参数"}, 400
        success = app_data.delete_annotation(code, freq, date_str, text)
        return {"ok": success}, 200
    elif action == "delete_by_date":
        if not date_str:
            return {"error": "缺少date参数"}, 400
        success = app_data.delete_annotation_by_date(code, freq, date_str)
        return {"ok": success}, 200
    elif action == "delete_all":
        success = app_data.delete_all_annotations(code, freq)
        return {"ok": success}, 200
    elif action == "update":
        old_text = body.get("old_text", "")
        new_text = body.get("text", "")
        if not date_str or not old_text or not new_text:
            return {"error": "缺少date/old_text/text参数"}, 400
        app_data.delete_annotation(code, freq, date_str, old_text)
        success = app_data.add_annotation(code, freq, date_str, new_text, y_offset)
        return {"ok": success}, 200
    else:
        return {"error": f"未知action: {action}"}, 400
