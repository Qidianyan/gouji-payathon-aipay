"""4+1 盲排评审：4 张参考 + 1 张我们的截图，打乱、去标识，交给全新上下文的视觉模型（本机代理）。
用法：python3 scripts/critique_4p1.py <ours.png> <ref1> <ref2> <ref3> <ref4> --seed N --out result.json
只输出排序与评语；不打印任何 key。"""
import argparse, base64, io, json, random, re, sys, urllib.request
from pathlib import Path
from PIL import Image

ENV = Path("/workspace/competitions/shared/llm_api.env")


def env():
    d = {}
    for line in ENV.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1); d[k.strip()] = v.strip()
    return d


def prep(p):
    im = Image.open(p).convert("RGB")
    w, h = im.size
    im = im.crop((0, 0, w, min(h, int(w * 900 / 1440))))
    im = im.resize((1024, int(im.size[1] * 1024 / im.size[0])))
    b = io.BytesIO(); im.save(b, "JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()


PROMPT = """你是资深产品设计评审。下面 5 张网页截图分别标为 A、B、C、D、E（顺序已打乱，来源不告诉你）。
只根据画面判断“精致度与品味”（排版、层级、留白、对齐、配色克制、细节完成度、是否有 AI 套路感）。
请只输出 JSON：{"ranking":["A",...从好到差],"scores":{"A":0-10,...},"gaps":{"A":["最大差距1","2","3"],...}}"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ours"); ap.add_argument("refs", nargs=4)
    ap.add_argument("--seed", type=int, default=1); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    items = [("ours", a.ours)] + [(Path(r).parent.name or Path(r).stem, r) for r in a.refs]
    random.Random(a.seed).shuffle(items)
    labels = "ABCDE"
    content = [{"type": "text", "text": PROMPT}]
    for lab, (_, p) in zip(labels, items):
        content += [{"type": "text", "text": f"截图 {lab}："}, {"type": "image_url", "image_url": {"url": prep(p)}}]
    e = env()
    body = {"model": e.get("LLM_MODEL", "deepseek-v4.1-flash"), "messages": [{"role": "user", "content": content}],
            "temperature": 0, "max_tokens": 1500}
    req = urllib.request.Request(e["OPENAI_BASE_URL"].rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + e["OPENAI_API_KEY"]})
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.load(r)
    txt = d["choices"][0]["message"]["content"]
    res = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
    mapping = {lab: src for lab, (src, _) in zip(labels, items)}
    ours = next(l for l, s in mapping.items() if s == "ours")
    out = {"seed": a.seed, "mapping": mapping, "ours_label": ours, "ranking": res["ranking"],
           "ours_rank": res["ranking"].index(ours) + 1, "ours_score": res["scores"].get(ours),
           "scores": res["scores"], "gaps": res["gaps"], "usage": d.get("usage")}
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps({k: out[k] for k in ("ours_rank", "ours_score", "ranking", "mapping")}, ensure_ascii=False))
    print("ours gaps:", out["gaps"].get(ours))


if __name__ == "__main__":
    main()
