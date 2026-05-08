from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import json
import time
import re
import os
from openai import OpenAI

app = FastAPI()
client = OpenAI(
    api_key=os.environ.get("OPENAI_API_KEY", "sk-your-key"),
    base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
)

def load_prompt(name):
    try:
        with open(f"soul_{name}.md", "r", encoding="utf-8") as f:
            return f.read()
    except:
        return "你是一个专业的诗词内容处理助手。"

def safe_parse_feishu(raw_text):
    """🛡️ 飞书专属容错解析器（彻底解决转义断裂/空值报错）"""
    if not raw_text: return {}
    cleaned = raw_text.strip().replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 1)[1].strip()
        if cleaned.lower().startswith("json"): cleaned = cleaned[4:].strip()
        if cleaned.endswith("```"): cleaned = cleaned[:-3].strip()
    match = re.search(r'\{.*\}', cleaned, re.DOTALL)
    try:
        return json.loads(match.group(0)) if match else json.loads(cleaned)
    except:
        return {}

@app.post("/api/v1/agent")
async def agent_endpoint(request: Request):
    start = time.time()
    try:
        payload = await request.json()
        agent = payload.get("agent", "").strip().lower()
        raw_input = payload.get("input_text", "")
        
        if agent == "formatter":
            # 🔑 核心修复：飞书传入的 JSON 字符串先安全清洗，再转回标准 JSON 供 LLM 读取
            parsed_dict = safe_parse_feishu(raw_input)
            llm_input = json.dumps(parsed_dict, ensure_ascii=False) if parsed_dict else raw_input
        else:
            llm_input = raw_input

        system_prompt = load_prompt(agent)
        response = client.chat.completions.create(
            model=os.environ.get("MODEL_NAME", "gpt-3.5-turbo"),
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": llm_input}],
            temperature=0.3
        )
        output_text = response.choices[0].message.content.strip()
        
        return JSONResponse({
            "status": "success",
            "agent": agent,
            "output": output_text,
            "elapsed_sec": round(time.time() - start, 2)
        })
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)
