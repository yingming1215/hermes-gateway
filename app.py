import os, json, asyncio, logging, datetime, traceback, time, re
from pathlib import Path
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from pydantic import BaseModel
import aiofiles

# ================= 配置 =================
VAULT_PATH = Path(os.environ.get("VAULT_PATH", "./vault"))
RUNS_PATH = Path("./runs")
PROMPT_V1 = Path("./prompts")
PROMPT_V2 = Path("./prompts/v2")
API_KEY = os.environ.get("OPENAI_API_KEY")
BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
MODEL = os.environ.get("MODEL_NAME", "qwen-plus")

for p in [VAULT_PATH / "inbox", RUNS_PATH, PROMPT_V1, PROMPT_V2]:
    p.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("hermes")
client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)

app = FastAPI(title="Hermes Gateway v2.0", version="0.14.0")
app.mount("/ui", StaticFiles(directory="ui"), name="ui")

# ================= 工具 =================
def get_run_id() -> str: return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")

def load_prompt(name: str, v2: bool = False) -> str:
    base = PROMPT_V2 if v2 else PROMPT_V1
    path = base / f"{name}.md"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f: return f.read()
    return f"你是一个专业的 {name} 助手。请严格按要求输出 JSON。"

async def call_llm(sys_prompt: str, user_prompt: str, timeout: int = 45) -> str:
    resp = await asyncio.wait_for(client.chat.completions.create(model=MODEL, messages=[
        {"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}
    ], temperature=0.3, max_tokens=4000), timeout=timeout)
    return resp.choices[0].message.content.strip()

def parse_json_safe(raw: str) -> Dict:
    if not raw: return {}
    c = raw.strip()
    if c.startswith("```"): c = c.split("```",1)[1].strip()
    if c.lower().startswith("json"): c = c[4:].strip()
    if c.endswith("```"): c = c[:-3].strip()
    m = re.search(r'\{.*\}', c, re.DOTALL)
    t = m.group(0) if m else c
    try: return json.loads(t)
    except:
        try: return json.loads(t.replace('\n','\\n').replace('\r',''))
        except: return {}

# ================= v2 路由 =================
@app.post("/api/v1/v2/weekly")
async def weekly_scripts(request: Request):
    run_id = get_run_id()
    body = await request.json()
    topics = body.get("topics", [])
    if len(topics) != 5: return JSONResponse({"error": "需提交5个主题（4诗词+1文化）"}, status_code=400)
    
    prompt = load_prompt("v2_weekly_planner", v2=True)
    llm_out = await call_llm(prompt, json.dumps(topics, ensure_ascii=False))
    data = parse_json_safe(llm_out)
    log_path = RUNS_PATH / f"weekly_{run_id}.json"
    async with aiofiles.open(log_path, "w", encoding="utf-8") as f: await f.write(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    return JSONResponse({"run_id": run_id, "status": "success", "scripts": data.get("weekly_scripts", []), "log": str(log_path)})

@app.post("/api/v1/v2/post_stream")
async def post_stream(request: Request):
    run_id = get_run_id()
    body = await request.json()
    raw_text = body.get("raw_text", "").strip()
    if len(raw_text) < 50: return JSONResponse({"error": "转写文本过短"}, status_code=400)
    
    results = {}
    # 1. 清洗
    res_clean = parse_json_safe(await call_llm(load_prompt("v2_transcriber", v2=True), raw_text))
    results["clean_text"] = res_clean
    
    # 2. T+0 分发
    highlights = res_clean.get("highlights", raw_text)
    results["t0_video"] = parse_json_safe(await call_llm(load_prompt("v2_t0_director", v2=True), json.dumps(highlights, ensure_ascii=False)))
    results["t0_visual"] = parse_json_safe(await call_llm(load_prompt("v2_t0_visual", v2=True), json.dumps(highlights, ensure_ascii=False)))
    
    # 3. T+1 分发
    results["t1_oa"] = parse_json_safe(await call_llm(load_prompt("v2_t1_oa", v2=True), json.dumps(res_clean, ensure_ascii=False)))
    results["t1_podcast"] = parse_json_safe(await call_llm(load_prompt("v2_t1_podcast", v2=True), json.dumps(res_clean, ensure_ascii=False)))
    
    # 4. 质控路由
    results["qc_checklist"] = parse_json_safe(await call_llm(load_prompt("v2_qc_dispatcher", v2=True), json.dumps(results, ensure_ascii=False, default=str)))
    
    log_path = RUNS_PATH / f"post_{run_id}.json"
    async with aiofiles.open(log_path, "w", encoding="utf-8") as f: await f.write(json.dumps({"run_id": run_id, "status": "success", "results": results}, ensure_ascii=False, indent=2, default=str))
    return JSONResponse({"run_id": run_id, "status": "success", "routed": {"t0": len(results["t0_video"].get("scripts",[])), "t1": 2}, "log": str(log_path)})

# ================= v1 兼容 & UI =================
@app.get("/")
async def root(): return {"service": "hermes-gateway", "version": "2.0.0", "status": "online"}

@app.post("/api/v1/trigger")
async def trigger_v1(request: Request):
    run_id = get_run_id(); start = time.monotonic(); body = await request.json()
    raw = body.get("text") or body.get("raw_text") or ""
    try:
        cur = parse_json_safe(await call_llm(load_prompt("soul_curator"), raw))
        fmt = parse_json_safe(await call_llm(load_prompt("soul_formatter"), json.dumps(cur, ensure_ascii=False)))
        return JSONResponse({"run_id": run_id, "status": "success", "curator_output": cur, "formatter_output": fmt, "elapsed_sec": round(time.monotonic()-start,2)})
    except Exception as e:
        return JSONResponse({"run_id": run_id, "status": "failed", "error": str(e), "elapsed_sec": round(time.monotonic()-start,2)})

@app.get("/api/v1/runs")
async def list_runs():
    runs = []
    if RUNS_PATH.exists():
        for f in sorted(RUNS_PATH.glob("*.json"), reverse=True)[:20]:
            async with aiofiles.open(f, "r", encoding="utf-8") as fh: runs.append(json.loads(await fh.read()))
    return JSONResponse({"runs": runs})

@app.get("/ui")
async def serve_ui(): return FileResponse(Path("ui/index.html")) if Path("ui/index.html").exists() else HTMLResponse("<h1>UI 未部署</h1>")
