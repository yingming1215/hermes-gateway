 # 🔧 安全解析层（防飞书转义断裂）
    import json, re
    raw_input = payload.get("input_text", "")
    
    # 1. 剥离飞书可能附加的多余引号或转义符
    if raw_input.startswith('"') and raw_input.endswith('"'):
        raw_input = json.loads(raw_input)
    elif raw_input.startswith("{"):
        # 尝试直接解析，若失败则清理反斜杠后重试
        try:
            parsed = json.loads(raw_input)
        except json.JSONDecodeError:
            raw_input = raw_input.replace('\\"', '"').replace('\n', '')
            parsed = json.loads(raw_input)
    else:
        parsed = {}

    # 2. 提取核心字段（兼容空值容错）
    data = parsed if isinstance(parsed, dict) else {}
    required_keys = ["title", "theme", "emotional_hook", "source_citation", "tone_note", "clean_markdown"]
    missing = [k for k in required_keys if not data.get(k)]
    
    if missing:
        return JSONResponse({"error": f"输入数据不完整：{', '.join(missing)} 为空。请检查上游节点输出。"}, status_code=400)
