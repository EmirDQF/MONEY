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
DEFAULT_TIMEOUT_MS = 180_000
MAX_EXTRACTION_RESULTS = 5
NO_RESULTS_MESSAGE = "No se encontraron resultados o el portal demoró en responder"


@dataclass(slots=True)
class Convocatoria:
    codigo_proceso: str
    entidad: str
    objeto_contrato: str
    fecha_publicacion: str
    fecha_limite_registro: str
    lugar: str = ""
    ficha_tecnica: str = ""


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalized(value: str) -> str:
    return clean(value).casefold()


async def first_visible(locators: Iterable[Locator]) -> Locator | None:
    for locator in locators:
        if await locator.count() and await locator.first.is_visible():
            return locator.first
    return None


async def wait_primefaces_ajax(page: Page, timeout_ms: int) -> None:
    """Wait for PrimeFaces overlays/spinners and a short DOM settling period."""
    overlay = page.locator(
        ".ui-blockui:visible, .ui-widget-overlay:visible, "
        ".ui-dialog-mask:visible, .ui-progressbar:visible"
    )
    try:
        await overlay.first.wait_for(state="hidden", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        # Some SEACE views leave a decorative overlay visible; the DOM check
        # below is the reliable completion signal.
        pass
    await page.wait_for_timeout(500)


async def select_primefaces_option(page: Page, value: str, terms: tuple[str, ...]) -> bool:
    """Select a PrimeFaces ui-selectonemenu by visible option text."""
    menu = await first_visible(
        page.locator(
            ".ui-selectonemenu[id*='{0}' i], "
            "[id*='{0}' i].ui-selectonemenu".format(term)
        )
        for term in terms
    )
    if not menu:
        return False
    await menu.click()
    option = page.locator(
        ".ui-selectonemenu-panel:visible "
        ".ui-selectonemenu-item:visible"
    ).filter(has_text=re.compile(rf"^\s*{re.escape(value)}\s*$", re.I)).last
    await option.wait_for(state="visible", timeout=30_000)
    await option.click()
    await wait_primefaces_ajax(page, 30_000)
    return True


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
    if await select_primefaces_option(page, value, terms):
        return
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
        await wait_primefaces_ajax(page, 30_000)
        return

    await control.fill(value)


async def set_object_field(page: Page, value: str) -> None:
    """Set a native select or a PrimeFaces-style editable dropdown."""
    if await select_primefaces_option(
        page, value, ("objeto", "tipoObjeto", "objetoContratacion")
    ):
        return
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
        try:
            await control.select_option(label=value)
        except PlaywrightTimeoutError:
            await control.select_option(value=value)
        await wait_primefaces_ajax(page, 30_000)
        return

    await control.fill(value)
    await page.keyboard.press("ArrowDown")
    await page.keyboard.press("Enter")
    await wait_primefaces_ajax(page, 30_000)


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
    print("[3/4] Clic en Buscar ejecutado. Esperando respuesta AJAX de PrimeFaces...")
    await page.wait_for_timeout(3000)
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")


def header_key(header: str) -> str | None:
    value = normalized(header)
    if "nomenclatura" in value or "codigo" in value or "proceso" in value or value.startswith("n°") or value.startswith("nº"):
        return "codigo_proceso"
    if "entidad" in value or "convocante" in value:
        return "entidad"
    if "descripci" in value:
        return "objeto_contrato"
    if "objeto" in value:
        return "objeto_contrato"
    if "publicaci" in value:
        return "fecha_publicacion"
    if "limite" in value or "registro" in value or "presentacion" in value:
        return "fecha_limite_registro"
    if "departamento" in value or "ubicacion" in value or "ubicación" in value or "lugar" in value:
        return "lugar"
    return None


async def _extract_results(page: Page) -> list[Convocatoria]:
    rows = page.locator("tbody[id*='data'] tr, table tbody tr")
    count = await rows.count()
    print(f"[4/4] Filas detectadas en el DOM: {count}")
    results: list[Convocatoria] = []
    for row_index in range(count):
        row = rows.nth(row_index)
        cells = [clean(text) for text in await row.locator("td").all_text_contents()]
        if len(cells) < 5 or "ui-datatable-empty-message" in (await row.get_attribute("class") or ""):
            continue
        detail_link = row.locator("a[href]").first
        results.append(
            Convocatoria(
                codigo_proceso=cells[3],
                entidad=cells[1],
                objeto_contrato=cells[5] if len(cells) > 5 else "",
                fecha_publicacion=cells[2],
                fecha_limite_registro="",
                lugar="",
                ficha_tecnica=(
                    await detail_link.get_attribute("href")
                    if await detail_link.count()
                    else ""
                ) or "",
            )
        )
        if len(results) == MAX_EXTRACTION_RESULTS:
            break
    return results


async def extract_results(page: Page) -> list[Convocatoria]:
    try:
        return await _extract_results(page)
    except Exception as error:
        print(f"Error detectado: {error}")
        print("Manteniendo el navegador abierto 30 segundos para inspección...")
        await page.wait_for_timeout(30000)
        raise


async def _pagination_marker(page: Page) -> tuple[str, str]:
    active = page.locator(".ui-paginator-page.ui-state-active").first
    active_text = clean(await active.inner_text()) if await active.count() else ""
    rows = page.locator(
        "div[id*='tblResultados' i] tbody tr, "
        ".ui-datatable-data tr, table tbody tr"
    )
    first_row = clean(await rows.first.inner_text()) if await rows.count() else ""
    return active_text, first_row


async def paginate_results(page: Page, headed: bool) -> list[Convocatoria]:
    """Extract every PrimeFaces page without waiting for navigation events."""
    all_results: list[Convocatoria] = []
    seen_pages: set[tuple[str, str]] = set()

    while True:
        marker = await _pagination_marker(page)
        if marker in seen_pages:
            break
        seen_pages.add(marker)
        page_results = await extract_results(page)
        all_results.extend(page_results)

        next_button = page.locator(".ui-paginator-next:visible").first
        if not await next_button.count() or not await next_button.is_visible():
            if headed:
                await page.wait_for_timeout(5000)
            break
        next_class = await next_button.get_attribute("class") or ""
        if "ui-state-disabled" in next_class:
            if headed:
                await page.wait_for_timeout(5000)
            break

        await next_button.click()
        previous_marker = marker
        await wait_primefaces_ajax(page, 30_000)
        await page.wait_for_timeout(1500)

        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            current_marker = await _pagination_marker(page)
            if current_marker != previous_marker:
                break
            await page.wait_for_timeout(500)
        else:
            await capture_error(page)
            raise PlaywrightTimeoutError(
                "El paginador PrimeFaces no actualizó la tabla"
            )

    return all_results


async def capture_error(page: Page) -> None:
    try:
        await page.screenshot(path="error_seace.png", full_page=True)
    except Exception:
        pass


def write_no_results() -> None:
    with open("alertas_hoy.txt", "w", encoding="utf-8") as output:
        output.write(NO_RESULTS_MESSAGE)


def format_whatsapp_alert(licitacion: Convocatoria) -> str:
    """Return one SEACE opportunity in the required direct-message format."""
    return "\n".join(
        (
            "🔔 *NUEVA OPORTUNIDAD SEACE DETECTADA*",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            f"🏢 *Entidad:* {licitacion.entidad or 'No disponible'}",
            f"📋 *Proceso:* {licitacion.codigo_proceso or 'No disponible'}",
            f"📦 *Objeto:* {licitacion.objeto_contrato or 'No disponible'}",
            f"📅 *Fecha de publicación:* {licitacion.fecha_publicacion or 'No disponible'}",
            "🔗 *Referencia:* Buscador Público SEACE 3.0",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "⏱️ _Alerta enviada de forma automática._",
        )
    )


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


def save_alerts(results: list[Convocatoria], path: str = "alertas_hoy.txt") -> None:
    """Persist all generated alerts, separated for direct copy/paste."""
    content = "\n\n".join(format_whatsapp_alert(item) for item in results)
    with open(path, "w", encoding="utf-8") as output:
        output.write(content)


async def scrape(year: str, object_name: str, description: str, timeout_ms: int, headed: bool) -> list[Convocatoria]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not headed)
        context = await browser.new_context()
        page = await context.new_page()
        page.set_default_timeout(180000)
        page.set_default_navigation_timeout(180000)
        try:
            print("[1/4] Accediendo al portal SEACE...")
            await page.goto(URL, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_load_state("domcontentloaded")
            await page.locator("form").first.wait_for(state="visible", timeout=timeout_ms)
            await activate_procedures_search(page, timeout_ms)
            print("[2/4] Aplicando filtros (Año, Tipo de contratación, Descripción)...")
            await set_field(page, "Año de convocatoria", year, ("anio", "ano", "year"))
            await set_object_field(page, object_name)
            await set_field(
                page,
                "Descripción del Objeto",
                description,
                ("descripcionObjeto", "descripcion", "description"),
            )
            await submit_search(page, timeout_ms)
            return await paginate_results(page, headed)
        except Exception:
            await capture_error(page)
            raise
        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", default="2026")
    parser.add_argument("--object", dest="object_name", default="Bienes")
    parser.add_argument("--description", default="reactivos")
    parser.add_argument("--timeout", type=int, default=180, help="Espera máxima por operación, en segundos")
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
    print(f"\nTotal de convocatorias extraídas: {len(results)}")
    if results:
        save_alerts(results)
        generated_alerts = "\n\n".join(format_whatsapp_alert(item) for item in results)
        print("¡Extracción completada con éxito!")
        print(generated_alerts)
    else:
        write_no_results()
        print(NO_RESULTS_MESSAGE)
    print("\n--- MENSAJE ---\n")
    if not results:
        print(NO_RESULTS_MESSAGE)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (LookupError, PlaywrightTimeoutError) as error:
        print(f"Error al consultar SEACE: {error}", file=sys.stderr)
        raise SystemExit(1) from error
