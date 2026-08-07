# Tuya Orchestrator

Integración de Home Assistant (HACS) para ingerir dispositivos Tuya **100%
por LAN**, sin nube en operación normal — pero, a diferencia de
[localtuya](https://github.com/rospogrigio/localtuya), sin modelos
hardcodeados por dispositivo. Cada dispositivo se define con un **perfil
declarativo** (YAML) que mapea sus datapoints (DPs) a entidades de Home
Assistant — la misma filosofía que ESPHome, aplicada a Tuya-por-LAN en vez
de a un firmware ESP.

Mismo enfoque "sin caja negra" que [Battery Orchestrator](https://github.com/neoalarrode/Battery-Orchestrator)
y [Climate Orchestrator](https://github.com/neoalarrode/Climate-Orchestrator):
todo inspeccionable, todo configurable por el usuario, nada aprendido por
un modelo opaco.

## Por qué un perfil declarativo y no clases por modelo

Local Tuya (y la mayoría de integraciones Tuya) mapean cada modelo de
dispositivo a código Python específico. Cada dispositivo nuevo, raro o
personalizado (DIY, firmware modificado, un DP no documentado) requiere un
PR. Acá en cambio cada dispositivo se define con un **perfil YAML** que
mapea sus datapoints (DPs) a entidades de HA — la misma filosofía que
ESPHome, aplicada a Tuya-por-LAN en vez de a un firmware ESP:

```yaml
name: Enchufe con medición de energía
dps:
  - id: 1
    platform: switch
    name: Power
  - id: 19
    platform: sensor
    name: Power
    device_class: power
    unit: W
    scale: 10        # el DP viaja como entero de punto fijo (÷10)
```

## El perfil se genera solo — no hay que escribirlo a mano

El `dp_id` de "el switch de encendido" o "el setpoint de temperatura" es
distinto **por dispositivo**, no por tipo — un enchufe puede tener el
switch en el DP 1, otro fabricante lo pone en el DP 16. Mapear a mano por
`product_id` (como se hizo al principio de este proyecto) no escala: cada
producto nuevo necesita su propio YAML escrito por alguien.

La solución real está en `auto_profile.py`: durante el alta, el config
flow le pide al **schema real del dispositivo** a Tuya Cloud
(`TuyaCloudApi.get_device_schema` — código semántico `code` + `dp_id`
numérico + tipo, por DP) y arma el perfil automáticamente mapeando cada
`code` a su rol conocido (`temp_set`→setpoint, `switch_led`→encendido de
luz, `electricity_left`→batería...), agrupado por la `category` del
dispositivo para evitar ambigüedad entre tipos. Cualquier DP no reconocido
se tipa igual de forma automática a partir de su tipo Tuya
(`Boolean`→switch/binary_sensor, `Integer`→number/sensor,
`Enum`→select/sensor...). Nada de esto depende de qué dispositivo
específico sea — funciona igual para un enchufe que jamás vimos.

El perfil auto-generado **siempre se muestra editable** en el paso
"Perfil" antes de crear la entrada — no es una caja negra. Esto importa en
la práctica: probado en vivo contra 8 dispositivos reales de 7 categorías,
detectó correctamente las 3 entidades compuestas (climate/light/vacuum) y
el resto como entidades sueltas, pero también heredó fielmente un error
real de metadata de Tuya (el `max` de temperatura del A/C, 88°C en vez de
31°C — ver caveat en `tuya_ac_basic.yaml`) porque **el schema de Tuya
decía eso**. Por diseño no hay corrección automática de datos de la nube,
solo mapeo estructural — corregir valores implausibles es responsabilidad
del usuario en ese paso de revisión.

Los perfiles a mano en `profiles/*.yaml` (los 12 que se armaron antes de
esta pieza) siguen ahí como referencia/fallback manual — el config flow
los sigue ofreciendo como opción si el auto-detectado falla o si estás
dando de alta un dispositivo sin cloud (alta manual por IP/local_key).

## Auto-agrupamiento en entidades reales, no solo DP-por-DP

Un perfil no tiene por qué mapear cada DP a su propia entidad suelta. Si
varios DPs forman en conjunto algo que Home Assistant ya modela nativamente
— un termostato, un aspirador, una luz regulable — el perfil los agrupa en
**una** entidad de ese tipo (`climate.*`, `vacuum.*`, `light.*`) con los
bloques `climates:`/`vacuums:`/`lights:`, en vez de exponerlos sueltos vía
`dps:` (switch + number + select por separado). Es la diferencia entre
"técnicamente correcto" y "usable en la realidad": nadie quiere controlar
el aire acondicionado con cuatro entidades desconectadas cuando HA tiene
una tarjeta de termostato nativa.

```yaml
climates:
  - name: AC
    switch_dp: 1
    current_temp_dp: 3
    target_temp_dp: 2
    target_temp_scale: 10
    mode_dp: 4
    mode_map:
      cold: cool
      hot: heat
      auto: heat_cool
```

Lo que **no** encaja en un tipo de entidad nativo de HA (contador de
excreciones de un arenero, vida útil de un filtro) se queda como `dps:`
suelto — eso también es "usable en la realidad", una tarjeta de sensor de
detalle es exactamente lo que se espera ahí. La regla es: agrupar lo que
HA ya sabe agrupar, dejar suelto lo que no tiene un tipo de entidad mejor.

Entidades compuestas disponibles hoy: `climates:` (termostato:
on-off/modo/setpoint/temp actual/velocidad de ventilador/preset),
`vacuums:` (aspiradora: start/pausa/dock/localizar/batería/estado/succión),
`lights:` (luz: on-off/brillo/temp. color, sin RGB todavía). Ver
`profile.py` (`ClimateMapping`/`VacuumMapping`/`LightMapping`) para el
detalle de campos de cada bloque.

## Cómo se obtiene el `local_key`

Tuya no expone el `local_key` de fábrica por ningún medio LAN — solo vía su
API de nube, una sola vez. El flujo de configuración ofrece dos caminos:

1. **Vía Tuya Cloud (recomendado)**: creás un proyecto "Cloud" gratuito en
   [iot.tuya.com](https://iot.tuya.com), vinculás tu cuenta de la app
   Tuya/Smart Life (`Cloud > Devices > Link Tuya App Account` — ahí también
   ves el UID), y le pasás Access ID/Secret + UID al wizard. Se usa
   **una sola vez**, solo para leer los `local_key`; nunca se vuelve a
   llamar a la nube durante la operación normal.
2. **Manual**: si ya conocés IP, `device_id` y `local_key` (por ejemplo
   vía `tinytuya wizard`), los pegás directo.

Después de la extracción, el descubrimiento LAN (broadcast UDP puertos
6666/6667) resuelve automáticamente la IP actual del dispositivo.

## Estado actual (v0.1.0)

- ✅ Protocolo LAN implementado directamente (framing + AES-ECB), versiones
  **3.1 y 3.3**.
- ❌ **3.4/3.5 (handshake HMAC de sesión) todavía no implementado** — la
  mayoría de dispositivos Tuya fabricados desde ~2022 usan 3.4/3.5. Si tu
  dispositivo es de esa generación, todavía no vas a poder usarlo — lanza
  `NotImplementedError` explícito en vez de fallar en silencio.
- ✅ Plataformas: `switch`, `sensor`, `number`, `binary_sensor`, `select`.
- ❌ `light`/`climate`/`cover` no implementados aún (agregar cuando un
  perfil real los necesite).
- ✅ Actualizaciones reactivas (push del propio socket LAN), no solo
  polling.
- ⚠️ **Nunca probado contra una instancia real de Home Assistant ni un
  dispositivo Tuya real** — scaffold inicial, verificado solo por
  `py_compile` y revisión manual contra las APIs documentadas de HA y
  Tuya. Reportá cualquier error real con el traceback completo.

Ver [CHANGELOG.md](CHANGELOG.md) para el detalle versión por versión.

## Perfiles incluidos (probados contra tu cuenta Tuya real)

| Perfil | Categoría Tuya | Cubre |
|---|---|---|
| `tuya_irrigation_switch` | `ggq` | Bomba de riego / válvula, on-off simple |
| `tuya_plug_basic` | `cz` | Enchufe simple (sin medición) + countdown + power-on behavior |
| `tuya_plug_energy` | `cz` | Enchufe con medición de energía (corriente/potencia/voltaje/kWh) |
| `tuya_ac_basic` | `kt` | A/C: on-off, setpoint, modo, temp/humedad actual — ⚠️ ver caveat de escala en el propio YAML, el rango de setpoint que reporta el schema de nube es implausible (16–88°C), corregilo si tu unidad muestra valores raros |
| `tuya_heater_basic` | `qn` | Calefactor (product_id `ynjanlglr4qa6dxf`): on-off, setpoint, temp actual |
| `tuya_heater_readywarm` | `qn` | Calefactor "ReadyWarm Crystal Connected" (product_id `n72zzd550zyw8qte`): on-off, bloqueo infantil, setpoint, temp actual, timer, modo Alta/Baja |
| `tuya_light_rgbcw` | `dj` | Luz: on-off, brillo, temperatura de color — **sin color RGB** (ver limitación) |
| `tuya_litter_box` | `msp` | Arenero autolimpiante: peso, excreciones, desodorizador |
| `tuya_vacuum_generic` | `sd` | Robot aspiradora genérico: básico (start/pause/dock/modo/batería/succión) |
| `tuya_vacuum_conga_s1099` | `sd` | Robot "Conga 1090/S1099": perfil completo (21 DPs — modo, velocidad, dirección, batería, vida útil de consumibles, nivel de agua, mopa, etc.) |

Los 3 calefactores y el robot S1099 quedaron **100% cubiertos** — el schema
de "Despacho"/"Pasillo abajo" (calefactores) y "S1099" (robot) no estaba
disponible vía los endpoints v1.x estándar de Tuya Cloud (`specification`,
`functions`, `status` devolvían error en los 3 casos), pero sí vía el
endpoint más nuevo **v2.0 "Thing Data Model"**
(`/v2.0/cloud/thing/{device_id}/model` +
`/v2.0/cloud/thing/{device_id}/shadow/properties`) — algunos productos
legacy solo publican su definición ahí. Si armás un perfil nuevo a mano y
el endpoint `specification` te falla, probá ese antes de rendirte.

**Limitación restante:** color RGB en luces (solo brillo + temperatura de
color por ahora, ver `tuya_light_rgbcw.yaml`).

## Instalación (HACS, repositorio personalizado)

1. HACS → Integraciones → menú (⋮) → Repositorios personalizados.
2. URL: `https://github.com/neoalarrode/Tuya-Orchestrator`, categoría
   "Integration".
3. Instalar, reiniciar Home Assistant.
4. Ajustes → Dispositivos y servicios → Agregar integración → "Tuya
   Orchestrator" → repetir una vez por dispositivo.

## Próximos pasos sugeridos

- Implementar protocolo 3.4/3.5 (handshake HMAC) — bloqueante para la
  mayoría de dispositivos modernos.
- Sumar más perfiles built-in a medida que se prueben dispositivos reales.
- `light`/`climate`/`cover` cuando haga falta.
