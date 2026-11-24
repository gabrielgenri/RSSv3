import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from html import unescape
import feedparser
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ARG_TZ = timezone(timedelta(hours=-3))
FEEDS_PATH = "data/feeds_argentina_larioja.json"
OUT_PATH = "data/noticias_consolidadas.json"
MAX_ITEMS_TOTAL = 600
MAX_VERSIONS = 30
MAX_SYNTHESIS_TOTAL = 60

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
}


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    clean = re.sub(r"<[^>]+>", " ", raw)
    clean = unescape(clean)
    return re.sub(r"\s+", " ", clean).strip()


def classify_topic(text: str, rules: dict, default_topic: str) -> tuple[str, list[str]]:
    haystack = text.lower()
    best_topic = default_topic
    best_score = 0
    matched_tags = []

    for topic, keywords in rules.items():
        hits = [kw for kw in keywords if re.search(r"\b" + re.escape(kw.lower()) + r"\b", haystack)]
        if hits:
            matched_tags.append(topic.split(",")[0].strip())
            for h in hits[:2]:
                matched_tags.append(h.title())
        if len(hits) > best_score:
            best_score = len(hits)
            best_topic = topic

    dedup_tags = list(dict.fromkeys(matched_tags))[:6]
    return best_topic, dedup_tags


def parse_entry_date(entry, fallback_iso: str) -> str:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        dt_struct = getattr(entry, attr, None)
        if dt_struct:
            try:
                dt_utc = datetime(*dt_struct[:6], tzinfo=timezone.utc)
                return dt_utc.astimezone(ARG_TZ).isoformat()
            except Exception:
                pass
    return fallback_iso


def fetch_feed_xml(url: str, timeout: int = 15):
    resp = requests.get(url, headers=HEADERS, timeout=timeout, verify=False)
    resp.raise_for_status()
    return resp.content


def build_cross_channel_synthesis(items: list, run_id: str, run_iso: str) -> list:
    by_topic = defaultdict(list)
    for it in items:
        tema = it.get("tema") or "Política y Gestión"
        by_topic[tema].append(it)

    syntheses = []
    for tema, group in by_topic.items():
        distinct_sources = {}
        for it in group:
            fid = it.get("fuente_id") or it.get("fuente_nombre")
            if fid and fid not in distinct_sources:
                distinct_sources[fid] = {
                    "id": fid,
                    "nombre": it.get("fuente_nombre", "Fuente"),
                    "ambito": it.get("ambito", "Nacional"),
                }

        if len(distinct_sources) < 2:
            continue

        selected_items = []
        used_sources = set()
        for it in group:
            fid = it.get("fuente_id") or it.get("fuente_nombre")
            if fid not in used_sources:
                selected_items.append(it)
                used_sources.add(fid)
            if len(selected_items) >= 4:
                break

        source_names = [s["nombre"] for s in list(distinct_sources.values())[:4]]
        joined_sources = ", ".join(source_names[:-1]) + " y " + source_names[-1] if len(source_names) > 1 else source_names[0]

        lead_titles = [f"«{it.get('titulo', '')}» ({it.get('fuente_nombre', '')})" for it in selected_items[:3]]
        resumen_original = (
            f"Cruces temáticos en {tema} a partir de {joined_sources}. "
            f"Las publicaciones recientes vinculadas bajo esta taxonomía incluyen: {'; '.join(lead_titles)}. "
            f"Al contrastar estos aportes de distintos canales, se configura un panorama articulado que conecta "
            f"las agendas locales y nacionales registradas en la corrida."
        )

        tag_pool = ["Síntesis"]
        for it in selected_items:
            for tg in (it.get("etiquetas") or []):
                if tg not in tag_pool:
                    tag_pool.append(tg)

        sin_id = hashlib.sha1(f"{run_id}::{tema}".encode("utf-8")).hexdigest()[:12]
        syntheses.append({
            "id": f"sin-{sin_id}",
            "tema": tema,
            "titulo": f"Síntesis multicanal · {tema} ({len(distinct_sources)} canales vinculados)",
            "resumen_original": resumen_original,
            "fuentes_vinculadas": list(distinct_sources.values())[:5],
            "notas_referenciadas": [
                {
                    "titulo": it.get("titulo", ""),
                    "enlace": it.get("enlace", ""),
                    "fuente_nombre": it.get("fuente_nombre", ""),
                }
                for it in selected_items
            ],
            "etiquetas": tag_pool[:6],
            "fecha_generacion": run_iso,
            "version_id": run_id,
        })

    syntheses.sort(key=lambda x: len(x.get("fuentes_vinculadas", [])), reverse=True)
    return syntheses


def main():
    now_arg = datetime.now(ARG_TZ)
    run_iso = now_arg.isoformat()
    run_id = now_arg.strftime("%Y-%m-%d-%H%M")
    run_label = now_arg.strftime("%d/%m/%Y · %H:%M")

    with open(FEEDS_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    rules = config.get("metadata", {}).get("reglas_clasificacion", {})
    fuentes = [src for src in config.get("fuentes", []) if src.get("activo", True)]

    existing = {"items": [], "versiones_disponibles": [], "sintesis_originales": []}
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    existing = loaded
        except Exception:
            pass

    existing_items = existing.get("items", [])
    seen_keys = set()
    for item in existing_items:
        key = item.get("enlace") or item.get("id")
        if key:
            seen_keys.add(key)

    new_items = []
    fuentes_ok = 0
    fuentes_error = 0

    for src in fuentes:
        url = src.get("url_rss")
        if not url:
            continue
        try:
            raw_xml = fetch_feed_xml(url)
            parsed = feedparser.parse(raw_xml)
            if parsed.bozo and not parsed.entries:
                raise RuntimeError(f"Parse error: {parsed.bozo_exception}")
            fuentes_ok += 1

            for entry in parsed.entries[:25]:
                title = strip_html(getattr(entry, "title", "") or "")
                link = (getattr(entry, "link", "") or "").strip()
                summary_raw = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
                summary = strip_html(summary_raw)[:340]
                if not title and not link:
                    continue

                dedup_key = link if link else f"{src['id']}::{title}"
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                item_id = hashlib.sha1(dedup_key.encode("utf-8")).hexdigest()[:12]
                tema, extra_tags = classify_topic(
                    f"{title} {summary}",
                    rules,
                    src.get("tema_por_defecto", "Política y Gestión"),
                )
                tags = list(dict.fromkeys([src.get("ambito", "Nacional"), src.get("tipo_entidad", "Medio")] + extra_tags))[:6]

                new_items.append({
                    "id": item_id,
                    "titulo": title or "(Sin título)",
                    "resumen": summary or "Sin resumen disponible en el canal RSS.",
                    "enlace": link,
                    "fecha_publicacion": parse_entry_date(entry, run_iso),
                    "version_id": run_id,
                    "fuente_id": src["id"],
                    "fuente_nombre": src["nombre"],
                    "ambito": src.get("ambito", "Nacional"),
                    "tipo_entidad": src.get("tipo_entidad", "Medio de Comunicación"),
                    "tema": tema,
                    "etiquetas": tags,
                })
        except Exception as exc:
            fuentes_error += 1
            print(f"[WARN] {src.get('nombre')} ({url}): {exc}")

    merged_items = new_items + existing_items
    merged_items.sort(key=lambda x: x.get("fecha_publicacion", ""), reverse=True)
    merged_items = merged_items[:MAX_ITEMS_TOTAL]

    # Vincular por taxonomía entre canales (priorizando noticias nuevas y recientes)
    pool_for_synthesis = new_items if len(new_items) >= 2 else merged_items[:80]
    current_syntheses = build_cross_channel_synthesis(pool_for_synthesis, run_id, run_iso)
    old_syntheses = [s for s in existing.get("sintesis_originales", []) if s.get("version_id") != run_id]
    merged_syntheses = (current_syntheses + old_syntheses)[:MAX_SYNTHESIS_TOTAL]

    version_entry = {
        "id": run_id,
        "etiqueta": f"{run_label} ({len(new_items)} nuevas · {len(current_syntheses)} síntesis)",
        "timestamp": run_iso,
        "total_items": len(new_items),
        "total_sintesis": len(current_syntheses),
        "fuentes_ok": fuentes_ok,
        "fuentes_error": fuentes_error,
    }

    versions = [v for v in existing.get("versiones_disponibles", []) if v.get("id") != run_id]
    versions.insert(0, version_entry)
    versions = versions[:MAX_VERSIONS]

    output = {
        "version_etiqueta": "RSSv3",
        "ultima_actualizacion": run_iso,
        "version_actual": version_entry["etiqueta"],
        "versiones_disponibles": versions,
        "items": merged_items,
        "sintesis_originales": merged_syntheses,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(
        f"[RSSv3] Listo: {len(new_items)} nuevas | {len(current_syntheses)} síntesis | "
        f"Total: {len(merged_items)} | OK: {fuentes_ok}, Err: {fuentes_error}"
    )


if __name__ == "__main__":
    main()
