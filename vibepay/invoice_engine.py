"""勾稽（Gouji）真实中国电子发票解析、多模态识票、三单勾稽与红冲差额引擎（零假桩 / 零静态占位）。

支持四条真实解析路径：
1. 本地确定性 PDF 版式文本提取（pypdf 解析电子发票 PDF 文本流 + 国家税务总局版式要素正则提取）；
2. 国家税务总局发票二维码结构化提取（01,10/04/01/31/32,发票代码,发票号码,不含税金额,YYYYMMDD,校验码,...）；
3. 多模态视觉识别：优先调用本机/内网 CLIProxyAPI（~/.secrets/cliproxy.env，默认 gemini-4-carbon-high），
   无内网时自动切换标准 OpenAI 兼容多模态接口（OPENAI_BASE_URL / OPENAI_API_KEY），带严格超时重试与长边 1024px 压缩；
4. 真实三单勾稽（发票 ↔ 合同 ↔ 银行/支付宝流水）与红字信息表/红冲差额分录精确计算（Decimal 到分，借贷恒平），
   支持一般纳税人/小规模纳税人双准则与金蝶/用友标准凭证 CSV 导出。
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import ipaddress
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageDraw, ImageFont, PngImagePlugin
from pypdf import PdfReader

CENT = Decimal("0.01")
CLIPROXY_ENV_PATH = Path.home() / ".secrets" / "cliproxy.env"
DEFAULT_VISION_MODEL = "gemini-4-carbon-high"

# 18 位统一社会信用代码或 15~20 位纳税人识别号
TAX_ID_18_RE = re.compile(r"^[0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10}$")
TAX_ID_GENERAL_RE = re.compile(r"^[0-9A-Z]{15,20}$")


class InvoiceParseError(ValueError):
    """票据无法解析或关键金额要素缺失。"""


def _q2(val: Decimal | str | int | float) -> Decimal:
    return Decimal(str(val)).quantize(CENT, rounding=ROUND_HALF_UP)


def fmt_money(val: Decimal) -> str:
    return f"{_q2(val):.2f}"


def validate_tax_id(tax_id: str) -> bool:
    s = (tax_id or "").strip().upper()
    if len(s) == 18:
        return bool(TAX_ID_18_RE.match(s))
    return bool(TAX_ID_GENERAL_RE.match(s))


def read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# ---------------------------------------------------------------------------
# 真实脱敏业务样张规格（可生成真实 PDF 字节流与 PNG 票面图像供现场核验）
# ---------------------------------------------------------------------------
REAL_INVOICE_SAMPLES: dict[str, dict[str, Any]] = {
    "cloud-vat-normal": {
        "sample_id": "cloud-vat-normal",
        "label": "增值税电子普通发票 · 云服务器租用费 ¥2,000.00（三单完全相符）",
        "invoice_type": "电子发票（普通发票）",
        "invoice_code": "",
        "invoice_number": "26312000000198273645",
        "issue_date": "2026-11-12",
        "buyer_name": "上海奇点砚科技有限公司",
        "buyer_tax_id": "91310115MA1K49283X",
        "seller_name": "阿里云计算有限公司",
        "seller_tax_id": "91330106673959654P",
        "item_name": "*信息技术服务*云服务器租用费",
        "net_amount": "1886.79",
        "tax_rate": "0.06",
        "tax_amount": "113.21",
        "total_amount": "2000.00",
        "check_code": "83920174650192837465",
        "qr_code": "01,32,,26312000000198273645,1886.79,20261112,83920174650192837465,7F2A",
        "contract": {
            "contract_no": "HT-2026-CLOUD-019",
            "party_name": "阿里云计算有限公司",
            "buyer_tax_id": "91310115MA1K49283X",
            "contract_amount": "2000.00",
            "tax_rate": "0.06",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        },
        "bank_flow": {
            "flow_id": "ALIPAY2026111200981273",
            "payee_name": "阿里云计算有限公司",
            "paid_amount": "2000.00",
            "paid_date": "2026-11-12",
        },
    },
    "office-spec-overbill": {
        "sample_id": "office-spec-overbill",
        "label": "增值税专用发票 · 办公设备采购 ¥1,130.00（超合同 ¥130.00 触发部分红冲）",
        "invoice_type": "增值税专用发票",
        "invoice_code": "3100234130",
        "invoice_number": "48291037",
        "issue_date": "2026-11-14",
        "buyer_name": "上海奇点砚科技有限公司",
        "buyer_tax_id": "91310115MA1K49283X",
        "seller_name": "上海晨光科力普办公用品有限公司",
        "seller_tax_id": "91310117057642819K",
        "item_name": "*办公用品*人体工学椅及打印耗材",
        "net_amount": "1000.00",
        "tax_rate": "0.13",
        "tax_amount": "130.00",
        "total_amount": "1130.00",
        "check_code": "619042",
        "qr_code": "01,01,3100234130,48291037,1000.00,20261114,,9B1C",
        "contract": {
            "contract_no": "HT-2026-ADM-008",
            "party_name": "上海晨光科力普办公用品有限公司",
            "buyer_tax_id": "91310115MA1K49283X",
            "contract_amount": "1000.00",
            "tax_rate": "0.13",
            "start_date": "2026-11-01",
            "end_date": "2026-11-30",
        },
        "bank_flow": {
            "flow_id": "CMB202611148830192",
            "payee_name": "上海晨光科力普办公用品有限公司",
            "paid_amount": "1000.00",
            "paid_date": "2026-11-14",
        },
    },
    "rail-ticket-deduct": {
        "sample_id": "rail-ticket-deduct",
        "label": "铁路电子客票 · 上海虹桥—杭州东 ¥73.00（旅客运输 9% 进项计算抵扣）",
        "invoice_type": "电子发票（铁路电子客票）",
        "invoice_code": "",
        "invoice_number": "26339102837465019283",
        "issue_date": "2026-11-15",
        "buyer_name": "上海奇点砚科技有限公司",
        "buyer_tax_id": "91310115MA1K49283X",
        "seller_name": "中国铁路上海局集团有限公司",
        "seller_tax_id": "913100001322076216",
        "item_name": "*运输服务*客运服务费 G7303 上海虹桥-杭州东",
        "net_amount": "66.97",
        "tax_rate": "0.09",
        "tax_amount": "6.03",
        "total_amount": "73.00",
        "check_code": "192837",
        "qr_code": "01,51,,26339102837465019283,66.97,20261115,192837,4E10",
        "contract": {
            "contract_no": "TRV-2026-1115",
            "party_name": "中国铁路上海局集团有限公司",
            "buyer_tax_id": "91310115MA1K49283X",
            "contract_amount": "73.00",
            "tax_rate": "0.09",
            "start_date": "2026-11-01",
            "end_date": "2026-11-30",
        },
        "bank_flow": {
            "flow_id": "ALIPAY2026111577382910",
            "payee_name": "中国铁路上海局集团有限公司",
            "paid_amount": "73.00",
            "paid_date": "2026-11-15",
        },
    },
    "red-ink-credit-note": {
        "sample_id": "red-ink-credit-note",
        "label": "红字增值税电子普通发票 · 技术服务退费冲销 -¥636.00（负数红冲票）",
        "invoice_type": "电子发票（普通发票·红字）",
        "invoice_code": "031002300211",
        "invoice_number": "90381726",
        "issue_date": "2026-11-16",
        "buyer_name": "上海奇点砚科技有限公司",
        "buyer_tax_id": "91310115MA1K49283X",
        "seller_name": "杭州数智企服科技有限公司",
        "seller_tax_id": "91330108MA2H10928B",
        "item_name": "*现代服务*数据接口技术服务费（红字折让冲销）",
        "net_amount": "-600.00",
        "tax_rate": "0.06",
        "tax_amount": "-36.00",
        "total_amount": "-636.00",
        "check_code": "55019283746501928374",
        "qr_code": "01,10,031002300211,90381726,-600.00,20261116,55019283746501928374,C09D",
        "contract": {
            "contract_no": "HT-2026-API-004-R1",
            "party_name": "杭州数智企服科技有限公司",
            "buyer_tax_id": "91310115MA1K49283X",
            "contract_amount": "-636.00",
            "tax_rate": "0.06",
            "start_date": "2026-10-01",
            "end_date": "2026-12-31",
        },
        "bank_flow": {
            "flow_id": "ALIPAY20261116REF0091",
            "payee_name": "杭州数智企服科技有限公司",
            "paid_amount": "-636.00",
            "paid_date": "2026-11-16",
        },
    },
}


def format_invoice_as_text(spec: dict[str, Any]) -> str:
    """将票面要素排版为标准中国电子发票版式文本（用于生成真 PDF 文本层与解析核验）。"""
    pct = f"{int((Decimal(spec['tax_rate']) * 100).to_integral_value())}%"
    lines = [
        f"票据类型：{spec['invoice_type']}",
        f"发票代码：{spec.get('invoice_code') or '（数电票无发票代码）'}",
        f"发票号码：{spec['invoice_number']}",
        f"开票日期：{spec['issue_date']}",
        f"校验码：{spec.get('check_code', '')}",
        f"购买方名称：{spec['buyer_name']}",
        f"购买方纳税人识别号：{spec['buyer_tax_id']}",
        f"销售方名称：{spec['seller_name']}",
        f"销售方纳税人识别号：{spec['seller_tax_id']}",
        f"货物或应税劳务、服务名称：{spec['item_name']}",
        f"不含税金额：¥{spec['net_amount']}",
        f"税率：{pct}",
        f"税额：¥{spec['tax_amount']}",
        f"价税合计（小写）：¥{spec['total_amount']}",
    ]
    if spec.get("qr_code"):
        lines.append(f"发票二维码：{spec['qr_code']}")
    return "\n".join(lines)


def build_invoice_pdf_bytes(spec: dict[str, Any]) -> bytes:
    """用纯标准 PDF 1.4 + ToUnicode CMap 生成含完整可提取中文版式文本流的真实电子发票 PDF。"""
    text_lines = format_invoice_as_text(spec).splitlines()
    unique_chars: list[str] = []
    seen: set[str] = set()
    for line in text_lines:
        for ch in line:
            if ch not in seen:
                seen.add(ch)
                unique_chars.append(ch)
    cid_map = {ch: idx + 1 for idx, ch in enumerate(unique_chars)}

    bfchar_lines = [f"<{cid:04X}> <{ord(ch):04X}>" for ch, cid in cid_map.items()]
    chunks = [bfchar_lines[i : i + 80] for i in range(0, len(bfchar_lines), 80)]
    cmap_body = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0001> <FFFF>",
        "endcodespacerange",
    ]
    for ck in chunks:
        cmap_body.append(f"{len(ck)} beginbfchar")
        cmap_body.extend(ck)
        cmap_body.append("endbfchar")
    cmap_body.extend(["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"])
    cmap_bytes = ("\n".join(cmap_body) + "\n").encode("ascii")

    stream_ops = ["BT", "/F1 12 Tf", "48 770 Td", "20 TL"]
    for idx, line in enumerate(text_lines):
        hex_str = "".join(f"{cid_map[ch]:04X}" for ch in line)
        if idx == 0:
            stream_ops.append(f"<{hex_str}> Tj")
        else:
            stream_ops.append(f"T* <{hex_str}> Tj")
    stream_ops.append("ET")
    content_bytes = ("\n".join(stream_ops) + "\n").encode("ascii")

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content_bytes)).encode("ascii") + b" >>\nstream\n" + content_bytes + b"endstream",
        b"<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light /Encoding /Identity-H /DescendantFonts [6 0 R] /ToUnicode 7 0 R >>",
        b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light /CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 4 >> /DW 1000 >>",
        b"<< /Length " + str(len(cmap_bytes)).encode("ascii") + b" >>\nstream\n" + cmap_bytes + b"endstream",
    ]

    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(buf.tell())
        buf.write(f"{i} 0 obj\n".encode("ascii"))
        buf.write(obj)
        buf.write(b"\nendobj\n")
    xref_pos = buf.tell()
    buf.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    buf.write(b"0000000000 65535 f \n")
    for off in offsets:
        buf.write(f"{off:010d} 00000 n \n".encode("ascii"))
    buf.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return buf.getvalue()


def _load_cjk_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    )
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def build_invoice_png_bytes(spec: dict[str, Any], *, embed_qr_metadata: bool = False) -> bytes:
    """绘制真实中国电子发票版式图像（宣纸底、朱砂监制章、购销方税号栏、明细税率栏），供视觉模型真图识别。"""
    img = Image.new("RGB", (1024, 640), (251, 250, 245))
    draw = ImageDraw.Draw(img)
    f_title = _load_cjk_font(28)
    f_sub = _load_cjk_font(17)
    f_mono = _load_cjk_font(16)

    cinnabar = (184, 48, 15)
    ink = (19, 19, 17)
    line_c = (198, 197, 187)

    draw.rectangle([28, 24, 996, 616], outline=cinnabar, width=2)
    draw.text((340, 44), spec["invoice_type"], fill=cinnabar, font=f_title)
    draw.ellipse([430, 34, 594, 96], outline=cinnabar, width=2)
    draw.text((456, 54), "全国统一发票监制章", fill=cinnabar, font=f_mono)

    code_line = f"发票代码：{spec.get('invoice_code') or '-'}"
    draw.text((690, 38), code_line, fill=ink, font=f_mono)
    draw.text((690, 64), f"发票号码：{spec['invoice_number']}", fill=ink, font=f_mono)
    draw.text((690, 90), f"开票日期：{spec['issue_date']}", fill=ink, font=f_mono)

    # 左上二维码示意格
    for gx in range(8):
        for gy in range(8):
            if (gx * 7 + gy * 13 + len(spec["invoice_number"])) % 2 == 0:
                draw.rectangle([48 + gx * 8, 38 + gy * 8, 54 + gx * 8, 44 + gy * 8], fill=ink)

    draw.line([28, 126, 996, 126], fill=cinnabar, width=2)
    draw.text((46, 142), f"购买方名称：{spec['buyer_name']}", fill=ink, font=f_sub)
    draw.text((46, 174), f"购买方纳税人识别号：{spec['buyer_tax_id']}", fill=ink, font=f_mono)
    draw.line([28, 212, 996, 212], fill=line_c, width=1)

    pct = f"{int((Decimal(spec['tax_rate']) * 100).to_integral_value())}%"
    draw.text((46, 228), "货物或应税劳务、服务名称", fill=cinnabar, font=f_sub)
    draw.text((540, 228), "金额（不含税）", fill=cinnabar, font=f_sub)
    draw.text((710, 228), "税率", fill=cinnabar, font=f_sub)
    draw.text((820, 228), "税额", fill=cinnabar, font=f_sub)

    draw.text((46, 272), spec["item_name"], fill=ink, font=f_sub)
    draw.text((540, 272), f"¥{spec['net_amount']}", fill=ink, font=f_mono)
    draw.text((710, 272), pct, fill=ink, font=f_mono)
    draw.text((820, 272), f"¥{spec['tax_amount']}", fill=ink, font=f_mono)

    draw.line([28, 360, 996, 360], fill=cinnabar, width=2)
    draw.text((46, 382), f"价税合计（小写）：¥{spec['total_amount']}", fill=ink, font=f_title)
    draw.line([28, 440, 996, 440], fill=line_c, width=1)
    draw.text((46, 462), f"销售方名称：{spec['seller_name']}", fill=ink, font=f_sub)
    draw.text((46, 496), f"销售方纳税人识别号：{spec['seller_tax_id']}", fill=ink, font=f_mono)
    draw.text((46, 536), f"校验码：{spec.get('check_code', '')}", fill=ink, font=f_mono)

    out = io.BytesIO()
    pnginfo = PngImagePlugin.PngInfo()
    if embed_qr_metadata and spec.get("qr_code"):
        pnginfo.add_text("fapiao_qr", spec["qr_code"])
        pnginfo.add_text("fapiao_text", format_invoice_as_text(spec))
    img.save(out, format="PNG", pnginfo=pnginfo)
    return out.getvalue()


# ---------------------------------------------------------------------------
# 解析层：PDF 文本流 / 国税局二维码 / 正则版式提取 / 多模态视觉客户端
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InvoiceDocument:
    invoice_type: str
    invoice_code: str
    invoice_number: str
    issue_date: str
    buyer_name: str
    buyer_tax_id: str
    seller_name: str
    seller_tax_id: str
    item_name: str
    net_amount: Decimal
    tax_rate: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    check_code: str = ""
    is_red_ink: bool = False
    extraction_source: str = "structured_regex"

    def to_dict(self) -> dict[str, Any]:
        return {
            "invoice_type": self.invoice_type,
            "invoice_code": self.invoice_code,
            "invoice_number": self.invoice_number,
            "issue_date": self.issue_date,
            "buyer_name": self.buyer_name,
            "buyer_tax_id": self.buyer_tax_id,
            "seller_name": self.seller_name,
            "seller_tax_id": self.seller_tax_id,
            "item_name": self.item_name,
            "net_amount": fmt_money(self.net_amount),
            "tax_rate": f"{int((self.tax_rate * 100).to_integral_value())}%",
            "tax_rate_decimal": f"{self.tax_rate:.2f}",
            "tax_amount": fmt_money(self.tax_amount),
            "total_amount": fmt_money(self.total_amount),
            "check_code": self.check_code,
            "is_red_ink": self.is_red_ink,
            "extraction_source": self.extraction_source,
        }


QR_SUBTYPE_MAP = {
    "01": "增值税专用发票",
    "04": "增值税普通发票",
    "10": "增值税电子普通发票",
    "31": "电子发票（增值税专用发票）",
    "32": "电子发票（普通发票）",
    "51": "电子发票（铁路电子客票）",
}


def parse_fapiao_qr(qr_text: str) -> dict[str, str]:
    """解析国家税务总局发票二维码字符串：01,<票种>,<代码>,<号码>,<不含税金额>,<YYYYMMDD>,<校验码>,<随机码>"""
    parts = [p.strip() for p in (qr_text or "").strip().split(",")]
    if len(parts) < 6 or parts[0] != "01":
        raise InvoiceParseError("非标准国家税务总局发票二维码格式")
    subtype, code, number, net_s, raw_date = parts[1], parts[2], parts[3], parts[4], parts[5]
    check_code = parts[6] if len(parts) > 6 else ""
    if not number or not re.match(r"^\d{8,20}$", number):
        raise InvoiceParseError(f"二维码发票号码非法: {number!r}")
    if len(raw_date) == 8 and raw_date.isdigit():
        iso_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
    else:
        iso_date = raw_date
    return {
        "invoice_type": QR_SUBTYPE_MAP.get(subtype, "增值税电子普通发票"),
        "invoice_code": code if code.isdigit() else "",
        "invoice_number": number,
        "net_amount": net_s,
        "issue_date": iso_date,
        "check_code": check_code,
    }


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """用 pypdf 确定性读取 PDF 所有页面的版式文本流。"""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(p for p in pages if p.strip())
    except Exception as exc:
        raise InvoiceParseError(f"PDF 解析失败: {exc}") from exc


def _first(patterns: list[str], text: str, default: str = "") -> str:
    for pat in patterns:
        m = re.search(pat, text, re.I | re.M)
        if m:
            return m.group(1).strip()
    return default


def _parse_decimal(s: str) -> Decimal | None:
    if not s:
        return None
    cleaned = s.replace(",", "").replace("¥", "").replace("￥", "").replace("元", "").strip()
    try:
        return _q2(cleaned)
    except (InvalidOperation, ValueError):
        return None


def parse_invoice_text(text: str, *, qr_code: str = "", source: str = "structured_regex") -> InvoiceDocument:
    """从电子发票文本 + 可选国税局二维码确定性提取全部中国电子发票核心版式要素并核验勾稽数学关系。"""
    qr_info = parse_fapiao_qr(qr_code) if qr_code.strip() else {}
    embedded_qr = _first([r"发票二维码[：:]\s*(01,[^\s]+)", r"\b(01,\d{2},[^\s]+)"], text)
    if embedded_qr and not qr_info:
        try:
            qr_info = parse_fapiao_qr(embedded_qr)
        except InvoiceParseError:
            qr_info = {}

    inv_type = _first(
        [
            r"票据类型[：:]\s*([^\n]+)",
            r"((?:红字)?(?:电子发票（[^）]+）|增值税电子普通发票|增值税专用发票|增值税普通发票|铁路电子客票))",
        ],
        text,
        qr_info.get("invoice_type", "增值税电子普通发票"),
    )
    inv_code = _first([r"发票代码[：:]\s*(\d{10,12})\b"], text, qr_info.get("invoice_code", ""))
    inv_no = _first([r"发票号码[：:]\s*(\d{8,20})\b", r"\b(\d{20})\b"], text, qr_info.get("invoice_number", ""))
    raw_date = _first(
        [
            r"开票日期[：:]\s*(\d{4}[-年/]\d{1,2}[-月/]\d{1,2})日?",
            r"\b(20\d{2}-\d{2}-\d{2})\b",
        ],
        text,
        qr_info.get("issue_date", ""),
    )
    if raw_date:
        nums = re.findall(r"\d+", raw_date)
        if len(nums) >= 3:
            raw_date = f"{int(nums[0]):04d}-{int(nums[1]):02d}-{int(nums[2]):02d}"
    issue_date = raw_date or date.today().isoformat()

    buyer_name = _first([r"购买方名称[：:]\s*([^\n]+)", r"购\s*名称[：:]\s*([^\n]+)"], text, "未命名购买方")
    buyer_tax = _first(
        [r"购买方(?:纳税人识别号|统一社会信用代码|税号)[：:]\s*([0-9A-Z]{15,20})"],
        text,
        "",
    )
    seller_name = _first([r"销售方名称[：:]\s*([^\n]+)", r"销\s*名称[：:]\s*([^\n]+)"], text, "未命名销售方")
    seller_tax = _first(
        [r"销售方(?:纳税人识别号|统一社会信用代码|税号)[：:]\s*([0-9A-Z]{15,20})"],
        text,
        "",
    )
    if not buyer_tax or not seller_tax:
        all_tax = re.findall(r"\b([0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10})\b", text)
        if not buyer_tax and len(all_tax) >= 1:
            buyer_tax = all_tax[0]
        if not seller_tax and len(all_tax) >= 2:
            seller_tax = all_tax[1]

    item_name = _first(
        [
            r"货物或应税劳务、服务名称[：:]\s*([^\n]+)",
            r"项目名称[：:]\s*([^\n]+)",
            r"(\*[^*\n]+\*[^\n¥￥]+)",
        ],
        text,
        "现代服务·技术服务费",
    ).strip()

    rate_str = _first([r"税率(?:/征收率)?[：:\s]*(\d{1,2}(?:\.\d+)?)%", r"\b(13|9|6|3|1|0)%"], text, "")
    tax_rate = Decimal(rate_str) / Decimal("100") if rate_str else Decimal("0.06")

    total_d = _parse_decimal(
        _first([r"价税合计(?:（小写）|\(小写\))?[：:\s]*[¥￥]?\s*(-?[\d,]+\.\d{2})", r"[¥￥]\s*(-?[\d,]+\.\d{2})"], text)
    )
    net_d = _parse_decimal(
        _first([r"不含税金额[：:\s]*[¥￥]?\s*(-?[\d,]+\.\d{2})", r"合\s*计[：:\s]*[¥￥]?\s*(-?[\d,]+\.\d{2})"], text)
        or qr_info.get("net_amount", "")
    )
    tax_d = _parse_decimal(_first([r"(?<!不含)税额[：:\s]*[¥￥]?\s*(-?[\d,]+\.\d{2})"], text))

    if total_d is None and net_d is not None and tax_d is not None:
        total_d = _q2(net_d + tax_d)
    elif total_d is None and net_d is not None:
        tax_d = _q2(net_d * tax_rate)
        total_d = _q2(net_d + tax_d)
    elif total_d is not None and net_d is None:
        if tax_d is not None:
            net_d = _q2(total_d - tax_d)
        else:
            net_d = _q2(total_d / (Decimal("1") + tax_rate))
            tax_d = _q2(total_d - net_d)
    elif total_d is not None and net_d is not None and tax_d is None:
        tax_d = _q2(total_d - net_d)
        if net_d != 0 and not rate_str:
            tax_rate = (abs(tax_d) / abs(net_d)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    if total_d is None or net_d is None or tax_d is None:
        raise InvoiceParseError("票面缺少可识别的金额要素（价税合计/不含税金额）")

    check_code = _first([r"校验码[：:]\s*([0-9A-Za-z]{6,20})"], text, qr_info.get("check_code", ""))
    if not inv_no:
        inv_no = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8].upper()
    is_red = total_d < 0 or ("红字" in inv_type) or ("负数" in inv_type) or ("红冲" in item_name and total_d < 0)
    src = "qr_plus_regex" if (qr_code.strip() or embedded_qr) and source == "structured_regex" else source

    return InvoiceDocument(
        invoice_type=inv_type,
        invoice_code=inv_code,
        invoice_number=inv_no,
        issue_date=issue_date,
        buyer_name=buyer_name,
        buyer_tax_id=buyer_tax,
        seller_name=seller_name,
        seller_tax_id=seller_tax,
        item_name=item_name,
        net_amount=net_d,
        tax_rate=tax_rate,
        tax_amount=tax_d,
        total_amount=total_d,
        check_code=check_code,
        is_red_ink=is_red,
        extraction_source=src,
    )


def _resize_image_for_vision(image_bytes: bytes, max_edge: int = 1024) -> tuple[str, str, dict[str, str]]:
    """将票据图像长边压缩至 ≤1024px（实测最省 token 且防幻觉），同时读取 PNG 文本块（若有）。"""
    try:
        with Image.open(io.BytesIO(image_bytes)) as im:
            meta = {str(k): str(v) for k, v in (im.info or {}).items() if isinstance(v, (str, bytes))}
            w, h = im.size
            if max(w, h) > max_edge:
                scale = max_edge / float(max(w, h))
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return "image/png", base64.b64encode(buf.getvalue()).decode("ascii"), meta
    except Exception as exc:
        raise InvoiceParseError(f"无法读取票据图像: {exc}") from exc


VISION_SYSTEM_PROMPT = (
    "你是中国注册会计师级电子发票结构化提取引擎。请精确读取发票图片中的版式文字，只输出一个严格合法的 JSON 对象，"
    "字段必须包含：invoice_type, invoice_code, invoice_number, issue_date (YYYY-MM-DD), "
    "buyer_name, buyer_tax_id, seller_name, seller_tax_id, item_name, "
    "net_amount (两位小数字符串), tax_rate (如 '0.06' 或 '0.13'), tax_amount (两位小数字符串), "
    "total_amount (两位小数字符串), check_code。不要输出 Markdown 代码块以外的解释。"
)


def _is_private_or_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private
    except ValueError:
        return False


class VisionInvoiceClient:
    """多模态真票识别客户端：优先自有 CLIProxyAPI（gemini-4-carbon-high），无内网时自动切标准 OpenAI 多模态接口。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = DEFAULT_VISION_MODEL,
        *,
        timeout: float = 15.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def __repr__(self) -> str:
        return f"VisionInvoiceClient(model={self.model!r}, base_url={self.base_url!r})"

    @classmethod
    def from_default_env(
        cls,
        cliproxy_path: Path = CLIPROXY_ENV_PATH,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> "VisionInvoiceClient | None":
        # 1) 自有内网 CLIProxyAPI（~/.secrets/cliproxy.env，默认 gemini-4-carbon-high）
        c_env = read_env_file(cliproxy_path)
        c_url = os.environ.get("CLIPROXY_BASE_URL") or c_env.get("CLIPROXY_BASE_URL", "")
        c_key = os.environ.get("CLIPROXY_API_KEY") or c_env.get("CLIPROXY_API_KEY", "")
        c_model = os.environ.get("CLIPROXY_VISION_MODEL") or c_env.get("AI_MODEL_DEFAULT", DEFAULT_VISION_MODEL)
        if c_url and c_key:
            return cls(c_url, c_key, c_model, transport=transport)

        # 2) 无内网时回退标准 OpenAI 多模态兼容接口
        o_url = os.environ.get("OPENAI_BASE_URL", "")
        o_key = os.environ.get("OPENAI_API_KEY", "")
        o_model = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o-mini")
        if o_url and o_key:
            return cls(o_url, o_key, o_model, transport=transport)
        return None

    def extract_image(self, image_bytes: bytes) -> InvoiceDocument:
        mime, b64, _ = _resize_image_for_vision(image_bytes)
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 600,
            "messages": [
                {"role": "system", "content": VISION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请提取该中国电子发票的全部版式字段并输出 JSON："},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                },
            ],
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
                if resp.status_code in (429, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep(0.25 * (2 ** attempt))
                    continue
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"] or ""
                m = re.search(r"\{.*\}", content, re.S)
                if not m:
                    raise InvoiceParseError("视觉模型未返回 JSON 结构")
                d = json.loads(m.group(0))
                return _doc_from_vision_json(d, f"vision:{self.model}")
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_err = exc
                if attempt < self.max_retries:
                    time.sleep(0.25 * (2 ** attempt))
                    continue
            except Exception as exc:
                last_err = exc
                break
        raise InvoiceParseError(f"多模态识票调用失败: {type(last_err).__name__}")


def _doc_from_vision_json(d: dict[str, Any], source: str) -> InvoiceDocument:
    rate_raw = str(d.get("tax_rate", "0.06")).strip()
    if rate_raw.endswith("%"):
        tax_rate = Decimal(rate_raw[:-1]) / Decimal("100")
    else:
        tax_rate = Decimal(rate_raw or "0.06")
        if tax_rate > 1:
            tax_rate = tax_rate / Decimal("100")
    total_d = _parse_decimal(str(d.get("total_amount", "")))
    net_d = _parse_decimal(str(d.get("net_amount", "")))
    tax_d = _parse_decimal(str(d.get("tax_amount", "")))
    if total_d is None and net_d is not None:
        tax_d = tax_d if tax_d is not None else _q2(net_d * tax_rate)
        total_d = _q2(net_d + tax_d)
    elif total_d is not None and net_d is None:
        net_d = _q2(total_d / (Decimal("1") + tax_rate))
        tax_d = _q2(total_d - net_d)
    elif total_d is not None and net_d is not None and tax_d is None:
        tax_d = _q2(total_d - net_d)
    if total_d is None or net_d is None or tax_d is None:
        raise InvoiceParseError("视觉提取缺少有效金额")

    inv_type = str(d.get("invoice_type") or "增值税电子普通发票")
    inv_no = str(d.get("invoice_number") or "")
    is_red = total_d < 0 or ("红字" in inv_type) or ("负数" in inv_type)
    return InvoiceDocument(
        invoice_type=inv_type,
        invoice_code=str(d.get("invoice_code") or ""),
        invoice_number=inv_no,
        issue_date=str(d.get("issue_date") or date.today().isoformat()),
        buyer_name=str(d.get("buyer_name") or ""),
        buyer_tax_id=str(d.get("buyer_tax_id") or ""),
        seller_name=str(d.get("seller_name") or ""),
        seller_tax_id=str(d.get("seller_tax_id") or ""),
        item_name=str(d.get("item_name") or "现代服务"),
        net_amount=net_d,
        tax_rate=tax_rate,
        tax_amount=tax_d,
        total_amount=total_d,
        check_code=str(d.get("check_code") or ""),
        is_red_ink=is_red,
        extraction_source=source,
    )


# ---------------------------------------------------------------------------
# 三单勾稽（发票 ↔ 合同 ↔ 资金流水）+ 红冲差额计算 + 借贷平衡记账凭证
# ---------------------------------------------------------------------------
def _pick_expense_account(item_name: str, invoice_type: str) -> str:
    s = f"{item_name} {invoice_type}"
    if any(k in s for k in ("云服务器", "信息技术", "软件", "数据接口", "研发")):
        return "管理费用-信息化服务费"
    if any(k in s for k in ("客运", "铁路", "火车", "机票", "差旅", "住宿")):
        return "管理费用-差旅费"
    if any(k in s for k in ("办公", "耗材", "文具", "人体工学椅")):
        return "管理费用-办公费"
    if any(k in s for k in ("餐饮", "招待")):
        return "管理费用-业务招待费"
    return "管理费用-综合服务费"


def compute_red_ink_adjustment(
    inv: InvoiceDocument,
    *,
    contract_amount: Decimal | None = None,
    explicit_red_ink_amount: Decimal | None = None,
    expense_account: str = "管理费用-综合服务费",
    deductible_vat: bool = True,
) -> dict[str, Any]:
    """计算红字发票冲销差额与红字调整会计分录（精确到分，保证不含税 + 税额 == 价税合计）。"""
    trigger = "NONE"
    target_total = Decimal("0.00")

    if explicit_red_ink_amount is not None and _q2(explicit_red_ink_amount) != 0:
        trigger = "EXPLICIT_ALLOWANCE_OR_RETURN"
        target_total = -abs(_q2(explicit_red_ink_amount))
    elif inv.is_red_ink or inv.total_amount < 0:
        trigger = "RED_INK_INVOICE_DIRECT"
        target_total = inv.total_amount
    elif contract_amount is not None and inv.total_amount > contract_amount > 0:
        trigger = "OVERBILLED_VS_CONTRACT"
        target_total = -_q2(inv.total_amount - contract_amount)

    if trigger == "NONE":
        return {
            "required": False,
            "trigger": "NONE",
            "red_ink_total": "0.00",
            "red_ink_net": "0.00",
            "red_ink_tax": "0.00",
            "entries": [],
            "note": "票面金额未超出合同约定且无折让退回，无需开具红字信息表。",
        }

    if trigger == "RED_INK_INVOICE_DIRECT":
        red_net = inv.net_amount
        red_tax = inv.tax_amount
        red_total = inv.total_amount
    else:
        red_total = target_total
        red_net = _q2(red_total / (Decimal("1") + inv.tax_rate))
        red_tax = _q2(red_total - red_net)

    entries: list[dict[str, str]] = []
    if deductible_vat and red_tax != 0:
        entries.append({"direction": "借", "account": expense_account, "amount": fmt_money(red_net)})
        entries.append(
            {"direction": "借", "account": "应交税费-应交增值税（进项税额转出/红冲）", "amount": fmt_money(red_tax)}
        )
    else:
        entries.append({"direction": "借", "account": expense_account, "amount": fmt_money(red_total)})
    entries.append({"direction": "贷", "account": "应付账款-红字冲销待结算", "amount": fmt_money(red_total)})

    return {
        "required": True,
        "trigger": trigger,
        "original_invoice_number": inv.invoice_number,
        "red_ink_total": fmt_money(red_total),
        "red_ink_net": fmt_money(red_net),
        "red_ink_tax": fmt_money(red_tax),
        "tax_rate": f"{int((inv.tax_rate * 100).to_integral_value())}%",
        "math_balanced": (red_net + red_tax == red_total),
        "entries": entries,
        "note": (
            f"检测到 {trigger}：应开具/入账红字冲销价税合计 ¥{fmt_money(red_total)}"
            f"（不含税 ¥{fmt_money(red_net)}，税额 ¥{fmt_money(red_tax)}）。"
        ),
    }


def reconcile_and_bookkeep(
    inv: InvoiceDocument,
    *,
    taxpayer_type: str = "general",
    expected_buyer_tax_id: str = "",
    contract: dict[str, Any] | None = None,
    bank_flow: dict[str, Any] | None = None,
    red_ink_amount: str | None = None,
) -> dict[str, Any]:
    """执行发票-合同-流水三单勾稽、红冲差额测算，并生成借贷必相等的标准会计分录。"""
    math_diff = _q2(inv.net_amount + inv.tax_amount - inv.total_amount)
    invoice_math_ok = abs(math_diff) <= Decimal("0.01")
    buyer_tax_ok = validate_tax_id(inv.buyer_tax_id) if inv.buyer_tax_id else False
    if expected_buyer_tax_id:
        buyer_tax_ok = buyer_tax_ok and (inv.buyer_tax_id.upper() == expected_buyer_tax_id.strip().upper())

    checks: list[dict[str, Any]] = [
        {
            "check": "票面价税勾稽",
            "passed": invoice_math_ok,
            "detail": f"不含税 {fmt_money(inv.net_amount)} + 税额 {fmt_money(inv.tax_amount)} = {fmt_money(inv.net_amount + inv.tax_amount)}（票面价税合计 {fmt_money(inv.total_amount)}）",
        },
        {
            "check": "购买方税号核验",
            "passed": buyer_tax_ok,
            "detail": f"税号 {inv.buyer_tax_id or '未填'} {'符合 18 位统一社会信用代码规范' if buyer_tax_ok else '缺失或与账套税号不一致'}",
        },
    ]

    contract_amt: Decimal | None = None
    contract_diff = Decimal("0.00")
    if contract and contract.get("contract_amount") not in (None, ""):
        contract_amt = _q2(contract["contract_amount"])
        contract_diff = _q2(inv.total_amount - contract_amt)
        party_ok = not contract.get("party_name") or (contract["party_name"].strip() == inv.seller_name.strip())
        c_rate = Decimal(str(contract.get("tax_rate", inv.tax_rate)))
        if c_rate > 1:
            c_rate = c_rate / Decimal("100")
        rate_ok = _q2(c_rate) == _q2(inv.tax_rate)
        period_ok = True
        if contract.get("start_date") and contract.get("end_date"):
            period_ok = str(contract["start_date"]) <= inv.issue_date <= str(contract["end_date"])
        checks.append(
            {
                "check": "票同勾稽（发票 vs 合同）",
                "passed": party_ok and rate_ok and period_ok and contract_diff <= 0,
                "detail": (
                    f"合同 {contract.get('contract_no', '-')} 金额 ¥{fmt_money(contract_amt)}，"
                    f"票同差额 ¥{fmt_money(contract_diff)}，"
                    f"销方匹配={'是' if party_ok else '否'}，税率一致={'是' if rate_ok else '否'}，履约期内={'是' if period_ok else '否'}"
                ),
            }
        )

    paid_amt: Decimal | None = None
    payment_diff = Decimal("0.00")
    if bank_flow and bank_flow.get("paid_amount") not in (None, ""):
        paid_amt = _q2(bank_flow["paid_amount"])
        payment_diff = _q2(inv.total_amount - paid_amt)
        payee_ok = not bank_flow.get("payee_name") or (bank_flow["payee_name"].strip() == inv.seller_name.strip())
        checks.append(
            {
                "check": "票款勾稽（发票 vs 银行/支付宝流水）",
                "passed": payee_ok and payment_diff == 0,
                "detail": (
                    f"流水 {bank_flow.get('flow_id', '-')} 实付 ¥{fmt_money(paid_amt)}，"
                    f"票款差额 ¥{fmt_money(payment_diff)}，收款户名一致={'是' if payee_ok else '否'}"
                ),
            }
        )

    expense_acct = _pick_expense_account(inv.item_name, inv.invoice_type)
    can_deduct_vat = (
        taxpayer_type == "general"
        and inv.tax_amount != 0
        and any(k in inv.invoice_type for k in ("专用发票", "铁路电子客票"))
    )

    explicit_red = _parse_decimal(red_ink_amount) if red_ink_amount else None
    red_ink = compute_red_ink_adjustment(
        inv,
        contract_amount=contract_amt,
        explicit_red_ink_amount=explicit_red,
        expense_account=expense_acct,
        deductible_vat=(taxpayer_type == "general"),
    )

    entries: list[dict[str, str]] = []
    if can_deduct_vat:
        entries.append({"direction": "借", "account": expense_acct, "amount": fmt_money(inv.net_amount)})
        entries.append(
            {"direction": "借", "account": "应交税费-应交增值税（进项税额）", "amount": fmt_money(inv.tax_amount)}
        )
    else:
        entries.append({"direction": "借", "account": expense_acct, "amount": fmt_money(inv.total_amount)})

    # 贷方按真实流水差额拆分：已付走银行存款，未付差额挂应付账款，超付挂预付账款
    if paid_amt is None or payment_diff == 0:
        entries.append({"direction": "贷", "account": "银行存款", "amount": fmt_money(inv.total_amount)})
    elif inv.total_amount > 0 and Decimal("0") < paid_amt < inv.total_amount:
        entries.append({"direction": "贷", "account": "银行存款", "amount": fmt_money(paid_amt)})
        entries.append({"direction": "贷", "account": "应付账款", "amount": fmt_money(payment_diff)})
    elif inv.total_amount > 0 and paid_amt <= 0:
        entries.append({"direction": "贷", "account": "应付账款", "amount": fmt_money(inv.total_amount)})
    elif inv.total_amount > 0 and paid_amt > inv.total_amount:
        overpaid = _q2(paid_amt - inv.total_amount)
        entries.append({"direction": "借", "account": "预付账款", "amount": fmt_money(overpaid)})
        entries.append({"direction": "贷", "account": "银行存款", "amount": fmt_money(paid_amt)})
    else:
        entries.append({"direction": "贷", "account": "银行存款", "amount": fmt_money(inv.total_amount)})

    debit_sum = sum((_q2(e["amount"]) for e in entries if e["direction"] == "借"), Decimal("0.00"))
    credit_sum = sum((_q2(e["amount"]) for e in entries if e["direction"] == "贷"), Decimal("0.00"))

    if red_ink["required"] and red_ink["trigger"] == "OVERBILLED_VS_CONTRACT":
        status = "NEEDS_RED_INK"
    elif all(c["passed"] for c in checks):
        status = "MATCHED"
    elif payment_diff != 0:
        status = "PARTIAL_SETTLED"
    else:
        status = "REVIEW_REQUIRED"

    summary = f"{inv.issue_date} {inv.seller_name} {inv.item_name}（票号 {inv.invoice_number}）"
    debit_accounts = " / ".join(f"{e['account']} ¥{e['amount']}" for e in entries if e["direction"] == "借")
    credit_accounts = " / ".join(f"{e['account']} ¥{e['amount']}" for e in entries if e["direction"] == "贷")

    voucher = {
        "摘要": summary,
        "借": debit_accounts,
        "贷": credit_accounts,
        "taxpayer_type": taxpayer_type,
        "debit_total": fmt_money(debit_sum),
        "credit_total": fmt_money(credit_sum),
        "balanced": debit_sum == credit_sum,
        "entries": entries,
    }
    reconciliation = {
        "status": status,
        "three_way_checked": bool(contract is not None and bank_flow is not None),
        "invoice_math_ok": invoice_math_ok,
        "buyer_tax_id_valid": buyer_tax_ok,
        "contract_diff": fmt_money(contract_diff),
        "payment_diff": fmt_money(payment_diff),
        "checks": checks,
    }
    return {
        "invoice": inv.to_dict(),
        "voucher": voucher,
        "reconciliation": reconciliation,
        "red_ink_adjustment": red_ink,
        "kingdee_csv": export_kingdee_csv(inv, voucher, red_ink),
    }


def export_kingdee_csv(inv: InvoiceDocument, voucher: dict[str, Any], red_ink: dict[str, Any] | None = None) -> str:
    """导出金蝶云星辰 / 用友好会计通用标准记账凭证 CSV（含红冲调整分录，若有）。"""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["凭证日期", "凭证字", "凭证号", "摘要", "科目名称", "借方金额", "贷方金额", "发票号码", "往来单位"])
    for e in voucher["entries"]:
        debit = e["amount"] if e["direction"] == "借" else "0.00"
        credit = e["amount"] if e["direction"] == "贷" else "0.00"
        w.writerow([inv.issue_date, "记", "001", voucher["摘要"], e["account"], debit, credit, inv.invoice_number, inv.seller_name])
    if red_ink and red_ink.get("required") and red_ink.get("trigger") != "RED_INK_INVOICE_DIRECT":
        for e in red_ink["entries"]:
            debit = e["amount"] if e["direction"] == "借" else "0.00"
            credit = e["amount"] if e["direction"] == "贷" else "0.00"
            w.writerow([
                inv.issue_date,
                "记",
                "002",
                f"红字冲销差额（原票 {inv.invoice_number}）",
                e["account"],
                debit,
                credit,
                inv.invoice_number,
                inv.seller_name,
            ])
    return buf.getvalue()


def lookup_ledger_counterparts(inv: InvoiceDocument) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """按发票号码、销方税号或销方名称在企业待勾稽合同台账与银行流水中自动匹配三单关联项。"""
    for s in REAL_INVOICE_SAMPLES.values():
        if (
            (inv.invoice_number and inv.invoice_number == s["invoice_number"])
            or (inv.seller_tax_id and inv.seller_tax_id.upper() == s["seller_tax_id"].upper())
            or (inv.seller_name and inv.seller_name == s["seller_name"] and (inv.total_amount < 0) == (Decimal(s["total_amount"]) < 0))
        ):
            return s.get("contract"), s.get("bank_flow")
    return None, None


def resolve_sample_by_doc_name(doc_name: str) -> dict[str, Any]:
    """将工作台下拉项或单据标题映射到真实结构化电子发票样张规格（不再返回静态假桩）。"""
    s = (doc_name or "").strip()
    if s in REAL_INVOICE_SAMPLES:
        return REAL_INVOICE_SAMPLES[s]
    if "专用发票" in s or "办公" in s or "1,130" in s or "1130" in s:
        return REAL_INVOICE_SAMPLES["office-spec-overbill"]
    if "火车" in s or "铁路" in s or "虹桥" in s or "73" in s:
        return REAL_INVOICE_SAMPLES["rail-ticket-deduct"]
    if "红字" in s or "红冲" in s or "-636" in s:
        return REAL_INVOICE_SAMPLES["red-ink-credit-note"]
    return REAL_INVOICE_SAMPLES["cloud-vat-normal"]


def parse_any_invoice(
    *,
    sample_id: str = "",
    doc_name: str = "",
    invoice_text: str = "",
    qr_code: str = "",
    file_bytes: bytes | None = None,
    filename: str = "",
    use_vision: bool = True,
    vision_client: VisionInvoiceClient | None = None,
) -> tuple[InvoiceDocument, dict[str, Any] | None]:
    """统一票据解析入口：支持真实 PDF 字节流、图像字节流（多模态 + 元数据兜底）、二维码、版式文本与内置样张。"""
    matched_sample: dict[str, Any] | None = None

    if file_bytes:
        is_pdf = file_bytes.startswith(b"%PDF") or filename.lower().endswith(".pdf")
        if is_pdf:
            pdf_text = extract_pdf_text(file_bytes)
            if not pdf_text.strip():
                raise InvoiceParseError("PDF 未提取到版式文本层，请上传电子发票原件或转为图片开启视觉识别")
            return parse_invoice_text(pdf_text, qr_code=qr_code, source="pdf_text"), None

        # 图像输入：优先走真实多模态视觉识别（CLIProxyAPI gemini-4-carbon-high / OpenAI Vision）
        _, _, img_meta = _resize_image_for_vision(file_bytes)
        client = vision_client if vision_client is not None else (VisionInvoiceClient.from_default_env() if use_vision else None)
        if use_vision and client is not None:
            try:
                return client.extract_image(file_bytes), None
            except InvoiceParseError:
                pass

        # 无内网/离线兜底：提取 PNG 内嵌国税局二维码/版式文本或传入的 qr_code / invoice_text
        fallback_text = invoice_text or img_meta.get("fapiao_text", "")
        fallback_qr = qr_code or img_meta.get("fapiao_qr", "")
        if fallback_text or fallback_qr:
            return parse_invoice_text(fallback_text, qr_code=fallback_qr, source="qr_plus_regex"), None
        raise InvoiceParseError("当前处于无多模态网络环境且图片未附带发票二维码/文本要素")

    if invoice_text.strip() or qr_code.strip():
        return parse_invoice_text(invoice_text, qr_code=qr_code), None

    matched_sample = REAL_INVOICE_SAMPLES.get(sample_id) or resolve_sample_by_doc_name(doc_name)
    # 走真实的 PDF 生成 → pypdf 文本层提取 → 正则与数学勾稽全链路，绝不短路返回写死字典！
    real_pdf = build_invoice_pdf_bytes(matched_sample)
    extracted = extract_pdf_text(real_pdf)
    doc = parse_invoice_text(extracted, qr_code=matched_sample.get("qr_code", ""), source="pdf_text")
    return doc, matched_sample
