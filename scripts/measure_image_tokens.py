"""实测票据图片在本机代理（DeepSeek 视觉）上的 token 用量。只打印 usage，不打印任何 key。
用法：python3 scripts/measure_image_tokens.py img1 [img2 ...] --sides 768,1024,1600 --out var/image_tokens.json"""
import argparse, base64, io, json, sys, time, urllib.request
from pathlib import Path
from PIL import Image
sys.path.insert(0, str(Path(__file__).parent))
from critique_4p1 import env  # noqa: E402

PROMPT = ("你是记账助手。识别这张票据，只输出 JSON："
          '{"票据类型":"","开票日期":"","销售方":"","购买方":"","金额":"","税额":"","价税合计":"","建议科目":""}，看不清的填 null。')


def data_url(p, side):
    im = Image.open(p).convert("RGB")
    if side:
        s = side / max(im.size); im = im.resize((round(im.size[0] * s), round(im.size[1] * s)))
    b = io.BytesIO(); im.save(b, "JPEG", quality=85)
    return im.size, "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()


def call(e, content):
    body = {"model": e.get("LLM_MODEL", "deepseek-v4.1-flash"), "messages": [{"role": "user", "content": content}],
            "temperature": 0, "max_tokens": 1200}
    req = urllib.request.Request(e["OPENAI_BASE_URL"].rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + e["OPENAI_API_KEY"]})
    t = time.time()
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.load(r)
    return d.get("usage"), d["choices"][0]["message"]["content"], round(time.time() - t, 1), d.get("model")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("imgs", nargs="+")
    ap.add_argument("--sides", default="768,1024,1600"); ap.add_argument("--out", required=True)
    a = ap.parse_args(); e = env(); rows = []
    base, _, _, model = call(e, [{"type": "text", "text": PROMPT}])
    rows.append({"image": None, "usage": base, "model": model})
    print("text-only baseline", base)
    for p in a.imgs:
        for side in [int(x) for x in a.sides.split(",")]:
            size, url = data_url(p, side)
            u, txt, sec, model = call(e, [{"type": "text", "text": PROMPT}, {"type": "image_url", "image_url": {"url": url}}])
            rows.append({"image": Path(p).name, "px": size, "usage": u, "seconds": sec, "model": model, "answer": txt})
            print(Path(p).name, size, u, f"{sec}s")
    Path(a.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
