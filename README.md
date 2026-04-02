# Monitor de pasajes Buenos Aires -> Madrid

Este proyecto consulta `Kayak`, `Aerolíneas Argentinas` y `Despegar` para detectar si aparecen precios visibles cerca de `700 USD`.

## Archivos

- `flight_price_monitor.py`: script principal.
- `requirements.txt`: dependencia de Python.

## Instalación

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Uso

```bash
python flight_price_monitor.py
```

Opciones útiles:

```bash
python flight_price_monitor.py --target-price 700 --tolerance 50 --json
python flight_price_monitor.py --headful
python flight_price_monitor.py --depart-date 2026-09-14 --return-date 2026-09-28 --json
python flight_price_monitor.py --depart-date 2026-09-14
python flight_price_monitor.py --depart-start 2026-10-01 --depart-end 2026-12-31 --step-days 7
python flight_price_monitor.py --depart-start 2026-10-01 --depart-end 2026-12-31 --step-days 7 --trip-days 14
python flight_price_monitor.py --depart-start 2026-10-01 --depart-end 2026-12-31 --step-days 7 --trip-days-min 18 --trip-days-max 20
```

Variables de entorno opcionales:

- `TARGET_PRICE_USD`: default `700`
- `PRICE_TOLERANCE_USD`: default `50`
- `BROWSER_TIMEOUT_MS`: default `45000`
- `DEPART_DATE`: fecha de ida `YYYY-MM-DD`
- `RETURN_DATE`: fecha de vuelta `YYYY-MM-DD`
- `DEPART_START`: inicio de rango flexible `YYYY-MM-DD`
- `DEPART_END`: fin de rango flexible `YYYY-MM-DD`
- `STEP_DAYS`: salto entre fechas probadas, default `7`
- `TRIP_DAYS`: si querés ida y vuelta automática, suma esos días a la ida
- `TRIP_DAYS_MIN`: mínimo de estadía para probar un rango
- `TRIP_DAYS_MAX`: máximo de estadía para probar un rango
- `OUTPUT_JSON`: archivo donde guardar el JSON
- `NOTIFY_EMAIL`: destinatario de la alerta
- `SMTP_HOST`: host SMTP
- `SMTP_PORT`: puerto SMTP, default `587`
- `SMTP_USERNAME`: usuario SMTP
- `SMTP_PASSWORD`: password SMTP o app password
- `SMTP_FROM`: remitente visible
- `NTFY_TOPIC`: topic de ntfy para push notifications
- `NTFY_URL`: servidor ntfy, default `https://ntfy.sh`
- `NTFY_TOKEN`: token opcional si usás un topic protegido

## Qué hace

- Abre cada web con `Playwright`.
- Si le pasás fechas, intenta abrir una búsqueda más específica por proveedor.
- Si le pasás un rango, prueba fechas separadas por `step-days`.
- Si le pasás `trip-days-min` y `trip-days-max`, prueba varias duraciones de estadía.
- Espera a que cargue contenido dinámico.
- Intenta cerrar banners de cookies comunes.
- Extrae precios visibles en USD desde el texto renderizado.
- Marca como coincidencia los precios dentro del rango `objetivo +/- tolerancia`.
- Puede devolver siempre exit code `0` con `--exit-zero`, útil para tareas programadas.
- Si configurás SMTP, envía email cuando detecta coincidencias dentro del rango.
- Si configurás ntfy, envía una notificación push cuando detecta coincidencias.

## Limitaciones importantes

- Los sitios de viajes cambian seguido y pueden tener `captcha`, anti-bot o selectores distintos.
- En `Aerolíneas Argentinas` puede ser necesario ajustar el flujo si el precio no aparece en la home o si el buscador exige interacción adicional.
- El precio detectado es una señal temprana, no una confirmación final de stock al momento de pagar.

## GitHub Actions

Hay un workflow listo en `.github/workflows/flight-monitor.yml` para correr gratis en GitHub Actions.

Qué hace:

- Se ejecuta manualmente o cada 6 horas.
- Instala Python y `Playwright` en `ubuntu-latest`.
- Corre el monitor para octubre a diciembre con estadías entre 18 y 20 días.
- Si hay coincidencias y configuraste secretos SMTP, envía un email.
- Si hay coincidencias y configuraste ntfy, envía una push notification.
- Sube `results.json` y `results.txt` como artifact del job.

### Secretos para ntfy

En `Settings > Secrets and variables > Actions`, cargá:

- `NTFY_TOPIC`
- `NTFY_URL`
- `NTFY_TOKEN` opcional

Ejemplo:

- `NTFY_TOPIC`: tu topic
- `NTFY_URL`: `https://ntfy.sh`

Cuando el monitor encuentre precios en rango, GitHub Actions va a publicar una notificación en ese topic y la vas a ver en tu computadora.
