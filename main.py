import os
from fastapi import FastAPI, Request, HTTPException, Depends, Header
from typing import Optional
import httpx

app = FastAPI()

# 从环境变量读取配置
LLM_API_URL = os.getenv("LLM_API_URL", "https://example.com/v1/chat/completions")
LLM_API_KEY = os.getenv("LLM_API_KEY", "sk-xxx")
LLM_MODEL = os.getenv("LLM_MODEL", "prx.free")
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "<your-bridge-token>")

# 自定义鉴权逻辑：兼容 Bearer 和 DeepL-Auth-Key
async def verify_token(request: Request, authorization: Optional[str] = Header(None)):
    if not BRIDGE_TOKEN:
        return
    
    token_to_check = None
    
    # 1. 尝试从 Authorization Header 获取
    if authorization:
        if authorization.startswith("Bearer "):
            token_to_check = authorization.replace("Bearer ", "")
        elif authorization.startswith("DeepL-Auth-Key "):
            token_to_check = authorization.replace("DeepL-Auth-Key ", "")
        else:
            # 兼容某些客户端直接把 token 放在 Authorization 里的情况
            token_to_check = authorization

    # 2. 如果 Header 没有，尝试从 Form Data 获取 (DeepL 规范允许)
    if not token_to_check:
        form_data = await request.form()
        token_to_check = form_data.get("auth_key")

    if token_to_check != BRIDGE_TOKEN:
        print(f"Auth Failed: Received {token_to_check}") # 打印日志方便调试
        raise HTTPException(status_code=403, detail="Invalid API Key")

@app.post("/translate")
async def translate_to_llm(request: Request, _ = Depends(verify_token)):
    # 判断格式
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
    else:
        data = await request.form()

    text_data = data.get("text")
    target_lang = data.get("target_lang", "ZH")
    # 获取 Collabora 传来的标签处理参数
    tag_handling = request.query_params.get("tag_handling", "plain")

    if not text_data:
        raise HTTPException(status_code=400, detail="Missing text parameter")

    text = text_data[0] if isinstance(text_data, list) else text_data

    # 构造 Prompt：特别加入 HTML 标签处理指令
    system_prompt = f"You are a professional translator. Translate the text into {target_lang}."
    if tag_handling == "html":
        system_prompt += " The input text contains HTML tags. You MUST preserve all HTML tags and structure, only translate the text content inside them."
    
    system_prompt += " ONLY output the translated result, no explanations."

    llm_payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
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
            
            # 返回标准的 DeepL 响应格式
            return {
                "translations": [{
                    "detected_source_language": "EN", 
                    "text": result
                }]
            }
        except Exception as e:
            print(f"LLM Error: {str(e)}")
            raise HTTPException(status_code=500, detail="Internal Server Error")
