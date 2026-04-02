#!/usr/bin/env python3
"""
Monitorea tarifas aproximadas Buenos Aires -> Madrid en:
- Kayak
- Aerolineas Argentinas
- Despegar

Notas:
- Estos sitios cambian seguido y pueden mostrar contenido dinámico o anti-bot.
- El script intenta detectar precios visibles en pantalla y devuelve el menor
  valor encontrado por proveedor.
- Un precio detectado no garantiza stock final al momento de pagar.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Callable, Iterable, Sequence

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


USD_PRICE_RE = re.compile(
    r"""
    (?:
        US\$|USD|\$
    )
    \s*
    (?P<value>
        \d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?|
        \d+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass
class ProviderResult:
    provider: str
    success: bool
    best_price_usd: float | None
    prices_found_usd: list[float]
    matched_target: bool
    url: str
    notes: str
    checked_at_utc: str
    depart_date: str | None = None
    return_date: str | None = None


@dataclass
class ProviderDefinition:
    name: str
    url_builder: Callable[[str | None, str | None], str]
    wait_for_ms: int = 8000
    cookie_selectors: tuple[str, ...] = ()


def kayak_url(depart_date: str | None, return_date: str | None) -> str:
    if depart_date and return_date:
        return f"https://www.kayak.com.ar/flights/BUE-MAD/{depart_date}/{return_date}"
    if depart_date:
        return f"https://www.kayak.com.ar/flights/BUE-MAD/{depart_date}?sort=bestflight_a"
    return "https://www.kayak.com.ar/vuelos/BUE-MAD"


def aerolineas_url(depart_date: str | None, return_date: str | None) -> str:
    # La home oficial suele resolver el flujo dinámicamente; dejamos los parámetros
    # en querystring para futuras mejoras y trazabilidad en logs.
    base = "https://www.aerolineasargentinas.com.ar/"
    params = []
    if depart_date:
        params.append(f"departureDate={depart_date}")
    if return_date:
        params.append(f"returnDate={return_date}")
    params.extend(("origin=BUE", "destination=MAD"))
    return f"{base}?{'&'.join(params)}" if params else base


def despegar_url(depart_date: str | None, return_date: str | None) -> str:
    if depart_date and return_date:
        return (
            "https://www.despegar.com.ar/shop/flights/results/"
            f"roundtrip/bue/mad/{depart_date}/{return_date}/1/0/0"
        )
    if depart_date:
        return (
            "https://www.despegar.com.ar/shop/flights/results/"
            f"oneway/bue/mad/{depart_date}/1/0/0"
        )
    return "https://www.despegar.com.ar/vuelos/aeropuerto/bue/mad/vuelos-a-madrid-desde-buenos+aires"


DEFAULT_PROVIDERS: tuple[ProviderDefinition, ...] = (
    ProviderDefinition(
        name="Kayak",
        url_builder=kayak_url,
        wait_for_ms=9000,
        cookie_selectors=(
            'button:has-text("Accept")',
            'button:has-text("Aceptar")',
            'button:has-text("I understand")',
        ),
    ),
    ProviderDefinition(
        name="Aerolineas Argentinas",
        url_builder=aerolineas_url,
        wait_for_ms=10000,
        cookie_selectors=(
            'button:has-text("Aceptar")',
            'button:has-text("Accept")',
        ),
    ),
    ProviderDefinition(
        name="Despegar",
        url_builder=despegar_url,
        wait_for_ms=9000,
        cookie_selectors=(
            'button:has-text("Entendido")',
            'button:has-text("Aceptar")',
            'button:has-text("Accept")',
        ),
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verifica si hay precios a Madrid cerca del objetivo indicado."
    )
    parser.add_argument(
        "--depart-date",
        default=os.getenv("DEPART_DATE"),
        help="Fecha de ida en formato YYYY-MM-DD.",
    )
    parser.add_argument(
        "--return-date",
        default=os.getenv("RETURN_DATE"),
        help="Fecha de vuelta en formato YYYY-MM-DD. Si se omite, busca solo ida o modo general.",
    )
    parser.add_argument(
        "--depart-start",
        default=os.getenv("DEPART_START"),
        help="Inicio de rango de ida en formato YYYY-MM-DD.",
    )
    parser.add_argument(
        "--depart-end",
        default=os.getenv("DEPART_END"),
        help="Fin de rango de ida en formato YYYY-MM-DD.",
    )
    parser.add_argument(
        "--step-days",
        type=int,
        default=int(os.getenv("STEP_DAYS", "7")),
        help="Cantidad de dias entre fechas probadas en un rango. Default: 7",
    )
    parser.add_argument(
        "--trip-days",
        type=int,
        default=int(os.getenv("TRIP_DAYS", "0")),
        help="Si es mayor a 0, calcula vuelta automatica sumando esos dias a la ida.",
    )
    parser.add_argument(
        "--trip-days-min",
        type=int,
        default=int(os.getenv("TRIP_DAYS_MIN", "0")),
        help="Minimo de dias de estadia para probar multiples vueltas.",
    )
    parser.add_argument(
        "--trip-days-max",
        type=int,
        default=int(os.getenv("TRIP_DAYS_MAX", "0")),
        help="Maximo de dias de estadia para probar multiples vueltas.",
    )
    parser.add_argument(
        "--target-price",
        type=float,
        default=float(os.getenv("TARGET_PRICE_USD", "700")),
        help="Precio objetivo en USD. Default: 700",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=float(os.getenv("PRICE_TOLERANCE_USD", "50")),
        help="Margen aceptable respecto del objetivo. Default: 50",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Abre el navegador visible en vez de headless.",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=int(os.getenv("BROWSER_TIMEOUT_MS", "45000")),
        help="Timeout general por sitio. Default: 45000",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Devuelve la salida como JSON además del resumen legible.",
    )
    parser.add_argument(
        "--output-json",
        default=os.getenv("OUTPUT_JSON"),
        help="Ruta de archivo para guardar resultados en JSON.",
    )
    parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="Finaliza con codigo 0 aunque no encuentre precios en rango.",
    )
    parser.add_argument(
        "--notify-email",
        default=os.getenv("NOTIFY_EMAIL"),
        help="Email destinatario para alertas cuando haya coincidencias.",
    )
    parser.add_argument(
        "--smtp-host",
        default=os.getenv("SMTP_HOST"),
        help="Servidor SMTP para enviar alertas.",
    )
    parser.add_argument(
        "--smtp-port",
        type=int,
        default=int(os.getenv("SMTP_PORT", "587")),
        help="Puerto SMTP. Default: 587",
    )
    parser.add_argument(
        "--smtp-username",
        default=os.getenv("SMTP_USERNAME"),
        help="Usuario SMTP.",
    )
    parser.add_argument(
        "--smtp-password",
        default=os.getenv("SMTP_PASSWORD"),
        help="Password o app password SMTP.",
    )
    parser.add_argument(
        "--smtp-from",
        default=os.getenv("SMTP_FROM"),
        help="Remitente visible del email.",
    )
    parser.add_argument(
        "--ntfy-topic",
        default=os.getenv("NTFY_TOPIC"),
        help="Topic de ntfy para enviar notificaciones push.",
    )
    parser.add_argument(
        "--ntfy-url",
        default=os.getenv("NTFY_URL", "https://ntfy.sh"),
        help="Base URL de ntfy. Default: https://ntfy.sh",
    )
    parser.add_argument(
        "--ntfy-token",
        default=os.getenv("NTFY_TOKEN"),
        help="Token de acceso opcional para ntfy.",
    )
    return parser.parse_args()


def validate_iso_date(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise SystemExit(f"{field_name} debe tener formato YYYY-MM-DD. Valor recibido: {value}") from exc


def parse_date_or_exit(value: str, field_name: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"{field_name} debe tener formato YYYY-MM-DD. Valor recibido: {value}") from exc


def build_date_pairs(
    depart_date: str | None,
    return_date: str | None,
    depart_start: str | None,
    depart_end: str | None,
    step_days: int,
    trip_days: int,
    trip_days_min: int,
    trip_days_max: int,
) -> list[tuple[str | None, str | None]]:
    if step_days <= 0:
        raise SystemExit("--step-days debe ser mayor a 0.")
    if trip_days < 0:
        raise SystemExit("--trip-days no puede ser negativo.")
    if trip_days_min < 0 or trip_days_max < 0:
        raise SystemExit("--trip-days-min y --trip-days-max no pueden ser negativos.")
    if (trip_days_min == 0) ^ (trip_days_max == 0):
        raise SystemExit("Debes indicar ambos: --trip-days-min y --trip-days-max.")
    if trip_days_min and trip_days_max and trip_days_max < trip_days_min:
        raise SystemExit("--trip-days-max no puede ser menor que --trip-days-min.")
    if trip_days and (trip_days_min or trip_days_max):
        raise SystemExit("Usa --trip-days o el rango --trip-days-min/--trip-days-max, no ambos.")

    trip_lengths: list[int] = []
    if trip_days > 0:
        trip_lengths = [trip_days]
    elif trip_days_min and trip_days_max:
        trip_lengths = list(range(trip_days_min, trip_days_max + 1))

    if depart_date:
        inferred_return = return_date
        if inferred_return is None and trip_lengths:
            base_depart = parse_date_or_exit(depart_date, "--depart-date")
            return [
                (depart_date, (base_depart + timedelta(days=length)).isoformat())
                for length in trip_lengths
            ]
        return [(depart_date, inferred_return)]

    if depart_start or depart_end:
        if not (depart_start and depart_end):
            raise SystemExit("Debes indicar ambos: --depart-start y --depart-end.")
        start = parse_date_or_exit(depart_start, "--depart-start")
        end = parse_date_or_exit(depart_end, "--depart-end")
        if end < start:
            raise SystemExit("--depart-end no puede ser anterior a --depart-start.")

        pairs: list[tuple[str | None, str | None]] = []
        current = start
        while current <= end:
            current_depart = current.isoformat()
            if trip_lengths:
                for length in trip_lengths:
                    current_return = (current + timedelta(days=length)).isoformat()
                    pairs.append((current_depart, current_return))
            else:
                pairs.append((current_depart, None))
            current += timedelta(days=step_days)
        return pairs

    return [(None, None)]


def normalize_price(raw: str) -> float | None:
    cleaned = raw.strip().replace(" ", "")
    if not cleaned:
        return None

    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif cleaned.count(",") == 1 and len(cleaned.split(",")[-1]) == 2:
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")

    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_usd_prices(text: str) -> list[float]:
    prices: list[float] = []
    for match in USD_PRICE_RE.finditer(text):
        value = normalize_price(match.group("value"))
        if value is None:
            continue
        if 150 <= value <= 5000:
            prices.append(value)
    return sorted(set(prices))


def try_click_any(page, selectors: Sequence[str]) -> None:
    for selector in selectors:
        try:
            button = page.locator(selector).first
            if button.is_visible(timeout=1000):
                button.click(timeout=2000)
                page.wait_for_timeout(700)
                return
        except Exception:
            continue


def collect_page_text(page) -> str:
    chunks: list[str] = []
    for selector in ("body", "main", '[role="main"]'):
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                text = locator.inner_text(timeout=3000)
                if text:
                    chunks.append(text)
        except Exception:
            continue
    return "\n".join(chunks)


def build_result(
    definition: ProviderDefinition,
    url: str,
    depart_date: str | None,
    return_date: str | None,
    prices: Iterable[float],
    target_price: float,
    tolerance: float,
    notes: str,
    success: bool,
) -> ProviderResult:
    values = sorted(set(prices))
    best = values[0] if values else None
    matched = best is not None and (target_price - tolerance) <= best <= (target_price + tolerance)
    return ProviderResult(
        provider=definition.name,
        success=success,
        best_price_usd=best,
        prices_found_usd=values,
        matched_target=matched,
        url=url,
        notes=notes,
        checked_at_utc=datetime.now(timezone.utc).isoformat(),
        depart_date=depart_date,
        return_date=return_date,
    )


def check_provider(
    definition: ProviderDefinition,
    depart_date: str | None,
    return_date: str | None,
    target_price: float,
    tolerance: float,
    headless: bool,
    timeout_ms: int,
) -> ProviderResult:
    url = definition.url_builder(depart_date, return_date)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(locale="es-AR", timezone_id="America/Argentina/Buenos_Aires")
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(definition.wait_for_ms)
            try_click_any(page, definition.cookie_selectors)

            try:
                page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 12000))
            except PlaywrightTimeoutError:
                pass

            text = collect_page_text(page)
            prices = extract_usd_prices(text)

            if prices:
                notes = "Se extrajeron precios visibles en la pagina."
                return build_result(definition, url, depart_date, return_date, prices, target_price, tolerance, notes, True)

            notes = (
                "No se detectaron precios en USD. Puede requerir ajustar "
                "selectores, fecha, moneda o resolver anti-bot/captcha."
            )
            return build_result(definition, url, depart_date, return_date, [], target_price, tolerance, notes, False)

        except Exception as exc:
            return build_result(
                definition,
                url,
                depart_date,
                return_date,
                [],
                target_price,
                tolerance,
                f"Fallo durante la consulta: {exc}",
                False,
            )
        finally:
            context.close()
            browser.close()


def print_human_summary(results: Sequence[ProviderResult], target_price: float, tolerance: float) -> None:
    print(f"Objetivo: USD {target_price:.2f} (+/- {tolerance:.2f})")
    print("")
    for result in results:
        if result.best_price_usd is None:
            status = "SIN DATOS"
            best = "n/d"
        elif result.matched_target:
            status = "EN RANGO"
            best = f"USD {result.best_price_usd:.2f}"
        else:
            status = "FUERA DE RANGO"
            best = f"USD {result.best_price_usd:.2f}"

        print(f"[{status}] {result.provider}")
        if result.depart_date:
            route_dates = f"  Fecha ida: {result.depart_date}"
            if result.return_date:
                route_dates += f" | Fecha vuelta: {result.return_date}"
            print(route_dates)
        print(f"  URL: {result.url}")
        print(f"  Mejor precio detectado: {best}")
        print(f"  Cantidad de precios detectados: {len(result.prices_found_usd)}")
        print(f"  Nota: {result.notes}")
        print("")

    matches = [item for item in results if item.matched_target]
    if matches:
        providers = ", ".join(item.provider for item in matches)
        print(f"Resultado final: se detectaron opciones cerca de USD {target_price:.2f} en {providers}.")
    else:
        print(f"Resultado final: no se detectaron opciones dentro del rango objetivo en esta corrida.")


def build_email_body(matches: Sequence[ProviderResult], target_price: float, tolerance: float) -> str:
    lines = [
        f"Se detectaron vuelos dentro del rango objetivo de USD {target_price:.2f} (+/- {tolerance:.2f}).",
        "",
    ]
    for result in matches:
        lines.append(f"Proveedor: {result.provider}")
        if result.depart_date:
            lines.append(f"Fecha ida: {result.depart_date}")
        if result.return_date:
            lines.append(f"Fecha vuelta: {result.return_date}")
        if result.best_price_usd is not None:
            lines.append(f"Mejor precio detectado: USD {result.best_price_usd:.2f}")
        lines.append(f"URL: {result.url}")
        lines.append(f"Nota: {result.notes}")
        lines.append("")
    return "\n".join(lines).strip()


def send_email_alert(
    matches: Sequence[ProviderResult],
    target_price: float,
    tolerance: float,
    notify_email: str | None,
    smtp_host: str | None,
    smtp_port: int,
    smtp_username: str | None,
    smtp_password: str | None,
    smtp_from: str | None,
) -> None:
    if not matches or not notify_email:
        return

    required = {
        "SMTP_HOST": smtp_host,
        "SMTP_USERNAME": smtp_username,
        "SMTP_PASSWORD": smtp_password,
        "SMTP_FROM": smtp_from,
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise SystemExit(
            "Faltan variables SMTP para enviar email: " + ", ".join(missing)
        )

    message = EmailMessage()
    message["Subject"] = f"Alerta vuelos BUE -> MAD cerca de USD {target_price:.0f}"
    message["From"] = smtp_from
    message["To"] = notify_email
    message.set_content(build_email_body(matches, target_price, tolerance))

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.send_message(message)


def send_ntfy_alert(
    matches: Sequence[ProviderResult],
    target_price: float,
    tolerance: float,
    ntfy_topic: str | None,
    ntfy_url: str,
    ntfy_token: str | None,
) -> None:
    if not matches or not ntfy_topic:
        return

    body = build_email_body(matches, target_price, tolerance)
    publish_url = f"{ntfy_url.rstrip('/')}/{ntfy_topic}"
    request = urllib.request.Request(
        publish_url,
        data=body.encode("utf-8"),
        method="POST",
        headers={
            "Title": f"Vuelos BUE -> MAD cerca de USD {target_price:.0f}",
            "Priority": "default",
            "Tags": "airplane,money_with_wings",
        },
    )
    if ntfy_token:
        request.add_header("Authorization", f"Bearer {ntfy_token}")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status >= 400:
                raise SystemExit(f"ntfy devolvio status {response.status}")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Error publicando en ntfy: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Error conectando con ntfy: {exc.reason}") from exc


def main() -> int:
    args = parse_args()
    depart_date = validate_iso_date(args.depart_date, "--depart-date")
    return_date = validate_iso_date(args.return_date, "--return-date")
    depart_start = validate_iso_date(args.depart_start, "--depart-start")
    depart_end = validate_iso_date(args.depart_end, "--depart-end")
    date_pairs = build_date_pairs(
        depart_date=depart_date,
        return_date=return_date,
        depart_start=depart_start,
        depart_end=depart_end,
        step_days=args.step_days,
        trip_days=args.trip_days,
        trip_days_min=args.trip_days_min,
        trip_days_max=args.trip_days_max,
    )

    try:
        results = [
            check_provider(
                definition=provider,
                depart_date=current_depart,
                return_date=current_return,
                target_price=args.target_price,
                tolerance=args.tolerance,
                headless=not args.headful,
                timeout_ms=args.timeout_ms,
            )
            for current_depart, current_return in date_pairs
            for provider in DEFAULT_PROVIDERS
        ]
    except KeyboardInterrupt:
        print("Interrumpido por el usuario.", file=sys.stderr)
        return 130

    print_human_summary(results, args.target_price, args.tolerance)

    payload = [asdict(result) for result in results]

    if args.json:
        print("")
        print(json.dumps(payload, ensure_ascii=True, indent=2))

    if args.output_json:
        with open(args.output_json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=True, indent=2)

    matches = [item for item in results if item.matched_target]
    send_email_alert(
        matches=matches,
        target_price=args.target_price,
        tolerance=args.tolerance,
        notify_email=args.notify_email,
        smtp_host=args.smtp_host,
        smtp_port=args.smtp_port,
        smtp_username=args.smtp_username,
        smtp_password=args.smtp_password,
        smtp_from=args.smtp_from,
    )
    send_ntfy_alert(
        matches=matches,
        target_price=args.target_price,
        tolerance=args.tolerance,
        ntfy_topic=args.ntfy_topic,
        ntfy_url=args.ntfy_url,
        ntfy_token=args.ntfy_token,
    )

    if args.exit_zero:
        return 0
    return 0 if matches else 1


if __name__ == "__main__":
    raise SystemExit(main())