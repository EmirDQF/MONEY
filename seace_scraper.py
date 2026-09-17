"""Consulta convocatorias vigentes del buscador publico del SEACE.

Uso:
    python seace_scraper.py
    python seace_scraper.py --headed --timeout 120
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterable

from playwright.async_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


URL = "https://prodapp2.seace.gob.pe/seacebus-uiwd-pub/buscadorConvocatorias/inicio.xhtml"
DEFAULT_TIMEOUT_MS = 90_000
MAX_RESULTS = 3


@dataclass(slots=True)
class Convocatoria:
    codigo_proceso: str
    entidad: str
    objeto_contrato: str
    fecha_publicacion: str
    fecha_limite_registro: str


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalized(value: str) -> str:
    return clean(value).casefold()


async def first_visible(locators: Iterable[Locator]) -> Locator | None:
    for locator in locators:
        if await locator.count() and await locator.first.is_visible():
            return locator.first
    return None


async def control_for_label(page: Page, label_text: str, fallback_terms: tuple[str, ...]) -> Locator:
    """Find a JSF input/select using its label first, then stable ID/name fragments."""
    label = page.locator("label").filter(has_text=re.compile(re.escape(label_text), re.I)).first
    if await label.count():
        target_id = await label.get_attribute("for")
        if target_id:
            control = page.locator(f"#{re.escape(target_id)}")
            if await control.count():
                return control.first

    for term in fallback_terms:
        candidate = await first_visible(
            (
                page.locator(f"select[id*='{term}' i], select[name*='{term}' i]"),
                page.locator(
                    f"input[id*='{term}' i], input[name*='{term}' i], "
                    f"textarea[id*='{term}' i]"
                ),
            )
        )
        if candidate:
            return candidate

    raise LookupError(f"No se encontró el campo '{label_text}'")


async def set_field(page: Page, label: str, value: str, terms: tuple[str, ...]) -> None:
    try:
        control = await control_for_label(page, label, terms)
    except LookupError:
        if normalized(label).startswith("descripci"):
            control = page.locator("input[id$=':descripcionObjeto']:visible").first
            if not await control.count():
                raise
        else:
            raise
    tag_name = await control.evaluate("(element) => element.tagName.toLowerCase()")
    if tag_name == "select":
        try:
            await control.select_option(label=value)
        except PlaywrightTimeoutError:
            await control.select_option(value=value)
        return

    await control.fill(value)


async def set_object_field(page: Page, value: str) -> None:
    """Set a native select or a PrimeFaces-style editable dropdown."""
    try:
        control = await control_for_label(
            page,
            "Objeto de Contratación",
            ("objeto", "tipoObjeto", "objetoContratacion"),
        )
    except LookupError:
        # The current JSF view gives this autocomplete a generated ID. In the
        # active procedure form it is the third visible text control.
        visible_inputs = page.locator("input:visible")
        if await visible_inputs.count() < 3:
            raise
        control = visible_inputs.nth(2)
    tag_name = await control.evaluate("(element) => element.tagName.toLowerCase()")
    if tag_name == "select":
        await control.select_option(label=value)
        return

    await control.fill(value)
    await page.keyboard.press("ArrowDown")
    await page.keyboard.press("Enter")


async def activate_procedures_search(page: Page, timeout_ms: int) -> None:
    """The legacy URL currently redirects to the public search with another tab active."""
    procedure_link = page.locator("a[href*='tbBuscador:tab1']").first
    if await procedure_link.count():
        await procedure_link.click()
    await page.locator(
        "input[id$=':anioConvocatoria_focus'], "
        "input[name*='anioConvocatoria' i]"
    ).first.wait_for(state="visible", timeout=timeout_ms)


async def submit_search(page: Page, timeout_ms: int) -> None:
    button = await first_visible(
        (
            page.get_by_role("button", name=re.compile(r"^\s*buscar\s*$", re.I)),
            page.locator("input[type='submit'][value*='Buscar' i]"),
            page.locator("button:has-text('Buscar')"),
        )
    )
    if not button:
        raise LookupError("No se encontró el botón Buscar")
    await button.click()

    # JSF updates the result component asynchronously; wait for either rows or
    # a visible no-results message instead of relying only on network idle.
    result_locators = (
        page.locator("table tbody tr"),
        page.locator("[id*='resultado' i], [id*='mensaje' i]"),
        page.get_by_text(re.compile(r"no se encontraron|sin resultados", re.I)),
    )
    for _ in range(max(1, timeout_ms // 500)):
        if await first_visible(result_locators):
            return
        await asyncio.sleep(0.5)
    raise PlaywrightTimeoutError("Los resultados del buscador no aparecieron a tiempo")


def header_key(header: str) -> str | None:
    value = normalized(header)
    if "codigo" in value or "proceso" in value or value.startswith("n°") or value.startswith("nº"):
        return "codigo_proceso"
    if "entidad" in value or "convocante" in value:
        return "entidad"
    if "objeto" in value or "descripci" in value:
        return "objeto_contrato"
    if "publicaci" in value:
        return "fecha_publicacion"
    if "limite" in value or "registro" in value or "presentacion" in value:
        return "fecha_limite_registro"
    return None


async def extract_results(page: Page) -> list[Convocatoria]:
    tables = page.locator("table")
    for table_index in range(await tables.count()):
        table = tables.nth(table_index)
        headers = [clean(text) for text in await table.locator("thead th").all_text_contents()]
        rows = table.locator("tbody tr")
        if not headers or not await rows.count():
            continue

        keys = [header_key(header) for header in headers]
        if "codigo_proceso" not in keys and not any("entidad" in normalized(h) for h in headers):
            continue

        results: list[Convocatoria] = []
        for row_index in range(await rows.count()):
            row = rows.nth(row_index)
            cells = [clean(text) for text in await row.locator("td").all_text_contents()]
            if not cells:
                continue
            row_text = normalized(" ".join(cells))
            if any(term in row_text for term in ("cancelado", "desierto", "culminado", "no vigente")):
                continue

            values = {
                key: cells[index] if index < len(cells) else ""
                for index, key in enumerate(keys)
                if key
            }
            results.append(
                Convocatoria(
                    codigo_proceso=values.get("codigo_proceso", ""),
                    entidad=values.get("entidad", ""),
                    objeto_contrato=values.get("objeto_contrato", ""),
                    fecha_publicacion=values.get("fecha_publicacion", ""),
                    fecha_limite_registro=values.get("fecha_limite_registro", ""),
                )
            )
            if len(results) == MAX_RESULTS:
                return results
        if results:
            return results

    raise LookupError("No se encontró una tabla de resultados con convocatorias")


def format_message(results: list[Convocatoria]) -> str:
    lines = ["Convocatorias vigentes SEACE", ""]
    for index, item in enumerate(results, start=1):
        lines.extend(
            (
                f"{index}. {item.codigo_proceso or 'Sin código'}",
                f"Entidad: {item.entidad or 'No disponible'}",
                f"Objeto: {item.objeto_contrato or 'No disponible'}",
                f"Publicación: {item.fecha_publicacion or 'No disponible'}",
                f"Límite de registro: {item.fecha_limite_registro or 'No disponible'}",
                "",
            )
        )
    return "\n".join(lines).strip()


async def scrape(year: str, object_name: str, description: str, timeout_ms: int, headed: bool) -> list[Convocatoria]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not headed)
        page = await browser.new_page()
        page.set_default_timeout(timeout_ms)
        try:
            await page.goto(URL, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except PlaywrightTimeoutError:
                # The portal may keep polling; the form can still be usable.
                pass
            await page.locator("form").first.wait_for(state="visible", timeout=timeout_ms)
            await activate_procedures_search(page, timeout_ms)
            await set_field(page, "Año de convocatoria", year, ("anio", "ano", "year"))
            await set_object_field(page, object_name)
            await set_field(
                page,
                "Descripción del Objeto",
                description,
                ("descripcionObjeto", "descripcion", "description"),
            )
            await submit_search(page, timeout_ms)
            return await extract_results(page)
        finally:
            await browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", default="2026")
    parser.add_argument("--object", dest="object_name", default="Bienes")
    parser.add_argument("--description", default="reactivos")
    parser.add_argument("--timeout", type=int, default=90, help="Espera máxima por operación, en segundos")
    parser.add_argument("--headed", action="store_true", help="Muestra el navegador para depuración")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    results = await scrape(
        args.year,
        args.object_name,
        args.description,
        args.timeout * 1000,
        args.headed,
    )
    payload = {
        "consultado_en": datetime.now().astimezone().isoformat(timespec="seconds"),
        "filtros": {
            "anio": args.year,
            "objeto": args.object_name,
            "descripcion": args.description,
        },
        "convocatorias": [asdict(item) for item in results],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("\n--- MENSAJE ---\n")
    print(format_message(results))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (LookupError, PlaywrightTimeoutError) as error:
        print(f"Error al consultar SEACE: {error}", file=sys.stderr)
        raise SystemExit(1) from error
