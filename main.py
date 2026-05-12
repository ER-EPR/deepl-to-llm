import os
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import httpx

app = FastAPI()
security = HTTPBearer()

# 从环境变量读取配置
LLM_API_URL = os.getenv("LLM_API_URL", "http://host.docker.internal:8080/v1/chat/completions")
LLM_API_KEY = os.getenv("LLM_API_KEY", "sk-xxx")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5-7b")
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "") # 如果为空则不鉴权

async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if BRIDGE_TOKEN and credentials.credentials != BRIDGE_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return credentials.credentials

@app.post("/translate")
async def translate_to_llm(request: Request, token: str = Depends(verify_token) if BRIDGE_TOKEN else None):
    # 解析 Collabora 发来的 form-data 数据
    data = await request.form()
    text = data.get("text")
    target_lang = data.get("target_lang", "ZH")

    if not text:
        raise HTTPException(status_code=400, detail="Missing text parameter")

    # 构造发给大模型的 Prompt
    llm_payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": f"You are a professional translator. Translate the following text into {target_lang}. ONLY output the translated text, without any explanations, quotes, or markdown formatting."},
            {"role": "user", "content": text}
        ],
        "temperature": 0.1
    }
    
    headers = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(LLM_API_URL, json=llm_payload, headers=headers)
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"].strip()
            
            # 严格按照 DeepL 格式返回给 Collabora
            return {"translations": [{"detected_source_language": "EN", "text": result}]}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
def health_check():
    return {"status": "LLM to DeepL Bridge is running"}
