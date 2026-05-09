import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

if not api_key:
    raise RuntimeError("找不到 OPENAI_API_KEY，請檢查 .env 是否放在專案根目錄。")

client = OpenAI(api_key=api_key)

response = client.responses.create(
    model=model,
    input="請用一句話回答：你可以協助分析 LINE 群組對話嗎？"
)

print(response.output_text)