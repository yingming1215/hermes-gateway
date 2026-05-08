from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import requests, json, time, sqlite3, os, re

app = FastAPI(title="Hermes-Feishu Bridge")

HERMES_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
HERMES_MODEL = "qwen-plus"
API_KEY = "sk-28389f7e7ca749348fa2917fca6c25d4"

DB_PATH = "agent_state.db"
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute("""CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY, agent TEXT, input_hash TEXT, status TEXT, 
    output_preview TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""")
conn.commit()

class AgentRequest(BaseModel):
    agent: str
    input_text: str

def extract_json(text: str) -> str:
    """智能提取：精准截取 {} 区间，彻底剥离前言后语"""
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        return match.group(0)
    # 容错兜底：包装为合法JSON，防下游飞书解析崩溃
    return json.dumps({"error": "model_output_not_json", "raw_head": text[:100]}, ensure_ascii=False)

@app.post("/api/v1/agent")
def route_agent(req: AgentRequest):
    print(f"\n🔍 [请求] Agent: {req.agent} | Input: {req.input_text[:30]}...")
    if req.agent not in ["curator", "formatter", "qc"]:
        raise HTTPException(400, "Invalid agent type")
    
    soul_path = f"soul_{req.agent}.md"
    if not os.path.exists(soul_path):
        raise HTTPException(404, f"Missing soul file: {soul_path}")
        
    with open(soul_path, "r", encoding="utf-8") as f:
        system_prompt = f.read()
        
    # 动态追加绝对指令，覆盖模型默认闲聊倾向
    system_prompt += "\n\n【绝对指令】仅输出合法JSON对象，严禁包含任何问候、解释、Markdown标记或额外文本。"
        
    payload = {
        "model": HERMES_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": req.input_text}
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"}
    }
    
    start = time.time()
    try:
        resp = requests.post(HERMES_API_URL, json=payload, headers={"Authorization": f"Bearer {API_KEY}"}, timeout=45)
        
        if resp.status_code != 200:
            print(f"\n🚨 [DashScope拒收] Status: {resp.status_code} | Body: {resp.text[:200]}\n")
            raise HTTPException(502, f"DashScope Error: {resp.status_code}")
            
        raw = resp.json()["choices"][0]["message"]["content"]
        print(f"✅ [成功] 原始长度: {len(raw)} | 提取后: {extract_json(raw)[:50]}...\n")
        
        result = json.dumps(json.loads(extract_json(raw)), ensure_ascii=False)
        elapsed = round(time.time() - start, 2)
        conn.execute("INSERT INTO logs (agent, input_hash, status, output_preview) VALUES (?, ?, ?, ?)",
                     (req.agent, hash(req.input_text[:50]), "success", result[:100]))
        conn.commit()
        
        return {"status": "success", "agent": req.agent, "output": result, "elapsed_sec": elapsed}
    except HTTPException:
        raise
    except Exception as e:
        conn.execute("INSERT INTO logs (agent, input_hash, status, output_preview) VALUES (?, ?, ?, ?)",
                     (req.agent, hash(req.input_text[:50]), "error", str(e)[:100]))
        conn.commit()
        raise HTTPException(502, f"Gateway Exception: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000)
