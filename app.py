import os, json, asyncio, logging, datetime, traceback, time, re
from pathlib import Path
from typing import Optional, Dict, Any
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
import aiofiles

# ================= 配置 =================
VAULT_PATH = Path(os.environ.get("VAULT_PATH", "./vault"))
RUNS_PATH = Path("./runs")
PROMPT_DIR = Path("./prompts")
API_KEY = os.environ.get("OPENAI_API_KEY")
BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
MODEL = os.environ.get("MODEL_NAME", "qwen-plus")

# 自动创建目录
for p in [VAULT_PATH / "inbox", RUNS_PATH, PROMPT_DIR]:
    p.mkdir(parents=True, exist_ok=True)

# 日志配置
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("hermes")

# OpenAI 客户端（异步）
client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)

# FastAPI 实例
app = FastAPI(title="Hermes Gateway v1.0", version="0.13.0")
app.mount("/ui", StaticFiles(directory="ui"), name="ui")

# ================= 数据契约 =================
class RunResult(BaseModel):
    run_id: str
    status: str
    curator_output: Optional[Dict] = None
    formatter_output: Optional[Dict] = None
    files_exported: list = []
    elapsed_sec: float = 0.0
    error: Optional[str] = None

# ================= 核心工具 =================
def get_run_id() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")

def load_prompt(name: str) -> str:
    path = PROMPT_DIR / f"soul_{name}.md"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return f"你是一个专业的 {name} 助手。请严格按要求输出 JSON。"

async def call_llm(system_prompt: str, user_prompt: str, timeout: int = 45) -> str:
    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.3,
            max_tokens=4000
        ),
        timeout=timeout
    )
    return response.choices[0].message.content.strip()

def parse_json_safe(raw: str) -> Dict:
    if not raw: return {}
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```", 1)
        cleaned = parts[1].strip() if len(parts) > 1 else cleaned
        if cleaned.lower().startswith("json"): cleaned = cleaned[4:].strip()
        if cleaned.endswith("```"): cleaned = cleaned[:-3].strip()
    
    match = re.search(r'\{.*\}', cleaned, re.DOTALL)
    target = match.group(0) if match else cleaned
    
    try:
        return json.loads(target)
    except json.JSONDecodeError:
        try:
            target = target.replace('\n', '\\n').replace('\r', '')
            target = re.sub(r',\s*([}\]])', r'\1', target)
            return json.loads(target)
        except Exception as e:
            logger.warning(f"JSON解析失败: {str(e)[:50]}")
            return {}

# ================= Agent Worker =================
async def worker_curator(raw_text: str) -> Dict:
    prompt = load_prompt("curator")
    return parse_json_safe(await call_llm(prompt, raw_text))

async def worker_formatter(data: Dict) -> Dict:
    prompt = load_prompt("formatter")
    return parse_json_safe(await call_llm(prompt, json.dumps(data, ensure_ascii=False, indent=2)))

async def worker_exporter(run_id: str, output: Dict) -> list:
    exported = []
    inbox = VAULT_PATH / "inbox"
    
    if "douyin_script" in output:
        path = inbox / f"{run_id}_douyin.md"
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(f"# 抖音口播稿\n\n{output['douyin_script']}\n")
        exported.append(str(path))
        
    if "oa_long_article" in output:
        path = inbox / f"{run_id}_oa_long.md"
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(f"# 公众号长文\n\n{output['oa_long_article']}\n")
        exported.append(str(path))
        
    if "video_short" in output:
        path = inbox / f"{run_id}_video_short.md"
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(f"# 视频号分镜表\n\n{json.dumps(output['video_short'], ensure_ascii=False, indent=2)}\n")
        exported.append(str(path))
    return exported

# ================= 路由 =================
@app.get("/")
async def root():
    return {"service": "hermes-gateway", "version": "0.13.0", "status": "online"}

@app.post("/api/v1/trigger")
async def trigger_pipeline(request: Request):
    run_id = get_run_id()
    run_log_path = RUNS_PATH / f"{run_id}.json"
    start = time.monotonic()
    result = RunResult(run_id=run_id, status="running")
    
    try:
        # 🔧 工业级请求解析：彻底兼容飞书变量注入导致的 JSON 破裂
        raw_bytes = await request.body()
        raw_str = raw_bytes.decode("utf-8", errors="ignore").strip()
        try:
            body = json.loads(raw_str)
        except json.JSONDecodeError:
            # 降级提取：兼容飞书未转义引号/换行符
            match = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_str)
            body = {"text": match.group(1).replace('\\"', '"').replace('\\n', '\n')} if match else {"text": raw_str}

        raw = body.get("text") or body.get("raw_text") or body.get("input_text") or raw_str
        raw_text = str(raw).strip()
        if len(raw_text) < 10:
            raise ValueError("输入文本过短或为空")
            
        logger.info(f"[{run_id}] 启动 Curator (输入长度: {len(raw_text)})")
        result.curator_output = await worker_curator(raw_text)
        
        logger.info(f"[{run_id}] 启动 Formatter")
        result.formatter_output = await worker_formatter(result.curator_output)
        
        logger.info(f"[{run_id}] 启动 Exporter")
        result.files_exported = await worker_exporter(run_id, result.formatter_output)
        result.status = "success"
        
    except Exception as e:
        result.status = "failed"
        result.error = f"{type(e).__name__}: {str(e)}"
        logger.error(f"[{run_id}] 流程失败: {traceback.format_exc()}")
    finally:
        result.elapsed_sec = round(time.monotonic() - start, 2)
        # 强容错落盘：序列化失败自动降级
        try:
            log_data = result.model_dump()
        except Exception:
            log_data = {"run_id": run_id, "status": result.status, "error": "序列化异常", "elapsed_sec": result.elapsed_sec}
        try:
            async with aiofiles.open(run_log_path, "w", encoding="utf-8") as f:
                await f.write(json.dumps(log_data, ensure_ascii=False, indent=2, default=str))
        except Exception as e:
            logger.error(f"[{run_id}] 落盘失败: {e}")

    return JSONResponse(result.model_dump())

@app.get("/api/v1/runs")
async def list_runs():
    runs = []
    if RUNS_PATH.exists():
        for f in sorted(RUNS_PATH.glob("*.json"), reverse=True)[:20]:
            async with aiofiles.open(f, "r", encoding="utf-8") as fh:
                data = await fh.read()
                runs.append(json.loads(data))
    return JSONResponse({"runs": runs})

@app.get("/ui")
async def serve_ui():
    path = Path("ui/index.html")
    return FileResponse(path) if path.exists() else HTMLResponse("<h1>UI 未部署</h1>")

@app.get("/warmup")
async def warmup():
    try:
        await client.models.list()
        return {"status": "warmed"}
    except:
        return {"status": "skip"}
