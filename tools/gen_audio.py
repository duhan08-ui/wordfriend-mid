"""(예비용) PC에서 직접 MP3 일괄 생성하는 스크립트.
평소엔 웹 관리 페이지가 자동으로 생성하므로 쓸 일 없음.
사용: pip install edge-tts  →  python tools/gen_audio.py
결과: audio/*.mp3 생성 → git push 하면 반영
"""
import asyncio, csv, os, re, sys

VOICE = sys.argv[1] if len(sys.argv) > 1 else "en-US-JennyNeural"

def slug(en):
    return re.sub(r"[^a-z0-9]+", "_", en.lower()).strip("_")

async def main():
    import edge_tts
    os.makedirs("audio", exist_ok=True)
    with open("words.csv", encoding="utf-8") as f:
        rows = [r for r in csv.reader(f) if r and r[0].strip() not in ("", "en")]
    # 칭찬 음성도 앱이 받아 가므로 함께 생성 (streamlit_app.py 의 PRAISES 와 같게)
    praises = ["Good job!", "Excellent!", "Great!", "Perfect!", "Wonderful!"]
    for en in [r[0].strip() for r in rows] + praises:
        path = f"audio/{slug(en)}.mp3"
        if os.path.exists(path):
            continue
        await edge_tts.Communicate(en, VOICE).save(path)
        print("생성:", path)

asyncio.run(main())
