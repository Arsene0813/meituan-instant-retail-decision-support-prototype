from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import httpx
import os
import time
import uuid
import json
import asyncio
import re
if __package__:
    from .retail_entity_scope import (
        extract_retail_entity_ids,
        resolve_retail_entity_id,
        retail_fact_matches_entities,
    )
else:
    from retail_entity_scope import (
        extract_retail_entity_ids,
        resolve_retail_entity_id,
        retail_fact_matches_entities,
    )

if __package__:
    from .retail_evidence_scope import (
        DEMO1_WINDOW, DEMO2_WINDOW, RetailWindow,
        parse_retail_window, select_retail_points,
    )
else:
    from retail_evidence_scope import (
        DEMO1_WINDOW, DEMO2_WINDOW, RetailWindow,
        parse_retail_window, select_retail_points,
    )

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")

QDRANT_BASE_URL = os.getenv("QDRANT_BASE_URL", "http://qdrant:6333")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")
QDRANT_KB_COLLECTION = os.getenv("QDRANT_KB_COLLECTION", "mem_kb")
QDRANT_CHAT_COLLECTION = os.getenv("QDRANT_CHAT_COLLECTION", "mem_chat")
KB_TOP_K = int(os.getenv("KB_TOP_K", "3"))
CHAT_TOP_K = int(os.getenv("CHAT_TOP_K", "5"))
CHAT_SCORE_THRESHOLD = float(os.getenv("CHAT_SCORE_THRESHOLD", "0.75"))
KB_SCORE_THRESHOLD = float(os.getenv("KB_SCORE_THRESHOLD", "0.75"))
CHAT_BASE_THRESHOLD = float(os.getenv("CHAT_BASE_THRESHOLD", "0.60"))
CHAT_GAP_THRESHOLD  = float(os.getenv("CHAT_GAP_THRESHOLD",  "0.05"))
DEBUG_ROUTES_ENABLED = os.getenv("DEBUG_ROUTES_ENABLED", "true").lower() == "true"

LIVESTREAM_FACT_TYPES = {
    "product_price",
    "promo",
    "stock_status",
    "shipping_policy",
    "product_feature",
}

FACT_POLICIES = {
    "pet_name": {
        "scope": "session",
        "slot": "pet_name",
        "single_value": False,
        "freshness_days": 30,
        "routing_threshold": None,
        "entity_kind": "self",
    },
    "location": {
        "scope": "session",
        "slot": "location",
        "single_value": True,
        "freshness_days": 30,
        "routing_threshold": None,
        "entity_kind": "self",
    },
    "preference": {
        "scope": "session",
        "slot": "preference",
        "single_value": False,
        "freshness_days": 30,
        "routing_threshold": None,
        "entity_kind": "self",
    },
    "identity": {
        "scope": "session",
        "slot": "identity",
        "single_value": True,
        "freshness_days": 30,
        "routing_threshold": None,
        "entity_kind": "self",
    },
    "relationship": {
        "scope": "session",
        "slot": "relationship",
        "single_value": False,
        "freshness_days": 30,
        "routing_threshold": None,
        "entity_kind": "self",
    },
    "product_price": {
        "scope": "catalog",
        "slot": "price",
        "single_value": True,
        "freshness_days": 7,
        "routing_threshold": 0.65,
        "entity_kind": "default_product",
    },
    "promo": {
        "scope": "catalog",
        "slot": "promo",
        "single_value": True,
        "freshness_days": 1,
        "routing_threshold": 0.65,
        "entity_kind": "default_product",
    },
    "stock_status": {
        "scope": "catalog",
        "slot": "stock_status",
        "single_value": True,
        "freshness_days": 3,
        "routing_threshold": 0.65,
        "entity_kind": "default_product",
    },
    "shipping_policy": {
        "scope": "catalog",
        "slot": "shipping_policy",
        "single_value": True,
        "freshness_days": 30,
        "routing_threshold": 0.65,
        "entity_kind": "default_product",
    },
    "product_feature": {
        "scope": "catalog",
        "slot": "feature",
        "single_value": False,
        "freshness_days": 30,
        "routing_threshold": 0.60,
        "entity_kind": "default_product",
    },
}

REQUIRED_POLICY_KEYS = {
    "scope",
    "slot",
    "single_value",
    "freshness_days",
    "routing_threshold",
    "entity_kind",
}

def validate_fact_policies():
    for fact_type, policy in FACT_POLICIES.items():
        missing = REQUIRED_POLICY_KEYS - set(policy.keys())
        if missing:
            raise RuntimeError(
                f"FACT_POLICIES[{fact_type}] missing keys: {sorted(missing)}"
            )

def normalize_fact_type(fact_type: str | None) -> str | None:
    if fact_type is None:
        return None

    t = str(fact_type).strip()
    if not t:
        return None

    return t

def get_fact_policy(fact_type: str | None) -> dict | None:
    fact_type = normalize_fact_type(fact_type)
    if fact_type is None:
        return None
    return FACT_POLICIES.get(fact_type)

def get_fact_policy_value(
    fact_type: str | None,
    key: str,
    default=None,
):
    policy = get_fact_policy(fact_type)
    if not policy:
        return default
    return policy.get(key, default)


def get_fact_freshness_days(fact_type: str | None) -> int:
    return int(get_fact_policy_value(fact_type, "freshness_days", 30))


def get_fact_entity_kind(fact_type: str | None) -> str:
    return str(get_fact_policy_value(fact_type, "entity_kind", "self"))


def get_livestream_score_threshold(fact_type: str | None) -> float:
    return float(get_fact_policy_value(fact_type, "routing_threshold", 0.65))

validate_fact_policies()
app = FastAPI(title="Agent API", version="0.1.0")

def require_session_id(session_id: str | None) -> str:
    sid = (session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required")
    return sid

class ChatReq(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, max_length=128)
    system: str | None = Field(default=None, max_length=4000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)

async def ollama_embed(text: str) -> list[float]:
    last_error = None

    for _ in range(3):
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(
                    f"{OLLAMA_BASE_URL}/api/embed",
                    json={"model": EMBED_MODEL, "input": text},
                )
                r.raise_for_status()
                data = r.json()
                return data["embeddings"][0]

        except Exception as e:
            last_error = e
            await asyncio.sleep(1)

    raise last_error

async def qdrant_upsert_chat(
    session_id: str,
    role: str,
    text: str,
    vector: list[float],
    fact_type: str | None = None,
):

    point_id = str(uuid.uuid4())
    payload = {
        "type": "chat",
        "session_id": session_id,
        "role": role,
        "text": text,
        "fact_type": fact_type,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    body = {"points": [{"id": point_id, "vector": vector, "payload": payload}]}

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.put(
            f"{QDRANT_BASE_URL}/collections/{QDRANT_CHAT_COLLECTION}/points",
            json=body,
        )
        r.raise_for_status()
        return r.json()

async def qdrant_search_chat(session_id: str, query_vector: list[float], limit: int):
    body = {
        "query": query_vector,
        "limit": limit,
        "with_payload": True,
        "filter": {"must": [{"key": "session_id", "match": {"value": session_id}}]},
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{QDRANT_BASE_URL}/collections/{QDRANT_CHAT_COLLECTION}/points/query",
            json=body,
        )
        r.raise_for_status()
        return r.json().get("result", {}).get("points", [])


async def qdrant_scroll_chat(session_id: str, limit: int = 20):
    body = {
        "limit": limit,
        "with_payload": True,
        "with_vectors": False,
        "filter": {"must": [{"key": "session_id", "match": {"value": session_id}}]},
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{QDRANT_BASE_URL}/collections/{QDRANT_CHAT_COLLECTION}/points/scroll",
            json=body,
        )
        r.raise_for_status()
        return r.json().get("result", {}).get("points", [])

async def get_last_user_message(session_id: str):
    points = await qdrant_scroll_chat(session_id, limit=50)
    user_points = []
    for p in points:
        payload = p.get("payload") or {}
        if payload.get("role") == "user":
            user_points.append(payload)

    if not user_points:
        return None

    user_points.sort(key=lambda x: x.get("ts") or "")
    return user_points[-1].get("text")

def is_obviously_non_fact_message(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return True

    exact_blocklist = {
        "你好", "您好", "哈喽", "嗨", "hi", "hello", "hey",
        "在吗", "在不在", "收到", "好的", "好", "嗯", "嗯嗯",
        "谢谢", "感谢", "多谢", "ok", "okay", "好的谢谢",
    }

    if t in exact_blocklist:
        return True

    short_patterns = [
        "你好啊", "您好啊", "谢谢啦", "收到啦",
    ]
    if t in short_patterns:
        return True

    return False

def should_store_user_memory(text: str) -> bool:
    t = (text or "").strip()

    if not t:
        return False

    # 太短，通常没有存储价值
    if len(t) < 2:
        return False

    # 纯数字，通常没有存储价值
    if t.isdigit():
        return False

    # 纯符号/标点，通常没有存储价值
    if all(not ch.isalnum() for ch in t):
        return False

    return True


def extract_product_ref_by_rules(text: str) -> str | None:
    t = (text or "").strip()
    if not t:
        return None

    patterns = [
        r"^([A-Za-z0-9\u4e00-\u9fa5\-_]+)的?(?:价格|售价|定价|特点|卖点)",
        r"^([A-Za-z0-9\u4e00-\u9fa5\-_]+)(?:目前)?(?:现货|有货|缺货|无货|售罄|预售)",
        r"^([A-Za-z0-9\u4e00-\u9fa5\-_]+)(?:支持|可)(?:次日达|当日达|包邮|免邮)",
        r"^([A-Za-z0-9\u4e00-\u9fa5\-_]+)主打",
    ]

    for pat in patterns:
        m = re.search(pat, t)
        if m:
            product_ref = m.group(1).strip()
            if product_ref and product_ref not in {"这款产品", "该产品", "本产品", "这个产品"}:
                return product_ref

    return None

def build_extract_fact_prompt(text: str) -> str:
    t = (text or "").strip()

    return (
        "You are a memory extraction system for a long-term assistant.\n"
        "Decide whether the message contains a storable fact. If yes, extract exactly one fact as JSON. If not, answer NONE.\n"
        "Valid fact types include: preference, identity, relationship, location, pet_name, "
        "product_price, promo, stock_status, shipping_policy, product_feature.\n"
        "Only extract if the message clearly contains a fact that should be stored as memory.\n"
        "For livestream or commerce-related messages, examples include product prices, promotions, "
        "stock status, shipping policies, and product features.\n\n"
        "Examples:\n"
        'User message: 这款隐形眼镜价格是99元\n'
        'Output: {"type":"product_price","value":"99元","source_text":"这款隐形眼镜价格是99元","product_ref":null}\n'
        'User message: A款隐形眼镜价格是99元\n'
        'Output: {"type":"product_price","value":"99元","source_text":"A款隐形眼镜价格是99元","product_ref":"A款隐形眼镜"}\n'
        'User message: 本场活动满199减30\n'
        'Output: {"type":"promo","value":"满199减30","source_text":"本场活动满199减30","product_ref":null}\n'
        'User message: C款护理液目前有货\n'
        'Output: {"type":"stock_status","value":"有货","source_text":"C款护理液目前有货","product_ref":"C款护理液"}\n'
        'User message: E款隐形眼镜支持次日达\n'
        'Output: {"type":"shipping_policy","value":"次日达","source_text":"E款隐形眼镜支持次日达","product_ref":"E款隐形眼镜"}\n'
        'User message: F款隐形眼镜主打高保湿\n'
        'Output: {"type":"product_feature","value":"高保湿","source_text":"F款隐形眼镜主打高保湿","product_ref":"F款隐形眼镜"}\n'
        'User message: 你好\n'
        'Output: NONE\n\n'
        "If no suitable fact is present, answer exactly: NONE\n"
        "Return JSON only, with this schema:\n"
        '{"type":"...", "value":"...", "source_text":"...", "product_ref": null}\n\n'
        f"User message: {t}\n"
        "Output:"
    )

def extract_structured_fact_by_rules(text: str) -> dict | None:
    t = (text or "").strip()
    if not t:
        return None

    product_ref = extract_product_ref_by_rules(t)

    # 1) product_price
    m = re.search(r"(?:价格|售价|定价)(?:是|为)?\s*([0-9]+(?:\.[0-9]+)?\s*(?:元|块|¥|人民币)?)", t)
    if m:
        value = m.group(1).strip()
        return {
            "type": "product_price",
            "value": value,
            "source_text": t,
            "product_ref": product_ref,
        }

    # 2) promo
    promo_patterns = [
        r"(满[0-9]+减[0-9]+)",
        r"([0-9]+件[0-9]+折)",
        r"([0-9]+折)",
        r"(买一送一)",
        r"(第二件半价)",
    ]
    for pat in promo_patterns:
        m = re.search(pat, t)
        if m:
            return {
                "type": "promo",
                "value": m.group(1).strip(),
                "source_text": t,
                "product_ref": product_ref,
            }

    # 3) stock_status
    stock_patterns = [
        (r"(现货)", "现货"),
        (r"(有货)", "有货"),
        (r"(缺货)", "缺货"),
        (r"(无货)", "无货"),
        (r"(售罄)", "售罄"),
        (r"(预售)", "预售"),
    ]
    for pat, normalized in stock_patterns:
        if re.search(pat, t):
            return {
                "type": "stock_status",
                "value": normalized,
                "source_text": t,
                "product_ref": product_ref,
            }

    # 4) shipping_policy
    shipping_patterns = [
        (r"(次日达)", "次日达"),
        (r"(当日达)", "当日达"),
        (r"(包邮)", "包邮"),
        (r"(免邮)", "免邮"),
        (r"([0-9]+天内发货)", None),
        (r"(48小时内发货)", "48小时内发货"),
    ]
    for pat, normalized in shipping_patterns:
        m = re.search(pat, t)
        if m:
            return {
                "type": "shipping_policy",
                "value": normalized or m.group(1).strip(),
                "source_text": t,
                "product_ref": product_ref,
            }

    # 5) product_feature
    feature_patterns = [
        r"主打([\u4e00-\u9fa5A-Za-z0-9\-]+)",
        r"特点是([\u4e00-\u9fa5A-Za-z0-9\-]+)",
        r"卖点是([\u4e00-\u9fa5A-Za-z0-9\-]+)",
        r"具有([\u4e00-\u9fa5A-Za-z0-9\-]+)性",
    ]
    for pat in feature_patterns:
        m = re.search(pat, t)
        if m:
            value = m.group(1).strip()
            if value:
                return {
                    "type": "product_feature",
                    "value": value,
                    "source_text": t,
                    "product_ref": product_ref,
                }

    return None


async def extract_structured_fact(text: str):
    t = (text or "").strip()
    if not t:
        return None

    if is_obviously_non_fact_message(t):
        return None

    rule_fact = extract_structured_fact_by_rules(t)
    if rule_fact is not None:
        return rule_fact

    prompt = build_extract_fact_prompt(t)

    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "user", "content": prompt}
        ],
    }

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()

    content = data.get("message", {}).get("content", "").strip()

    if content.upper() == "NONE":
        return None

    fact = try_parse_json_object(content)
    if fact is None:
        return None

    required_keys = {"type", "value", "source_text"}
    if not required_keys.issubset(fact):
        return None

    fact["type"] = normalize_fact_type(str(fact.get("type")).strip())
    fact["value"] = str(fact.get("value")).strip()
    fact["source_text"] = str(fact.get("source_text")).strip()

    if fact["type"] not in FACT_POLICIES:
        return None

    product_ref = fact.get("product_ref")
    if product_ref is not None:
        product_ref = str(product_ref).strip()
        fact["product_ref"] = product_ref if product_ref else None
    else:
        fact["product_ref"] = None


    if not fact["type"] or not fact["value"] or not fact["source_text"]:
        return None

    return fact

def try_parse_json_object(text: str) -> dict | None:
    if not text:
        return None

    raw = text.strip()

    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 3:
            raw = "\n".join(lines[1:-1]).strip()

    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = raw[start:end + 1]
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    return None

def is_fact_fresh(source_ts: str | None, freshness_days: int | None) -> bool:
    if not source_ts or freshness_days is None:
        return True

    try:
        source_time = time.strptime(source_ts, "%Y-%m-%dT%H:%M:%SZ")
        source_epoch = time.mktime(source_time)
        now_epoch = time.time()
        age_days = (now_epoch - source_epoch) / 86400
        return age_days <= freshness_days
    except Exception:
        return False

def should_supersede_existing_fact(fact_type: str | None) -> bool:
    return bool(get_fact_policy_value(fact_type, "single_value", False))


def infer_fact_scope(fact_type: str | None) -> str:
    return str(get_fact_policy_value(fact_type, "scope", "session"))


def infer_fact_slot(fact_type: str | None) -> str | None:
    slot = get_fact_policy_value(fact_type, "slot", None)
    return str(slot) if slot is not None else None


def infer_entity_id(session_id: str, fact: dict) -> str:
    fact_type = normalize_fact_type(fact.get("type"))
    entity_kind = get_fact_entity_kind(fact_type)

    if entity_kind == "default_product":
        product_ref = fact.get("product_ref")
        if product_ref:
            return f"{session_id}::product::{product_ref}"
        return f"{session_id}::default_product"

    return f"{session_id}::self"


def get_payload_slot(payload: dict) -> str | None:
    slot = payload.get("slot")
    if slot:
        return str(slot).strip()
    return infer_fact_slot(payload.get("type"))


def get_payload_entity_id(session_id: str, payload: dict) -> str | None:
    entity_id = payload.get("entity_id")
    if entity_id:
        return str(entity_id).strip()

    legacy_fact = {
        "type": payload.get("type"),
    }
    return infer_entity_id(session_id, legacy_fact)

async def kb_delete_facts_by_session_and_type(session_id: str, fact_type: str):
    now_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    body = {
        "payload": {
            "is_active": False,
            "deactivated_ts": now_ts,
        },
        "filter": {
            "must": [
                {"key": "session_id", "match": {"value": session_id}},
                {"key": "type", "match": {"value": fact_type}},
                {"key": "is_active", "match": {"value": True}},
            ]
        }
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{QDRANT_BASE_URL}/collections/{QDRANT_KB_COLLECTION}/points/payload",
            json=body,
        )
        r.raise_for_status()
        return r.json()

async def kb_delete_facts_by_session_entity_slot(
    session_id: str,
    entity_id: str,
    slot: str,
):
    if not session_id or not entity_id or not slot:
        return {"matched": 0, "updated": 0, "ids": []}

    scroll_body = {
        "limit": 200,
        "with_payload": True,
        "with_vector": False,
        "filter": {
            "must": [
                {"key": "session_id", "match": {"value": session_id}},
                {"key": "is_active", "match": {"value": True}},
            ]
        },
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{QDRANT_BASE_URL}/collections/{QDRANT_KB_COLLECTION}/points/scroll",
            json=scroll_body,
        )
        r.raise_for_status()
        data = r.json()

    points = data.get("result", {}).get("points", [])
    matched_ids = []

    for pt in points:
        pid = pt.get("id")
        payload = pt.get("payload") or {}

        old_slot = get_payload_slot(payload)
        old_entity_id = get_payload_entity_id(session_id, payload)

        if old_slot == slot and old_entity_id == entity_id:
            matched_ids.append(pid)

    if not matched_ids:
        return {"matched": 0, "updated": 0, "ids": []}

    now_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    async with httpx.AsyncClient(timeout=60) as client:
        for pid in matched_ids:
            r = await client.put(
                f"{QDRANT_BASE_URL}/collections/{QDRANT_KB_COLLECTION}/points/payload",
                json={
                    "payload": {
                        "is_active": False,
                        "deactivated_ts": now_ts,
                    },
                    "points": [pid],
                },
            )
            r.raise_for_status()

    return {
        "matched": len(matched_ids),
        "updated": len(matched_ids),
        "ids": matched_ids,
    }

async def kb_upsert_fact(session_id: str, fact: dict, vector: list[float] | None = None):
    fact["type"] = normalize_fact_type(fact.get("type"))
    fact["scope"] = infer_fact_scope(fact.get("type"))
    fact["slot"] = infer_fact_slot(fact.get("type"))
    fact["entity_id"] = infer_entity_id(session_id, fact)

    if should_supersede_existing_fact(fact.get("type")):
        await kb_delete_facts_by_session_entity_slot(
            session_id=session_id,
            entity_id=fact["entity_id"],
            slot=fact["slot"],
        )

    fact_text = json.dumps(fact, ensure_ascii=False)
    embed_text = fact.get("source_text") or fact_text

    if vector is not None:
        vec = vector
    else:
        vec = await ollama_embed(embed_text)

    point_id = str(uuid.uuid4())
    now_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    fact_type = fact.get("type")
    freshness_days = get_fact_freshness_days(fact_type)

    body = {
        "points": [
            {
                "id": point_id,
                "vector": vec,
                "payload": {
                    "kind": "structured_fact",
                    "session_id": session_id,
                    "type": fact.get("type"),
                    "scope": fact.get("scope"),
                    "slot": fact.get("slot"),
                    "entity_id": fact.get("entity_id"),
                    "product_ref": fact.get("product_ref"),
                    "value": fact.get("value"),
                    "source_text": fact.get("source_text"),
                    "text": fact_text,
                    "ts": now_ts,
                    "source_ts": now_ts,
                    "last_seen_ts": now_ts,
                    "freshness_days": freshness_days,
                    "is_active": True,
                }
            }
        ]
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.put(
            f"{QDRANT_BASE_URL}/collections/{QDRANT_KB_COLLECTION}/points",
            json=body,
        )
        r.raise_for_status()
        return {"id": point_id, "result": r.json()}

async def kb_touch_fact(point_id: str):
    now_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    body = {
        "payload": {
            "last_seen_ts": now_ts
        },
        "points": [point_id],
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{QDRANT_BASE_URL}/collections/{QDRANT_KB_COLLECTION}/points/payload",
            json=body,
        )
        r.raise_for_status()
        return r.json()

async def store_user_memory_if_needed(
    session_id: str,
    message: str,
    qvec: list[float],
    current_fact: dict | None,
):
    fact_extracted = False
    kb_upsert_ok = False
    kb_upsert_error = None
    memory_llm_ok = None
    skipped_reason = None

    if not (message.strip() and should_store_user_memory(message)):
        return {
            "memory_llm_ok": memory_llm_ok,
            "fact_ready": fact_extracted,
            "fact_extracted": fact_extracted,
            "kb_upsert_ok": kb_upsert_ok,
            "kb_upsert_error": kb_upsert_error,
            "skipped_reason": skipped_reason,
        }

    fact = current_fact
    if fact is not None:
        fact_extracted = True
        memory_llm_ok = True
    else:
        memory_llm_ok = False
        return {
            "memory_llm_ok": memory_llm_ok,
            "fact_ready": fact_extracted,
            "fact_extracted": fact_extracted,
            "kb_upsert_ok": kb_upsert_ok,
            "kb_upsert_error": kb_upsert_error,
            "skipped_reason": skipped_reason,
        }

    last_user_text = await get_last_user_message(session_id)
    if last_user_text == message:
        skipped_reason = "duplicate_message"
        return {
            "memory_llm_ok": memory_llm_ok,
            "fact_ready": fact_extracted,
            "fact_extracted": fact_extracted,
            "kb_upsert_ok": kb_upsert_ok,
            "kb_upsert_error": kb_upsert_error,
            "skipped_reason": skipped_reason,
        }

    if fact is not None:
        fact_extracted = True
        fact_type = normalize_fact_type(fact.get("type"))

        await qdrant_upsert_chat(
            session_id,
            "user",
            message,
            qvec,
            fact_type=fact_type,
        )

        try:
            await kb_upsert_fact(session_id, fact)
            kb_upsert_ok = True
        except Exception as e:
            kb_upsert_error = str(e)
    else:
        await qdrant_upsert_chat(session_id, "user", message, qvec)

    return {
        "memory_llm_ok": memory_llm_ok,
        "fact_ready": fact_extracted,
        "fact_extracted": fact_extracted,
        "kb_upsert_ok": kb_upsert_ok,
        "kb_upsert_error": kb_upsert_error,
        "skipped_reason": skipped_reason,
    }

@app.get("/health")
async def health():
    return {"ok": True, "model": OLLAMA_MODEL}

@app.post("/chat")
async def chat(req: ChatReq):
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "messages": []
    }
    if req.system:
        payload["messages"].append({"role": "system", "content": req.system})
    payload["messages"].append({"role": "user", "content": req.message})

    if req.temperature is not None:
        payload["options"] = {"temperature": req.temperature}

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()

    # Ollama /api/chat returns {"message": {"role": "...", "content": "..."}, ...}
    return {
        "model": OLLAMA_MODEL,
        "reply": data.get("message", {}).get("content", ""),
        "raw": data,
    }


@app.get("/test")
async def test():
    return {"ok": True}

@app.post("/chat_mem")
async def chat_mem(req: ChatReq):
    session_id = require_session_id(req.session_id)
    current_fact = None
    current_fact_type = None

    # 1) embedding 用户输入（只算向量，不写库）
    qvec = await ollama_embed(req.message)

    # 2) 先检索历史记忆（注意：此时还没把本次 user 写进去，所以不会“命中自己”）
    hits = await qdrant_search_chat(session_id, qvec, CHAT_TOP_K)
    candidate_hits = []
    for h in hits:
        p = h.get("payload") or {}
        txt = (p.get("text") or "").strip()
        role = p.get("role")

        if not txt:
            continue
        if role != "user":
            continue
        if not should_store_user_memory(txt):
            continue

        candidate_hits.append(h)

    if should_store_user_memory(req.message):
        try:
            current_fact = await extract_structured_fact(req.message)
            if current_fact is not None:
                current_fact_type = normalize_fact_type(current_fact.get("type"))
        except Exception as e:
            print("extract_structured_fact failed:", repr(e))
            current_fact = None
            current_fact_type = None

    if current_fact_type is not None:
        filtered_candidates = []
        for h in candidate_hits:
            p = h.get("payload") or {}
            old_fact_type = normalize_fact_type(p.get("fact_type"))

            # 旧消息如果带了 fact_type，而且和当前类型不一致，就丢掉
            if old_fact_type is not None and old_fact_type != current_fact_type:
                continue

            filtered_candidates.append(h)

        candidate_hits = filtered_candidates

    # ---- Dynamic gating (research-y) ----
    top1 = candidate_hits[0].get("score") if len(candidate_hits) >= 1 else None
    top2 = candidate_hits[1].get("score") if len(candidate_hits) >= 2 else None

    top1_score = top1
    top2_score = top2

    score_th = CHAT_BASE_THRESHOLD
    gap_ok = False
    th_ok = False

    if top1 is not None:
        th_ok = top1 >= CHAT_BASE_THRESHOLD

        if top2 is None:
            gap_ok = True
        elif top1 >= 0.92:
            gap_ok = True
        else:
            gap_ok = (top1 - top2) >= CHAT_GAP_THRESHOLD

    memory_allowed = bool(th_ok and gap_ok)

    if current_fact_type is not None and len(candidate_hits) == 0:
        memory_allowed = True

    has_current_fact = current_fact is not None
    # ------------------------------------

    # 3) 只有在“既不能安全使用历史记忆，又没有当前可写入事实”时，才 hard refusal
    if (not memory_allowed) and (not has_current_fact):
        memres = await store_user_memory_if_needed(
            session_id=session_id,
            message=req.message,
            qvec=qvec,
            current_fact=current_fact,
        )

        system_text = req.system or "你是一个有长期记忆的助手。"

        payload = {
            "model": OLLAMA_MODEL,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": req.message},
            ],
        }
        if req.temperature is not None:
            payload["options"] = {"temperature": req.temperature}

        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()

        reply = data.get("message", {}).get("content", "")

        return {
            "session_id": session_id,
            "model": OLLAMA_MODEL,
            "reply": reply,
            "refusal": False,
            "sources": [],
            "fact_debug": {
                "memory_llm_ok": memres["memory_llm_ok"],
                "fact_ready": memres["fact_ready"],
                "extracted": memres["fact_extracted"],
                "kb_upsert_ok": memres["kb_upsert_ok"],
                "kb_upsert_error": memres["kb_upsert_error"],
                "skipped_reason": memres["skipped_reason"],
            },
            "retrieval": {
                "query": req.message,
                "top_k": CHAT_TOP_K,
                "base_threshold": CHAT_BASE_THRESHOLD,
                "gap_threshold": CHAT_GAP_THRESHOLD,
                "top1_score": top1_score,
                "top2_score": top2_score,
                "score_threshold": score_th,
                "dynamic_threshold": score_th,
                "th_ok": th_ok,
                "gap_ok": gap_ok,
                "hit_count_raw": len(hits),
                "hit_count_candidates": len(candidate_hits),
                "hit_count_used": 0,
            },
        }


    # 4) 通过 gating：把 hits 里的 payload 做成可追溯 sources + 引用编号
    contexts = []  # 给 LLM 的文本块，带 [1][2]...
    sources = []   # 返回给用户的可追溯信息
    cite_no = 0

    for h in candidate_hits:
        score = h.get("score")
        point_id = h.get("id")
        p = h.get("payload") or {}

        txt = p.get("text") or ""
        role = p.get("role")
        ts = p.get("ts")

        if txt.strip() == "":
            continue
        if role != "user":
            continue
        if not should_store_user_memory(txt):
            continue

        if txt.strip() == req.message.strip():
            continue

        hit_fact_type = normalize_fact_type(p.get("fact_type"))
        if current_fact_type is not None and hit_fact_type is not None and hit_fact_type != current_fact_type:
            continue

        cite_no += 1
        contexts.append(f"[{cite_no}] {txt}")

        sources.append({
            "cite": cite_no,
            "point_id": point_id,
            "score": score,
            "collection": QDRANT_CHAT_COLLECTION,
            "role": role,
            "ts": ts,
            "text": txt,
        })

    # 5) 拼 system prompt
    system_text = req.system or "你是一个有长期记忆的助手。"
    if contexts:
        memory_block = (
            "以下是与当前问题相关的历史记忆（可能不完全准确）。"
            "如果使用了其中信息，请用 [编号] 标注引用：\n"
            + "\n".join(contexts[:CHAT_TOP_K])
        )
        system_text = system_text + "\n\n" + memory_block

    # 6) 调用 LLM
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": req.message},
        ],
    }
    if req.temperature is not None:
        payload["options"] = {"temperature": req.temperature}

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()

    reply = data.get("message", {}).get("content", "")

    # 7) 最后统一写入记忆：user + assistant（都只写一次）
    memres = await store_user_memory_if_needed(
        session_id=session_id,
        message=req.message,
        qvec=qvec,
        current_fact=current_fact,
    )

    #if reply.strip():
    #    avec = await ollama_embed(reply)
    #    await qdrant_upsert_chat(session_id, "assistant", reply, avec)

    return {
        "session_id": session_id,
        "model": OLLAMA_MODEL,
        "reply": reply,
        "refusal": False,
        "fact_debug": {
            "memory_llm_ok": memres["memory_llm_ok"],
            "fact_ready": memres["fact_ready"],
            "extracted": memres["fact_extracted"],
            "kb_upsert_ok": memres["kb_upsert_ok"],
            "kb_upsert_error": memres["kb_upsert_error"],
            "skipped_reason": memres["skipped_reason"],
        },
        "sources": sources[:CHAT_TOP_K],
        "retrieval": {
            "query": req.message,
            "top_k": CHAT_TOP_K,
            "base_threshold": CHAT_BASE_THRESHOLD,
            "gap_threshold": CHAT_GAP_THRESHOLD,
            "top1_score": top1_score,
            "top2_score": top2_score,
            "th_ok": th_ok,
            "gap_ok": gap_ok,
            "score_threshold": score_th,
            "hit_count_raw": len(hits),
            "hit_count_candidates": len(candidate_hits),
            "hit_count_used": len(sources[:CHAT_TOP_K]),
        }
    }


@app.post("/chat_mem_strict")
async def chat_mem_strict(req: ChatReq):
    """
    Legacy endpoint.
    Kept for comparison/debugging with the old fixed-threshold retrieval behavior.
    New development should happen in /chat_mem.
    """
    session_id = require_session_id(req.session_id)

    # NOTE:
    # This endpoint intentionally preserves the older fixed-threshold behavior.
    # Do not add new memory/fact features here unless we explicitly decide to revive it.

    # 1) embedding 用户输入
    qvec = await ollama_embed(req.message)

    # 2) 检索历史记忆
    hits = await qdrant_search_chat(session_id, qvec, CHAT_TOP_K)

    # 3) 阈值过滤
    memories = []
    kept = []  # 用于返回调试信息：point_id/score
    for h in hits:
        score = h.get("score")
        p = h.get("payload") or {}
        t = p.get("text")
        pid = h.get("id")

        if t and (score is not None) and (score >= CHAT_SCORE_THRESHOLD):
            memories.append(t)
            kept.append({"point_id": pid, "score": score})

    # 4) HARD REFUSAL：没有任何通过阈值的记忆 -> 不调用 LLM
    if not memories:
        return {
            "session_id": session_id,
            "model": OLLAMA_MODEL,
            "reply": "...",
            "refusal": True,
            "memory_used": [],
            "retrieval": {
                "query": req.message,
                "top_k": CHAT_TOP_K,
                "score_threshold": CHAT_SCORE_THRESHOLD,
                "hit_count_raw": len(hits),
                "hit_count_filtered": 0,
            }
        }

    # 5) 拼 system prompt
    system_text = req.system or "你是一个有长期记忆的助手。"
    memory_block = "以下是与当前问题相关的历史记忆（可能不完全准确）：\n" + "\n".join(
        [f"- {m}" for m in memories[:CHAT_TOP_K]]
    )
    system_text = system_text + "\n\n" + memory_block

    # 6) 调用 LLM
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": req.message},
        ],
    }
    if req.temperature is not None:
        payload["options"] = {"temperature": req.temperature}

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()

    reply = data.get("message", {}).get("content", "")

    # 7) 写入记忆：user + assistant（和你现在的 chat_mem 一样）
    if req.message.strip():
        await qdrant_upsert_chat(session_id, "user", req.message, qvec)

    if reply.strip():
        avec = await ollama_embed(reply)
        await qdrant_upsert_chat(session_id, "assistant", reply, avec)

    return {
        "session_id": session_id,
        "model": OLLAMA_MODEL,
        "reply": reply,
        "refusal": False,
        "memory_used": memories[:CHAT_TOP_K],
        "retrieval": {
            "query": req.message,
            "top_k": CHAT_TOP_K,
            "score_threshold": CHAT_SCORE_THRESHOLD,
            "hit_count_raw": len(hits),
            "hit_count_filtered": len(memories[:CHAT_TOP_K]),
            "kept": kept,
        }
}

@app.get("/debug/chat_mem/{session_id}")
async def debug_chat_mem(session_id: str, limit: int = 20):
    if not DEBUG_ROUTES_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")

    points = await qdrant_scroll_chat(session_id, limit=limit)
    items = []
    for p in points:
        payload = p.get("payload") or {}
        items.append({
            "point_id": p.get("id"),
            "role": payload.get("role"),
            "text": payload.get("text"),
            "fact_type": payload.get("fact_type"),
            "ts": payload.get("ts"),
        })
    return {
        "session_id": session_id,
        "collection": QDRANT_CHAT_COLLECTION,
        "count": len(items),
        "items": items,
    }
class DebugChatSearchReq(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=128)
    query: str = Field(..., min_length=1, max_length=4000)
    top_k: int | None = Field(default=None, ge=1, le=20)

class DebugFactOverwriteReq(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=128)
    message: str = Field(..., min_length=1, max_length=4000)

class DebugExtractFactReq(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)

@app.post("/debug/chat_mem_search")
async def debug_chat_mem_search(req: DebugChatSearchReq):
    if not DEBUG_ROUTES_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")

    top_k = req.top_k or CHAT_TOP_K
    qvec = await ollama_embed(req.query)
    hits = await qdrant_search_chat(req.session_id, qvec, top_k)

    out = []
    for h in hits:
        p = h.get("payload") or {}
        out.append({
            "score": h.get("score"),
            "point_id": h.get("id"),
            "role": p.get("role"),
            "fact_type": p.get("fact_type"),
            "text": p.get("text"),
            "ts": p.get("ts"),
        })

    return {
        "session_id": req.session_id,
        "top_k": top_k,
        "hits": out,
    }

@app.post("/debug/chat_mem_trace")
async def debug_chat_mem_trace(req: ChatReq):
    if not DEBUG_ROUTES_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")

    session_id = require_session_id(req.session_id)

    qvec = await ollama_embed(req.message)
    hits = await qdrant_search_chat(session_id, qvec, CHAT_TOP_K)

    candidate_hits = []
    for h in hits:
        p = h.get("payload") or {}
        txt = (p.get("text") or "").strip()
        role = p.get("role")

        if not txt:
            continue
        if role != "user":
            continue
        if not should_store_user_memory(txt):
            continue

        candidate_hits.append({
            "score": h.get("score"),
            "point_id": h.get("id"),
            "role": role,
            "fact_type": p.get("fact_type"),
            "text": txt,
            "ts": p.get("ts"),
        })

    current_fact = None
    current_fact_type = None

    if should_store_user_memory(req.message):
        try:
            current_fact = await extract_structured_fact(req.message)
            if current_fact is not None:
                current_fact_type = normalize_fact_type(current_fact.get("type"))
        except Exception:
            current_fact = None
            current_fact_type = None

        filtered_candidates = []

        for h in candidate_hits:
            txt = (h.get("text") or "").strip()
            old_fact_type = normalize_fact_type(h.get("fact_type"))

            # 1) 过滤掉和当前输入完全相同的 self-hit
            if txt == req.message.strip():
                continue

            # 2) 如果当前抽到了 fact 类型，就过滤掉类型不一致的历史消息
            if current_fact_type is not None and old_fact_type is not None and old_fact_type != current_fact_type:
                continue

            filtered_candidates.append(h)

    final_top1_score = filtered_candidates[0].get("score") if len(filtered_candidates) >= 1 else None
    final_top2_score = filtered_candidates[1].get("score") if len(filtered_candidates) >= 2 else None

    score_th = CHAT_BASE_THRESHOLD

    final_th_ok = (
        final_top1_score is not None and final_top1_score >= score_th
    )

    final_gap_ok = False
    if final_top1_score is not None:
        if final_top2_score is None:
            final_gap_ok = True
        elif final_top1_score >= 0.92:
            final_gap_ok = True
        else:
            final_gap_ok = (final_top1_score - final_top2_score) >= CHAT_GAP_THRESHOLD

    final_memory_allowed = bool(final_th_ok and final_gap_ok)
    memory_allowed_reason = "retrieval_passed" if final_memory_allowed else "none"

    # 和 /chat_mem 主逻辑保持一致：
    # 当前有 fact，但过滤后没有历史候选，也允许继续
    if current_fact_type is not None and len(filtered_candidates) == 0:
        final_memory_allowed = True
        memory_allowed_reason = "current_fact_without_history"

    return {
        "session_id": session_id,
        "query": req.message,
        "should_store_by_rule": should_store_user_memory(req.message),
        "current_fact": current_fact,
        "current_fact_type": current_fact_type,
        "retrieval": {
            "top_k": CHAT_TOP_K,
            "base_threshold": CHAT_BASE_THRESHOLD,
            "gap_threshold": CHAT_GAP_THRESHOLD,
            "hit_count_raw": len(hits),
            "hit_count_candidates": len(candidate_hits),
            "hit_count_after_type_filter": len(filtered_candidates),
            "top1_score": final_top1_score,
            "top2_score": final_top2_score,
            "score_threshold": score_th,
            "th_ok": final_th_ok,
            "gap_ok": final_gap_ok,
            "memory_allowed": final_memory_allowed,
            "memory_allowed_reason": memory_allowed_reason,
        },
        "candidate_hits": candidate_hits,
        "filtered_candidates": filtered_candidates,
    }

@app.post("/debug/extract_fact")
async def debug_extract_fact(req: DebugExtractFactReq):
    t = (req.message or "").strip()

    prompt = build_extract_fact_prompt(t)

    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "user", "content": prompt}
        ],
    }

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()

    raw_content = data.get("message", {}).get("content", "").strip()
    parsed = try_parse_json_object(raw_content)

    normalized = None
    if isinstance(parsed, dict):
        required_keys = {"type", "value", "source_text"}
        if required_keys.issubset(parsed):
            normalized = {
                "type": normalize_fact_type(str(parsed.get("type")).strip()),
                "value": str(parsed.get("value")).strip(),
                "source_text": str(parsed.get("source_text")).strip(),
            }

    return {
        "message": req.message,
        "raw_content": raw_content,
        "parsed": parsed,
        "normalized": normalized,
    }

class ChatLivestreamKBReq(BaseModel):
    session_id: str | None = Field(default=None, max_length=128)
    message: str = Field(..., min_length=1, max_length=4000)
    system: str | None = Field(default=None, max_length=4000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_k: int | None = Field(default=None, ge=1, le=20)
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)

@app.post("/debug/select_livestream_fact")
async def debug_select_livestream_fact(req: ChatLivestreamKBReq):
    if not DEBUG_ROUTES_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")

    top_k = req.top_k if req.top_k else KB_TOP_K

    best = await select_best_livestream_fact(
        message=req.message,
        session_id=req.session_id,
        top_k=top_k,
        score_threshold=req.score_threshold,
    )

    if best is None:
        return {
            "message": req.message,
            "session_id": req.session_id,
            "selected_fact_type": None,
            "score_threshold": req.score_threshold if req.score_threshold is not None else 0.65,
            "selected_hit": None,
        }

    hit = best.get("hit") or {}
    payload = hit.get("payload") or {}

    return {
        "message": req.message,
        "session_id": req.session_id,
        "selected_fact_type": best.get("fact_type"),
        "score_threshold": best.get("score_threshold"),
        "selected_hit": {
            "point_id": hit.get("id"),
            "score": hit.get("score"),
            "type": normalize_fact_type(payload.get("type")),
            "value": payload.get("value"),
            "source_text": payload.get("source_text"),
            "source_ts": payload.get("source_ts"),
            "last_seen_ts": payload.get("last_seen_ts"),
            "freshness_days": payload.get("freshness_days"),
            "is_active": payload.get("is_active"),
            "text": payload.get("text"),
        },
    }

@app.post("/debug/fact_overwrite_trace")
async def debug_fact_overwrite_trace(req: DebugFactOverwriteReq):
    if not DEBUG_ROUTES_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")

    session_id = require_session_id(req.session_id)

    should_store = should_store_user_memory(req.message)

    current_fact = None
    current_fact_type = None
    current_scope = None
    current_slot = None
    current_entity_id = None
    current_should_supersede = False

    if should_store:
        try:
            current_fact = await extract_structured_fact(req.message)
            if current_fact is not None:
                current_fact["type"] = normalize_fact_type(current_fact.get("type"))
                current_fact["scope"] = infer_fact_scope(current_fact.get("type"))
                current_fact["slot"] = infer_fact_slot(current_fact.get("type"))
                current_fact["entity_id"] = infer_entity_id(session_id, current_fact)

                current_fact_type = current_fact.get("type")
                current_scope = current_fact.get("scope")
                current_slot = current_fact.get("slot")
                current_entity_id = current_fact.get("entity_id")
                current_should_supersede = should_supersede_existing_fact(current_fact_type)
        except Exception as e:
            current_fact = None
            current_fact_type = None
            current_scope = None
            current_slot = None
            current_entity_id = None
            current_should_supersede = False

    trace_items = []

    # 不再用 kb_search，因为那是语义检索 + top_k，不适合做覆盖追踪
    scroll_body = {
        "limit": 200,
        "with_payload": True,
        "with_vector": False,
        "filter": {
            "must": [
                {"key": "session_id", "match": {"value": session_id}},
                {"key": "is_active", "match": {"value": True}},
            ]
        },
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{QDRANT_BASE_URL}/collections/{QDRANT_KB_COLLECTION}/points/scroll",
            json=scroll_body,
        )
        r.raise_for_status()
        data = r.json()

    points = data.get("result", {}).get("points", [])

    for pt in points:
        payload = pt.get("payload") or {}

        existing_type = normalize_fact_type(payload.get("type"))
        existing_scope = payload.get("scope")
        existing_slot = get_payload_slot(payload)
        existing_entity_id = get_payload_entity_id(session_id, payload)
        existing_is_active = payload.get("is_active", True)

        same_type = (existing_type == current_fact_type)
        same_scope = (existing_scope == current_scope)
        same_slot = (existing_slot == current_slot)
        same_entity_id = (existing_entity_id == current_entity_id)

        would_supersede = bool(
            current_should_supersede
            and existing_is_active
            and same_entity_id
            and same_slot
        )

        trace_items.append({
            "point_id": pt.get("id"),
            "type": existing_type,
            "scope": existing_scope,
            "slot": existing_slot,
            "entity_id": existing_entity_id,
            "value": payload.get("value"),
            "source_text": payload.get("source_text"),
            "source_ts": payload.get("source_ts"),
            "last_seen_ts": payload.get("last_seen_ts"),
            "freshness_days": payload.get("freshness_days"),
            "is_active": existing_is_active,
            "text": payload.get("text"),
            "same_type": same_type,
            "same_scope": same_scope,
            "same_slot": same_slot,
            "same_entity_id": same_entity_id,
            "would_supersede": would_supersede,
        })

    return {
        "session_id": session_id,
        "message": req.message,
        "should_store": should_store,
        "current_fact": current_fact,
        "current_fact_type": current_fact_type,
        "current_scope": current_scope,
        "current_slot": current_slot,
        "current_entity_id": current_entity_id,
        "current_should_supersede": current_should_supersede,
        "trace": trace_items,
    }

class ChatKBReq(BaseModel):
    session_id: str | None = Field(default=None, max_length=128)
    message: str = Field(..., min_length=1, max_length=4000)
    system: str | None = Field(default=None, max_length=4000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_k: int | None = Field(default=None, ge=1, le=20)
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    fact_type: str | None = Field(default=None, max_length=64)

class DebugKBSearchReq(BaseModel):
    session_id: str | None = Field(default=None, max_length=128)
    query: str = Field(..., min_length=1, max_length=4000)
    top_k: int | None = Field(default=None, ge=1, le=20)
    fact_type: str | None = Field(default=None, max_length=64)

class KBUpsertReq(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    id: str | None = Field(default=None, max_length=128)

async def kb_search(
    query: str,
    top_k: int,
    session_id: str | None = None,
    only_active: bool = False,
    fact_type: str | None = None,
):

    # 1. 生成 embedding
    qvec = await ollama_embed(query)

    # 2. 搜索 Qdrant
    body = {
        "query": qvec,
        "limit": top_k,
        "with_payload": True
    }

    must_filters = []

    if session_id is not None:
        must_filters.append(
            {"key": "session_id", "match": {"value": session_id}}
        )

    if only_active:
        must_filters.append(
            {"key": "is_active", "match": {"value": True}}
        )

    normalized_type = normalize_fact_type(fact_type)
    if normalized_type is not None:
        must_filters.append(
            {"key": "type", "match": {"value": normalized_type}}
        )

    if must_filters:
        body["filter"] = {"must": must_filters}

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{QDRANT_BASE_URL}/collections/{QDRANT_KB_COLLECTION}/points/query",
            json=body
        )
        r.raise_for_status()
        return r.json().get("result", {}).get("points", [])

async def kb_upsert(text: str, point_id: str | None = None):
    vec = await ollama_embed(text)
    if point_id is None:
        point_id = str(uuid.uuid4())

    body = {
        "points": [
            {
                "id": point_id,
                "vector": vec,
                "payload": {"text": text}
            }
        ]
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.put(
            f"{QDRANT_BASE_URL}/collections/{QDRANT_KB_COLLECTION}/points",
            json=body,
        )
        r.raise_for_status()
        return {"id": point_id, "result": r.json()}

async def select_best_livestream_fact(
    message: str,
    session_id: str | None,
    top_k: int,
    score_threshold: float | None = None,
):
    best = None

    hits = await kb_search(
        query=message,
        top_k=top_k,
        session_id=session_id,
        only_active=True,
    )

    for h in hits:
        payload = h.get("payload") or {}
        txt = payload.get("text") or ""
        score = h.get("score")
        fact_type = normalize_fact_type(payload.get("type"))
        is_active = payload.get("is_active")
        source_ts = payload.get("source_ts")
        freshness_days = payload.get("freshness_days")

        if fact_type not in LIVESTREAM_FACT_TYPES:
            continue
        if txt.strip() == "":
            continue
        if is_active is False:
            continue
        if not is_fact_fresh(source_ts, freshness_days):
            continue

        if score_threshold is not None:
            current_threshold = score_threshold
        else:
            current_threshold = get_livestream_score_threshold(fact_type)

        if score is None or score < current_threshold:
            continue

        candidate = {
            "fact_type": fact_type,
            "score": score,
            "hit": h,
            "score_threshold": current_threshold,
        }

        if best is None or score > best["score"]:
            best = candidate

    return best

@app.post("/kb_upsert")
async def kb_upsert_api(req: KBUpsertReq):
    return await kb_upsert(req.text, req.id)

@app.post("/debug/kb_search")
async def debug_kb_search(req: DebugKBSearchReq):
    if not DEBUG_ROUTES_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")

    session_id = require_session_id(req.session_id)
    top_k = req.top_k or KB_TOP_K
    wanted_type = normalize_fact_type(req.fact_type)

    hits = await kb_search(
        query=req.query,
        top_k=top_k,
        session_id=session_id,
        only_active=True,
        fact_type=wanted_type,
    )

    out = []
    for h in hits:
        payload = h.get("payload") or {}

        fact_type = normalize_fact_type(payload.get("type"))
        if wanted_type is not None and fact_type != wanted_type:
            continue

        out.append({
            "score": h.get("score"),
            "point_id": h.get("id"),
            "kind": payload.get("kind"),
            "type": fact_type,
            "scope": payload.get("scope"),
            "slot": payload.get("slot"),
            "entity_id": payload.get("entity_id"),
            "value": payload.get("value"),
            "source_text": payload.get("source_text"),
            "source_ts": payload.get("source_ts"),
            "last_seen_ts": payload.get("last_seen_ts"),
            "freshness_days": payload.get("freshness_days"),
            "is_active": payload.get("is_active"),
            "deactivated_ts": payload.get("deactivated_ts"),
            "text": payload.get("text"),
        })

    return {
        "session_id": session_id,
        "query": req.query,
        "top_k": top_k,
        "fact_type": wanted_type,
        "hits": out,
    }

@app.post("/chat_kb")
async def chat_kb(req: ChatKBReq):
    session_id = require_session_id(req.session_id)
    top_k = req.top_k if req.top_k else KB_TOP_K
    wanted_type = normalize_fact_type(req.fact_type)

    if req.score_threshold is not None:
        score_th = req.score_threshold
    elif wanted_type in LIVESTREAM_FACT_TYPES:
        score_th = get_livestream_score_threshold(wanted_type)
    else:
        score_th = KB_SCORE_THRESHOLD

    hits = await kb_search(
        query=req.message,
        top_k=top_k,
        session_id=session_id,
        only_active=True,
        fact_type=wanted_type,
    )

    raw_hit_count = len(hits)

    contexts = []
    sources = []  # 这里我们改名叫 sources（返回给用户的，可追溯）
    filtered_out = []
    cite_no = 0   # 引用编号：1,2,3...（只给“通过阈值”的命中编号，避免中间断号）

    for h in hits:
        point_id = h.get("id")  # ✅ 真实 Qdrant point id（uuid 或数字）
        payload = h.get("payload") or {}
        txt = payload.get("text") or ""
        score = h.get("score")
        kind = payload.get("kind")
        fact_type = normalize_fact_type(payload.get("type"))
        fact_value = payload.get("value")
        source_text = payload.get("source_text")
        source_ts = payload.get("source_ts")
        last_seen_ts = payload.get("last_seen_ts")
        freshness_days = payload.get("freshness_days")
        is_active = payload.get("is_active")

        if is_active is False:
            filtered_out.append({
                "point_id": point_id,
                "type": fact_type,
                "score": score,
                "reason": "inactive",
                "source_text": source_text,
                "is_active": is_active,
            })
            continue

        if not is_fact_fresh(source_ts, freshness_days):
            filtered_out.append({
                "point_id": point_id,
                "type": fact_type,
                "score": score,
                "reason": "stale",
                "source_text": source_text,
                "source_ts": source_ts,
                "freshness_days": freshness_days,
            })
            continue

        # 兼容旧 structured_fact：payload 里没有 value/source_text，只把 JSON 放在 text 里
        if kind == "structured_fact" and txt.strip().startswith("{"):
            try:
                legacy = json.loads(txt)
                kind = "structured_fact_legacy"
                fact_type = normalize_fact_type(legacy.get("type"))
                fact_value = legacy.get("value")
                source_text = legacy.get("source_text")
            except Exception:
                pass

        if wanted_type is not None and fact_type != wanted_type:
            filtered_out.append({
                "point_id": point_id,
                "type": fact_type,
                "score": score,
                "reason": "type_mismatch",
                "source_text": source_text,
            })
            continue

        if (txt is None) or (txt.strip() == ""):
            filtered_out.append({
                "point_id": point_id,
                "type": fact_type,
                "score": score,
                "reason": "empty_text",
                "source_text": source_text,
            })
            continue

        if (score is None) or (score < score_th):
            filtered_out.append({
                "point_id": point_id,
                "type": fact_type,
                "score": score,
                "reason": "below_score_threshold",
                "source_text": source_text,
                "score_threshold": score_th,
            })
            continue

        await kb_touch_fact(point_id)

        cite_no += 1
        contexts.append(f"[{cite_no}] {txt}")
        sources.append({
            "cite": cite_no,
            "point_id": point_id,
            "score": score,
            "collection": QDRANT_KB_COLLECTION,
            "kind": payload.get("kind"),
            "type": payload.get("type"),
            "scope": payload.get("scope"),
            "slot": payload.get("slot"),
            "entity_id": payload.get("entity_id"),
            "value": payload.get("value"),
            "source_text": payload.get("source_text"),
            "source_ts": payload.get("source_ts"),
            "last_seen_ts": payload.get("last_seen_ts"),
            "freshness_days": payload.get("freshness_days"),
            "is_active": payload.get("is_active"),
            "text": payload.get("text"),
        })

    filtered_hit_count = len(sources)

    context_block = "\n".join(contexts) if contexts else "(知识库未命中)"
    # HARD REFUSAL: after threshold filtering, no usable KB context -> do not call LLM

    if not sources:
        return {
            "model": OLLAMA_MODEL,
            "reply": "...",
            "sources": [],
            "refusal": True,
            "retrieval": {
                "query": req.message,
                "top_k": top_k,
                "fact_type": wanted_type,
                "session_id": req.session_id,
                "hit_count_raw": raw_hit_count,
                "hit_count_filtered": 0,
                "filtered_out": filtered_out,
            }
        }

    base_system = req.system or "你是一个检索增强助手。"

    system_prompt = (
        f"{base_system}\n"
        "请基于知识库回答问题，并在使用知识时用 [编号] 标注引用。\n\n"
        f"【知识库】\n{context_block}\n\n"
        "【用户问题】"
    )

    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": req.message}
        ]
    }

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()

    return {
        "model": OLLAMA_MODEL,
        "reply": data.get("message", {}).get("content", ""),
        "sources": sources,
        "refusal": False,
        "retrieval": {
            "query": req.message,
            "top_k": top_k,
            "score_threshold": score_th,
            "fact_type": wanted_type,
            "session_id": session_id,
            "hit_count_raw": raw_hit_count,
            "hit_count_filtered": filtered_hit_count,
            "filtered_out": filtered_out,
        }
    }


@app.post("/chat_livestream_kb")
async def chat_livestream_kb(req: ChatLivestreamKBReq):
    session_id = require_session_id(req.session_id)
    top_k = req.top_k if req.top_k else KB_TOP_K

    best = await select_best_livestream_fact(
        message=req.message,
        session_id=session_id,
        top_k=top_k,
        score_threshold=req.score_threshold,
    )

    default_score_threshold = req.score_threshold if req.score_threshold is not None else 0.65

    if best is None:
        return {
            "model": OLLAMA_MODEL,
            "reply": "我目前没有检索到足够可靠的直播知识来回答这个问题。",
            "sources": [],
            "refusal": True,
            "routed_fact_type": None,
            "routing": {
                "candidate_fact_types": sorted(LIVESTREAM_FACT_TYPES),
                "selected_score": None,
                "selected_point_id": None,
            },
            "retrieval": {
                "query": req.message,
                "top_k": top_k,
                "score_threshold": default_score_threshold,
                "session_id": req.session_id,
                "selected_fact_type": None,
            },
        }

    routed_fact_type = best.get("fact_type")
    selected_hit = best.get("hit") or {}

    delegated_req = ChatKBReq(
        session_id=session_id,
        message=req.message,
        system=req.system,
        temperature=req.temperature,
        top_k=top_k,
        score_threshold=req.score_threshold if req.score_threshold is not None else best.get("score_threshold"),
        fact_type=routed_fact_type,
    )

    result = await chat_kb(delegated_req)
    result["routed_fact_type"] = routed_fact_type
    result["routing"] = {
        "candidate_fact_types": sorted(LIVESTREAM_FACT_TYPES),
        "selected_score": best.get("score"),
        "selected_point_id": selected_hit.get("id"),
    }

    return result



class RetailOpsKbReq(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    entity_id: str | None = Field(default="store_A", max_length=128)
    top_k: int | None = Field(default=5, ge=1, le=10)


def normalize_retail_entity_id(entity_id: str | None) -> str:
    return (entity_id or "store_A").strip().lower().replace(" ", "_")


def _contains_retail_term(
    query: str,
    terms: list[str],
) -> bool:
    return any(
        re.search(
            rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])",
            query,
        )
        for term in terms
    )


def infer_retail_slots(message: str) -> list[str]:
    q = (message or "").lower()
    slots: list[str] = []

    if _contains_retail_term(
        q,
        [
            "visibility",
            "entry",
            "search",
            "ranking",
            "rank",
            "exposure",
            "曝光",
            "搜索",
            "排名",
            "入店",
        ],
    ):
        slots.extend([
            "visibility_entry_profile",
            "single_metric_attribution_guard",
        ])

    if _contains_retail_term(
        q,
        [
            "promotion",
            "activity",
            "promo",
            "saturated",
            "活动",
            "促销",
            "补贴",
        ],
    ):
        slots.append("activity_lever_profile")

    if _contains_retail_term(
        q,
        [
            "conversion",
            "aov",
            "average order",
            "transaction",
            "order",
            "orders",
            "ordered",
            "payment",
            "转化",
            "客单价",
            "成交",
            "订单",
            "支付",
        ],
    ):
        slots.extend([
            "transaction_conversion_profile",
            "single_metric_attribution_guard",
        ])

    if _contains_retail_term(
        q,
        [
            "growth",
            "grew",
            "recover",
            "recovered",
            "recovery",
            "增长",
            "恢复",
        ],
    ):
        slots.extend([
            "transaction_conversion_profile",
            "single_metric_attribution_guard",
            "activity_lever_profile",
        ])

    if _contains_retail_term(
        q,
        [
            "sku",
            "product mix",
            "category",
            "商品",
            "品类",
            "产品结构",
        ],
    ):
        slots.append("top3_sku_product_mix_note")

    deduped = []
    seen = set()

    for slot in slots:
        if slot not in seen:
            deduped.append(slot)
            seen.add(slot)

    return deduped


def is_unsupported_retail_scope(
    message: str,
    entity_id: str | None,
) -> str | None:
    q = (message or "").lower()
    eid = normalize_retail_entity_id(entity_id)
    try:
        window = parse_retail_window(message, DEMO1_WINDOW)
    except ValueError as exc:
        return str(exc)
    if (window.period_start < DEMO1_WINDOW.period_start
            or window.period_end > DEMO1_WINDOW.period_end):
        return (
            "Demo 1 supports Store A evidence "
            "from February through April 2026."
        )

    if any(entity != "store_a" for entity in extract_retail_entity_ids(message)):
        return "The current retail demo only supports Store A facts."

    if _contains_retail_term(
        q,
        [
            "48 stores",
            "all stores",
            "cross-store",
            "compare stores",
            "across stores",
            "全部门店",
            "跨店",
            "所有门店",
        ],
    ):
        return (
            "The Demo 1 Store A endpoint supports only "
            "Store A month-over-month retail facts. "
            "Use /chat_retail_ops_demo2_kb for the "
            "implemented Demo 2 same-period B-F diagnostic. "
            "Pairwise comparability gating and full 48-store "
            "automation are not implemented."
        )

    causal_terms = [
        "cause",
        "causes",
        "caused",
        "causing",
        "causal",
        "causation",
        "because of",
        "attribute to",
        "attributed to",
        "归因",
        "证明",
        "导致",
    ]
    attribution_warning_terms = [
        "should",
        "should not",
        "alone",
        "only",
        "whether",
        "是否",
        "能否",
        "该不该",
        "是不是",
        "单独",
        "仅仅",
        "只",
    ]

    is_causal_query = _contains_retail_term(
        q,
        causal_terms,
    )
    is_attribution_warning_query = (
        _contains_retail_term(
            q,
            attribution_warning_terms,
        )
    )

    if (
        is_causal_query
        and not is_attribution_warning_query
    ):
        return (
            "The current retail demo supports cautious "
            "interpretation, not causal attribution."
        )

    if eid not in {"", "store_a"}:
        return (
            "The current retail demo only supports "
            "Store A facts."
        )

    return None


async def qdrant_scroll_retail_slot(entity_id_norm: str, slot: str, *, window: RetailWindow, limit: int = 5):
    body = {
        "limit": limit,
        "with_payload": True,
        "with_vectors": False,
        "filter": {
            "must": [
                {"key": "domain", "match": {"value": "retail_ops"}},
                {"key": "entity_id_norm", "match": {"value": entity_id_norm}},
                {"key": "slot", "match": {"value": slot}},
                {"key": "is_active", "match": {"value": True}},
                *[{"key": field, "match": {"value": getattr(window, field)}}
                  for field in ("period_start", "period_end", "period_label", "period_granularity")],
            ]
        },
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{QDRANT_BASE_URL}/collections/{QDRANT_KB_COLLECTION}/points/scroll",
            json=body,
        )
        r.raise_for_status()
        return r.json().get("result", {}).get("points", [])


async def qdrant_query_retail(message: str, entity_id_norm: str, *, window: RetailWindow, limit: int = 5):
    query_vector = await ollama_embed(message)

    body = {
        "vector": query_vector,
        "limit": limit,
        "with_payload": True,
        "filter": {
            "must": [
                {"key": "domain", "match": {"value": "retail_ops"}},
                {"key": "entity_id_norm", "match": {"value": entity_id_norm}},
                {"key": "is_active", "match": {"value": True}},
                *[{"key": field, "match": {"value": getattr(window, field)}}
                  for field in ("period_start", "period_end", "period_label", "period_granularity")],
            ]
        },
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{QDRANT_BASE_URL}/collections/{QDRANT_KB_COLLECTION}/points/search",
            json=body,
        )
        r.raise_for_status()
        return r.json().get("result", [])


def retail_answer_from_points(points: list[dict], *, entity_ids: set[str],
                              required_slots: set[str], window: RetailWindow,
                              limit: int, min_score: float | None = None) -> dict:
    points, reason = select_retail_points(
        points, entity_ids=entity_ids, required_slots=required_slots,
        window=window, limit=limit, min_score=min_score,
    )
    if reason:
        return {"supported": False, "answer": reason, "facts": []}
    facts = []
    answer_parts = []

    for p in points:
        payload = p.get("payload") or {}
        value = payload.get("value")
        slot = payload.get("slot")
        confidence = payload.get("confidence")
        source_path = payload.get("source_path")
        limitations = payload.get("limitations") or []

        if value:
            answer_parts.append(f"- {value}")

        facts.append({
            "value": value,
            "entity_id": payload.get("entity_id"),
            "period_start": payload.get(
                "period_start"
            ),
            "period_end": payload.get(
                "period_end"
            ),
            "period_label": payload.get(
                "period_label"
            ),
            "period_granularity": payload.get(
                "period_granularity"
            ),
            "slot": slot,
            "confidence": confidence,
            "source_fields": (
                payload.get("source_fields")
                or []
            ),
            "observed_values": (
                payload.get("observed_values")
                or {}
            ),
            "calculation": payload.get(
                "calculation"
            ),
            "source_path": source_path,
            "supporting_source_paths": (
                payload.get(
                    "supporting_source_paths"
                )
                or []
            ),
            "lineage_path": payload.get(
                "lineage_path"
            ),
            "limitations": limitations,
            "score": p.get("score"),
        })

    if not answer_parts:
        return {
            "supported": False,
            "answer": "No supported retail memory fact was found for this question.",
            "facts": [],
        }

    limitation_set = []
    for fact in facts:
        for item in fact.get("limitations") or []:
            if item not in limitation_set:
                limitation_set.append(item)

    if limitation_set:
        answer_parts.append("")
        answer_parts.append("Interpretation limits:")
        for item in limitation_set:
            answer_parts.append(f"- {item}")

    return {
        "supported": True,
        "answer": "\n".join(answer_parts),
        "facts": facts,
    }




class RetailOpsDemo2KbReq(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    entity_id: str | None = Field(default=None, max_length=128)
    top_k: int | None = Field(default=5, ge=1, le=25)


def normalize_demo2_retail_entity_id(entity_id: str | None) -> str:
    raw = (entity_id or "").strip().lower().replace(" ", "_")

    if raw in {"b", "c", "d", "e", "f"}:
        return f"store_{raw}"

    return raw


def extract_demo2_retail_entity_ids(message: str) -> list[str]:
    return extract_retail_entity_ids(message)


def is_demo2_cross_store_query(message: str) -> bool:
    q = (message or "").lower()

    if len(
        extract_demo2_retail_entity_ids(message)
    ) >= 2:
        return True

    return _contains_retail_term(
        q,
        [
            "cross-store",
            "cross store",
            "compare stores",
            "across stores",
            "stores b-f",
            "stores b to f",
            "b-f",
            "多店",
            "跨店",
            "对比门店",
            "比较门店",
        ],
    )


def is_unsupported_demo2_retail_scope(message: str, entity_id: str | None) -> str | None:
    q = (message or "").lower()
    eid = resolve_retail_entity_id(message, entity_id)
    try:
        window = parse_retail_window(message, DEMO2_WINDOW)
    except ValueError as exc:
        return str(exc)
    if window != DEMO2_WINDOW:
        return (
            "Demo 2 supports the March 2026 "
            "B-F diagnostic evidence."
        )

    message_entity_ids = (
        extract_demo2_retail_entity_ids(message)
    )
    supported_entity_ids = {
        "store_b",
        "store_c",
        "store_d",
        "store_e",
        "store_f",
    }

    if eid and eid not in supported_entity_ids:
        return "The supplied entity_id is outside the Demo 2 B-F dataset."

    if any(
        message_entity_id
        not in supported_entity_ids
        for message_entity_id
        in message_entity_ids
    ):
        return (
            "Demo 2 currently supports Store B, "
            "Store C, Store D, Store E, and "
            "Store F facts."
        )

    if (
        eid in supported_entity_ids
        and message_entity_ids
        and eid not in message_entity_ids
    ):
        return (
            "The store named in the message "
            "does not match the supplied entity_id."
        )

    if _contains_retail_term(
        q,
        [
            "48 stores",
            "all 48",
            "all stores",
            "全部门店",
            "所有门店",
            "48家",
        ],
    ):
        return "Demo 2 currently supports only the anonymized B-F same-period diagnostic sample, not all 48 stores."

    if _contains_retail_term(
        q,
        [
            "strategy transfer",
            "transfer strategy",
            "transfer promotion",
            "transfer activity",
            "copy strategy",
            "copy promotion",
            "copy activity",
            "activity strategy be transferred",
            "promotion strategy be transferred",
            "can store b's activity strategy be transferred",
            "迁移策略",
            "转移策略",
            "复制策略",
            "复制活动",
            "照搬活动",
            "照搬策略",
            "活动策略能不能迁移",
            "活动策略是否可以迁移",
        ],
    ):
        return (
            "Demo 2 supports diagnostic evidence and comparison limits, "
            "not pairwise strategy-transfer approval or final operating recommendations."
        )

    if _contains_retail_term(
        q,
        [
            "best store",
            "worst store",
            "which store is best",
            "which store should",
            "more subsidy",
            "receive subsidy",
            "ranking of stores",
            "最好",
            "最差",
            "哪家最好",
            "哪家最差",
            "应该补贴",
            "加补贴",
            "经营建议",
        ],
    ):
        return "Demo 2 supports cautious diagnostic comparison, not best-store ranking or final operating recommendations."

    if is_demo2_cross_store_query(message):
        if eid and not message_entity_ids:
            return "Name the stores to compare when supplying entity_id."
        return None

    if eid not in {"store_b", "store_c", "store_d", "store_e", "store_f"}:
        return "Demo 2 currently supports Store B, Store C, Store D, Store E, and Store F facts."

    return None


def load_demo2_retail_facts() -> list[dict]:
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent
    if root.name == "api":
        root = root.parent
    path = root / "retail_ops/outputs/generated_demo2_retail_memory_facts.json"

    return json.loads(path.read_text(encoding="utf-8"))


def demo2_retail_facts_to_points(facts: list[dict]) -> list[dict]:
    return [
        {
            "score": None,
            "payload": fact,
        }
        for fact in facts
    ]


# Endpoint-level heuristic for the Qdrant-backed Demo 1 vector fallback.
# The deterministic Demo 2 endpoint above does not use vector retrieval.
# This is separate from the offline exploratory reference reported in
# retail_ops/outputs/retrieval_threshold_summary.md and is not an answer-decision rule.
RETAIL_DEMO1_VECTOR_FALLBACK_MIN_SCORE = 0.45


@app.post("/chat_retail_ops_demo2_kb")
async def chat_retail_ops_demo2_kb(req: RetailOpsDemo2KbReq):
    unsupported_reason = is_unsupported_demo2_retail_scope(req.message, req.entity_id)

    if unsupported_reason:
        return {
            "supported": False,
            "answer": unsupported_reason,
            "facts": [],
        }

    slots = infer_retail_slots(req.message)
    is_cross_store = is_demo2_cross_store_query(req.message)

    if not slots and not is_cross_store:
        return {
            "supported": False,
            "answer": "No supported Demo 2 retail memory fact was found for this question.",
            "facts": [],
        }

    facts = load_demo2_retail_facts()

    if is_cross_store:
        message_entity_ids = (
            extract_demo2_retail_entity_ids(
                req.message
            )
        )
        target_entity_ids = {
            entity_id
            for entity_id in message_entity_ids
            if entity_id
            in {
                "store_b",
                "store_c",
                "store_d",
                "store_e",
                "store_f",
            }
        }

        if not target_entity_ids:
            target_entity_ids = {
                "store_b",
                "store_c",
                "store_d",
                "store_e",
                "store_f",
            }

        requested_slots = [
            slot
            for slot in slots
            if slot
            != "single_metric_attribution_guard"
        ]
        selected_slots = set(
            requested_slots
            or ["single_metric_attribution_guard"]
        )

        selected_facts = [
            fact
            for fact in facts
            if isinstance(fact, dict)
            and isinstance(fact.get("entity_id"), str)
            and (
                fact.get("entity_id", "").lower()
                in target_entity_ids
            )
            and isinstance(fact.get("slot"), str)
            and fact["slot"] in selected_slots
        ]
    else:
        entity_id_norm = resolve_retail_entity_id(req.message, req.entity_id)
        target_entity_ids = {entity_id_norm}
        selected_slots = set(slots)

        selected_facts = [
            fact
            for fact in facts
            if isinstance(fact, dict)
            and isinstance(fact.get("entity_id"), str)
            and fact["entity_id"].lower() == entity_id_norm
            and isinstance(fact.get("slot"), str)
            and fact["slot"] in slots
        ]

    points = demo2_retail_facts_to_points(selected_facts)
    result = retail_answer_from_points(
        points, entity_ids=target_entity_ids, required_slots=selected_slots,
        window=parse_retail_window(req.message, DEMO2_WINDOW), limit=req.top_k or 5,
    )

    result["demo_scope"] = "demo2_same_period_b_f_diagnostic"
    result["retrieval_mode"] = "not_used"
    result["selection_mode"] = "deterministic_entity_slot_filter"
    return result



@app.post("/chat_retail_ops_kb")
async def chat_retail_ops_kb(req: RetailOpsKbReq):
    unsupported_reason = is_unsupported_retail_scope(req.message, req.entity_id)
    if unsupported_reason:
        return {
            "supported": False,
            "answer": unsupported_reason,
            "facts": [],
        }

    entity_id_norm = normalize_retail_entity_id(req.entity_id)
    slots = infer_retail_slots(req.message)

    if not slots:
        return {
            "supported": False,
            "answer": (
                "No supported retail evidence class "
                "was found for this question."
            ),
            "facts": [],
        }

    window = parse_retail_window(req.message, DEMO1_WINDOW)
    limit = req.top_k or 5
    if len(slots) > limit:
        return {"supported": False, "answer": f"The requested evidence requires {len(slots)} facts; increase top_k to at least {len(slots)}.", "facts": []}

    slot_points = []
    for slot in slots:
        slot_points.extend(
            await qdrant_scroll_retail_slot(
                entity_id_norm=entity_id_norm,
                slot=slot,
                window=window,
                limit=2,
            )
        )

    if slot_points:
        return retail_answer_from_points(
            slot_points, entity_ids={entity_id_norm}, required_slots=set(slots),
            window=window, limit=limit,
        )

    vector_points = await qdrant_query_retail(
        message=req.message,
        entity_id_norm=entity_id_norm,
        window=window,
        limit=limit + 1,
    )

    if not vector_points:
        return {
            "supported": False,
            "answer": "No supported retail memory fact was found for this question.",
            "facts": [],
        }

    return retail_answer_from_points(
        vector_points, entity_ids={entity_id_norm}, required_slots=set(slots),
        window=window, limit=limit, min_score=RETAIL_DEMO1_VECTOR_FALLBACK_MIN_SCORE,
    )
