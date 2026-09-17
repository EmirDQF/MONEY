# Consulta automatizada del SEACE

Script asincrónico con Playwright para consultar convocatorias vigentes del
buscador público del SEACE.

## Instalación

```powershell
py -m pip install -r requirements.txt
py -m playwright install chromium
```

## Ejecución

```powershell
py seace_scraper.py
```

Los valores por defecto son año `2026`, objeto `Bienes` y descripción
`reactivos`. Se pueden cambiar con `--year`, `--object` y `--description`.
El tiempo de espera por operación es de 90 segundos; se puede cambiar con
`--timeout 120`. Para ver el navegador durante una prueba, usar `--headed`.

La salida contiene primero JSON y luego un bloque de texto listo para copiar y
enviar por mensaje.
