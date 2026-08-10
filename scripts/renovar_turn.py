"""Renueva las credenciales TURN de Cloudflare Realtime en el `.env`.

Uso: `make turn` (en la placa). Lee `TURN_KEY_ID` y `TURN_KEY_TOKEN` del
`.env`, pide credenciales ICE con 48 horas de vida —el máximo— y reescribe
`ICE_SERVERS`, `TURN_USERNAME` y `TURN_CREDENTIAL`. Hay que reiniciar el
servicio web después (`systemctl --user restart clara-web`).

Las credenciales caducan: ejecutar esto la mañana de la sesión de evaluación.
Solo biblioteca estándar, a propósito.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

#: Las URLs que se anuncian al pipeline. El `turns:` por 443 es el que
#: atraviesa las redes más cerradas (parece HTTPS).
URLS = (
    "stun:stun.cloudflare.com:3478,"
    "turn:turn.cloudflare.com:3478?transport=udp,"
    "turn:turn.cloudflare.com:80?transport=tcp,"
    "turns:turn.cloudflare.com:443?transport=tcp"
)


def main() -> int:
    """Renueva las credenciales y reescribe el `.env`."""
    ruta = Path(".env")
    if not ruta.is_file():
        print("No hay .env en el directorio actual; ejecuta desde la raíz del proyecto.")
        return 1
    env = ruta.read_text(encoding="utf-8")

    def valor(clave: str) -> str | None:
        m = re.search(rf"^{clave}=(.+)$", env, flags=re.M)
        return m.group(1).strip() if m else None

    key_id, token = valor("TURN_KEY_ID"), valor("TURN_KEY_TOKEN")
    if not key_id or not token:
        print(
            "Faltan TURN_KEY_ID o TURN_KEY_TOKEN en el .env (dashboard de Cloudflare → Realtime)."
        )
        return 1

    peticion = urllib.request.Request(
        f"https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate-ice-servers",
        data=json.dumps({"ttl": 172800}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(peticion, timeout=30) as respuesta:
        datos = json.load(respuesta)

    turn = next(s for s in datos["iceServers"] if s.get("username"))

    for clave, nuevo in (
        ("ICE_SERVERS", URLS),
        ("TURN_USERNAME", turn["username"]),
        ("TURN_CREDENTIAL", turn["credential"]),
    ):
        if re.search(rf"^{clave}=", env, flags=re.M):
            env = re.sub(rf"^{clave}=.*$", f"{clave}={nuevo}", env, flags=re.M)
        else:
            env += f"{clave}={nuevo}\n"
    ruta.write_text(env, encoding="utf-8")

    print("Credenciales TURN renovadas (48 h). Reinicia el servicio:")
    print("    systemctl --user restart clara-web")
    return 0


if __name__ == "__main__":
    sys.exit(main())
